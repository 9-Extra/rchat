"""测试脚手架：临时会话目录、假配置、假模型，纯 stdlib unittest（不引入新依赖）。

跑法（仓库根目录）：

    uv run python -m unittest discover -s tests -t .          # 全部
    uv run python -m unittest tests.test_server_locks -v      # 单个

这里不启动真实服务器、不连任何模型端点：`server.stream_body` / `server.generate_options`
被换成假的，所以整套测试离线可跑，也不会碰你 `sessions/` 里的真数据。
"""
import asyncio
import copy
import json
import logging
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import auth, core, server, tools, world  # noqa: E402

# 用例里有故意触发的错误（错误密码、假模型失败、宽限超时），日志不用刷屏
logging.disable(logging.CRITICAL)

# 这两个是 .gitignore 白名单里保证存在的预设/角色卡，任何 clone 都有
TEST_PRESET = "GM-safe"
TEST_CARD = "龙娘x猫娘"

TEST_CONFIG = {
    "endpoints": [{
        "display_name": "测试端点",
        "api_base": "http://127.0.0.1:1/v1",
        "model": "test-model",
        "api_key": "test-key",
        "api_type": "chat_completions",
    }],
    "temperature": 0.7,
    "max_tokens": 256,
    "reasoning_effort": "low",
    "options_enabled": False,
}

TEST_USERS = {"alice": "alice-pass", "bob": "bob-pass"}


class FakeLLM:
    """假的模型输出：按分片流式吐正文，可选先来一次 world_run 工具调用。

    契约与 app/llm.py 的 stream_body 一致：yield reasoning/content/tool 事件，
    最后 yield 一个 done（带完整 content 与 reasoning）。
    """

    def __init__(self, chunks=("你好", "，这是假模型的正文。"), reasoning=("想一下。",),
                 tool_programs=None, delay=0.02, fail=False):
        self.chunks = list(chunks)
        self.reasoning = list(reasoning)
        self.tool_programs = list(tool_programs or [])
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def __call__(self, input_items, config, run_tool):
        self.calls += 1
        if self.fail:
            raise RuntimeError("假模型故意失败")
        for piece in self.reasoning:
            await asyncio.sleep(self.delay)
            yield {"type": "reasoning", "delta": piece}
        # 工具调用（第一遍才发；结果由真正的 tools.execute_tool 执行）
        if self.tool_programs:
            program = self.tool_programs.pop(0)
            args = json.dumps({"program": program})
            result = await run_tool("world_run", args)
            yield {"type": "tool", "name": "world_run", "arguments": args,
                   "result": result, "reasoning": "".join(self.reasoning)}
        content = ""
        for piece in self.chunks:
            await asyncio.sleep(self.delay)
            content += piece
            yield {"type": "content", "delta": piece}
        yield {"type": "done", "content": content, "reasoning": "".join(self.reasoning)}


async def fake_generate_options(options_input, config):
    """假的选项请求：固定返回两条。"""
    await asyncio.sleep(0)
    return ["先去公会看看", "直接出城"], None, ""


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    """把 server 跑在进程内（httpx ASGI），会话目录指向临时目录。"""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-test-")
        self.sessions_dir = pathlib.Path(self._tmp.name) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        patches = [
            mock.patch.object(core, "SESSIONS_DIR", self.sessions_dir),
            mock.patch.object(core, "load_config", lambda: copy.deepcopy(TEST_CONFIG)),
            mock.patch.object(auth, "auth_settings", self._auth_settings),
            mock.patch.object(server, "generate_options", fake_generate_options),
        ]
        for p in patches:
            self.addCleanup(p.stop)
            p.start()

        # 全局状态清干净，免得上一个用例的会话状态串味
        for reg in (server._active, server._locks, world._runtimes, auth._sessions, auth._fails):
            reg.clear()
        self.addCleanup(self._tmp.cleanup)

        import httpx

        self.llm = FakeLLM()
        stream_patch = mock.patch.object(server, "stream_body", self.llm)
        self.addCleanup(stream_patch.stop)
        stream_patch.start()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url="http://testserver",
        )
        self.addCleanup(self.client.aclose)

    # 鉴权默认关（本地模式）；需要鉴权的用例把它打开
    auth_enabled = False

    def _auth_settings(self, config=None):
        return {
            "enabled": self.auth_enabled,
            "users": TEST_USERS if self.auth_enabled else {},
            "ttl": 30 * 86400,
            "cookie_secure": False,
        }

    # ---------- 小工具 ----------

    async def login(self, user="alice"):
        r = await self.client.post("/api/login", json={"username": user, "password": TEST_USERS[user]})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    async def new_session(self, name="s1", preset=TEST_PRESET, card=TEST_CARD):
        r = await self.client.post("/api/sessions", json={
            "name": name, "preset": preset, "card": card, "beginning_index": 0,
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def get_session(self, name="s1"):
        r = await self.client.get(f"/api/sessions/{name}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def turn(self, path, body=None, name="s1"):
        """跑一轮生成，返回 (状态码, 事件列表)。假模型很快，直接收完整包。"""
        r = await self.client.post(f"/api/sessions/{name}{path}", json=body)
        events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        return r.status_code, events

    def key(self, user, name="s1"):
        return core.session_key(user, name)

    def write_history(self, user, entries, name="s1"):
        core.save_history(self.key(user, name), entries)


def program_for(ticks=30, sleep=0.05):
    """一段会持续改 state 的 world_run 程序（用来测打断时的收尾）。"""
    return f"import time\nfor i in range({ticks}):\n    state['ticks'] = i\n    time.sleep({sleep})"
