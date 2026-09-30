"""多用户 API 端到端（进程内跑 ASGI，不联网、不碰真 sessions/）。

覆盖：鉴权闸门与 cookie、两个用户同名会话的隔离、一轮生成与落盘、
生成中禁止改会话、生成前落盘用户输入、崩溃恢复（末尾 user 块点「继续」）、
回滚 / fork / 删除、注销。
"""
import json
import unittest

import tests.support  # noqa: F401

from app import core
from tests.support import TEST_USERS, AppTestCase


class AuthGateTest(AppTestCase):
    auth_enabled = True

    async def test_unauthenticated_is_rejected(self):
        r = await self.client.get("/api/sessions")
        self.assertEqual(r.status_code, 401)
        r = await self.client.get("/api/presets")
        self.assertEqual(r.status_code, 401)
        r = await self.client.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["auth_enabled"])
        self.assertIsNone(r.json()["user"])

    async def test_login_sets_httponly_cookie(self):
        r = await self.client.post("/api/login", json={"username": "alice", "password": "wrong"})
        self.assertEqual(r.status_code, 401)
        r = await self.login("alice")
        cookie = r.headers.get("set-cookie", "").lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertEqual((await self.client.get("/api/me")).json()["user"], "alice")

    async def test_logout_invalidates(self):
        await self.login()
        await self.client.post("/api/logout")
        self.assertEqual((await self.client.get("/api/sessions")).status_code, 401)

    async def test_rate_limit_is_per_user(self):
        for _ in range(5):
            await self.client.post("/api/login", json={"username": "carol", "password": "nope"})
        r = await self.client.post("/api/login", json={"username": "carol", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("次数过多", r.text)
        # 别人不受影响
        r = await self.client.post("/api/login", json={"username": "alice", "password": TEST_USERS["alice"]})
        self.assertEqual(r.status_code, 200)


class IsolationTest(AppTestCase):
    auth_enabled = True

    async def test_same_name_sessions_are_isolated(self):
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server_app),
                                     base_url="http://testserver") as alice, \
                   httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server_app),
                                     base_url="http://testserver") as bob:
            for client, user, preset in ((alice, "alice", "GM-safe"), (bob, "bob", "GM-ptc")):
                r = await client.post("/api/login", json={"username": user, "password": TEST_USERS[user]})
                self.assertEqual(r.status_code, 200, r.text)
                r = await client.post("/api/sessions", json={"name": "same", "preset": preset,
                                                             "card": "龙娘x猫娘", "beginning_index": 0})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["id"], "same", "API 只暴露裸会话名")
                self.assertEqual(r.json()["owner"], user)

            # 各自只看到自己的
            self.assertEqual([s["id"] for s in (await alice.get("/api/sessions")).json()], ["same"])
            self.assertEqual([s["id"] for s in (await bob.get("/api/sessions")).json()], ["same"])
            self.assertEqual((await alice.get("/api/sessions/same")).json()["preset"], "GM-safe")
            self.assertEqual((await bob.get("/api/sessions/same")).json()["preset"], "GM-ptc")

        # 落盘确实分了两层目录
        self.assertTrue((self.sessions_dir / "alice" / "same" / "state.json").exists())
        self.assertTrue((self.sessions_dir / "bob" / "same" / "state.json").exists())

    @property
    def server_app(self):
        from app import server
        return server.app

    async def test_new_session_inherits_own_users_params_only(self):
        import httpx

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server_app),
                                     base_url="http://testserver") as alice, \
                   httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server_app),
                                     base_url="http://testserver") as bob:
            for client, user in ((alice, "alice"), (bob, "bob")):
                await client.post("/api/login", json={"username": user, "password": TEST_USERS[user]})
            await alice.post("/api/sessions", json={"name": "a1", "preset": "GM-safe",
                                                    "card": "龙娘x猫娘", "beginning_index": 0})
            await alice.post("/api/sessions/a1/params", json={"temperature": 1.9})
            await bob.post("/api/sessions", json={"name": "b1", "preset": "GM-ptc",
                                                  "card": "龙娘x猫娘", "beginning_index": 0})
            r = await bob.post("/api/sessions", json={"name": "b2", "preset": "GM-ptc",
                                                      "card": "龙娘x猫娘", "beginning_index": 0})
            self.assertNotEqual(r.json()["temperature"], 1.9, "bob 的新会话继承了 alice 的参数")


class TurnFlowTest(AppTestCase):
    """单用户（本地模式，auth 关）下的完整一轮。"""

    async def test_start_and_chat(self):
        await self.new_session()
        code, events = await self.turn("/start")
        self.assertEqual(code, 200)
        kinds = [e["type"] for e in events]
        self.assertIn("content", kinds)
        self.assertIn("done", kinds)

        state = await self.get_session()
        self.assertEqual([e["role"] for e in state["history"]], ["assistant"])
        self.assertIn("假模型", state["history"][0]["content"])
        self.assertIsNone(state["generating"])

        await self.turn("/chat", {"input": "我向前走"})
        state = await self.get_session()
        self.assertEqual([e["role"] for e in state["history"]],
                         ["assistant", "user", "assistant"])
        self.assertEqual(state["history"][1]["content"], "我向前走")

    async def test_options_are_generated_and_stored(self):
        await self.new_session()
        r = await self.client.post("/api/sessions/s1/options", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        await self.turn("/start")
        entry = (await self.get_session())["history"][0]
        self.assertEqual(entry["options"], ["先去公会看看", "直接出城"])

    async def test_generation_failure_is_persisted(self):
        await self.new_session()
        self.llm.fail = True
        code, events = await self.turn("/start")
        self.assertEqual(code, 200)
        self.assertTrue([e for e in events if e["type"] == "error"])
        entry = (await self.get_session())["history"][0]
        self.assertIn("error", entry, "失败也要落盘（带 error 标记），不能丢半截")

    async def test_user_input_is_persisted_before_generation(self):
        """生成中途进程被杀也不能丢用户输入：开局时就写进 history。"""
        await self.new_session()
        await self.turn("/start")
        code, events = await self.turn("/chat", {"input": "这句不能丢"})
        self.assertEqual(code, 200)
        # 模拟「进程在生成中途被杀」：磁盘上只留一个没有回复的用户块
        history = core.load_history(self.key(None))
        self.write_history(None, history[:-1] + [{"role": "user", "content": "崩在半路的一句"}])
        state = await self.get_session()
        self.assertEqual(state["history"][-1]["role"], "user")
        self.assertIsNone(state["generating"])
        # 前端那个「继续」走的就是 regenerate
        await self.turn("/regenerate")
        state = await self.get_session()
        self.assertEqual([e["role"] for e in state["history"]][-2:], ["user", "assistant"])
        self.assertEqual(state["history"][-2]["content"], "崩在半路的一句")

    async def test_rollback_fork_delete(self):
        await self.new_session()
        await self.turn("/start")
        await self.turn("/chat", {"input": "往前走"})

        r = await self.client.post("/api/sessions/s1/rollback", json={"index": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["input"], "往前走")
        self.assertEqual(len((await self.get_session())["history"]), 1)

        r = await self.client.post("/api/sessions/s1/fork", json={"index": 0})
        self.assertEqual(r.status_code, 400, "0 号不是用户块")

        await self.turn("/regenerate")
        await self.turn("/chat", {"input": "再走一步"})
        r = await self.client.post("/api/sessions/s1/fork", json={"index": 1})
        self.assertEqual(r.status_code, 200, r.text)
        fork_id = r.json()["id"]
        self.assertEqual(r.json()["owner"], None)
        self.assertEqual((await self.client.get(f"/api/sessions/{fork_id}")).status_code, 200)
        self.assertEqual(len((await self.client.get("/api/sessions")).json()), 2)

        self.assertEqual((await self.client.delete(f"/api/sessions/{fork_id}")).status_code, 200)
        self.assertEqual((await self.client.delete("/api/sessions/s1")).status_code, 200)
        self.assertEqual((await self.client.get("/api/sessions/s1")).status_code, 404)

    async def test_world_run_tool_call(self):
        await self.new_session()
        self.llm.tool_programs = ["state['hp'] = 7\nprint('开局血量', state['hp'])"]
        code, events = await self.turn("/start")
        self.assertEqual(code, 200)
        tools_used = [e for e in events if e["type"] == "tool"]
        self.assertEqual(len(tools_used), 1)
        self.assertIn("hp", tools_used[0]["result"])

        entry = (await self.get_session())["history"][0]
        self.assertEqual(entry["tool_calls"][0]["name"], "world_run")
        # 世界状态落盘了
        self.assertEqual(core.load_state(self.key(None))["chat_id"],
                         (await self.get_session())["chat_id"])
        from app import world
        self.assertEqual(world._runtimes[self.key(None)]["ns"]["state"]["hp"], 7)


if __name__ == "__main__":
    unittest.main()
