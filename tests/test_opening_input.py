"""开场输入（玩家的第一句，也就是开局）进预设 section 的端到端行为。

卡片里的 `<game_beginning>` 只是前端「选择开局」的候选，不再是宏：开局通过
`state.opening_input` 落到 `{{user_input}}`。这里守四件事：

1. 空历史 /chat 能发，首句只在 section 里出现一次（不额外追加 user 消息）；
2. 次轮、选项请求用的 section 与首轮逐字一致（前缀缓存）；
3. 重掷首轮 / 改开场输入后，下一次请求带的是 state 里的最新值；
4. 预设没写 `{{user_input}}` 时静默忽略，但 state 里仍保留（玩家可见可改）。
"""
import copy
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core, server
from tests.support import TEST_CONFIG, AppTestCase, FakeLLM, make_settings


PRESET_WITH_INPUT = """\
---
name: opening
---
<preset_section role="system">
你是测试主持。
</preset_section>

<preset_section role="user">
<start_scene>{{user_input}}</start_scene>
</preset_section>
"""

PRESET_WITHOUT_INPUT = """\
---
name: plain
---
<preset_section role="system">
你是测试主持。
</preset_section>

<preset_section role="user">
<start_scene>固定场景</start_scene>
</preset_section>
"""


class RecordingLLM(FakeLLM):
    """记录每次正文请求的 input_items（选项请求由单独的打桩接住）。"""

    def __init__(self):
        super().__init__()
        self.inputs = []

    async def __call__(self, input_items, turn, run_tool):
        self.inputs.append(copy.deepcopy(input_items))
        async for event in super().__call__(input_items, turn, run_tool):
            yield event


class OpeningInputTest(AppTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        tmp = tempfile.TemporaryDirectory(prefix="rchat-opening-")
        self.addCleanup(tmp.cleanup)
        preset_dir = pathlib.Path(tmp.name)
        (preset_dir / "opening.md").write_text(PRESET_WITH_INPUT, encoding="utf-8")
        (preset_dir / "plain.md").write_text(PRESET_WITHOUT_INPUT, encoding="utf-8")
        patch = mock.patch.object(core, "PRESET_DIR", preset_dir)
        patch.start()
        self.addCleanup(patch.stop)
        core._CACHE.pop("presets", None)
        self.addCleanup(core._CACHE.pop, "presets", None)

        self.rec = RecordingLLM()
        stream_patch = mock.patch.object(server, "stream_body", self.rec)
        stream_patch.start()
        self.addCleanup(stream_patch.stop)

    def section_content(self, items, role="user"):
        return next(m["content"] for m in items if m["role"] == role)

    async def test_first_message_goes_into_preset_section(self):
        await self.new_session(preset="opening")
        code, events = await self.turn("/chat", {"input": "我想当女骑士"})
        self.assertEqual(code, 200)
        self.assertIn("saved", [e["type"] for e in events])

        state = await self.get_session()
        self.assertEqual(state["opening_input"], "我想当女骑士")
        self.assertEqual([e["role"] for e in state["history"]], ["assistant"],
                         "首句不能额外落成一条 user 块")

        self.assertEqual(len(self.rec.inputs), 1)
        items = self.rec.inputs[0]
        self.assertEqual([m["role"] for m in items], ["system", "user"])
        self.assertIn("<start_scene>我想当女骑士</start_scene>", self.section_content(items))

    async def test_second_turn_replays_history_after_preset(self):
        await self.new_session(preset="opening")
        await self.turn("/chat", {"input": "第一句"})
        await self.turn("/chat", {"input": "第二句"})

        items = self.rec.inputs[-1]
        self.assertEqual([m["role"] for m in items],
                         ["system", "user", "assistant", "user"])
        self.assertIn("<start_scene>第一句</start_scene>", self.section_content(items))
        self.assertEqual(items[3]["content"], "第二句")
        self.assertEqual([e["role"] for e in (await self.get_session())["history"]],
                         ["assistant", "user", "assistant"])

    async def test_start_without_first_message_renders_empty(self):
        await self.new_session(preset="opening")
        await self.turn("/start")
        state = await self.get_session()
        self.assertEqual(state["opening_input"], "")
        self.assertIn("<start_scene></start_scene>", self.section_content(self.rec.inputs[0]),
                      "没有首句时宏应该渲染成空串，而不是报错")

    async def test_regenerate_first_turn_reuses_opening(self):
        await self.new_session(preset="opening")
        await self.turn("/chat", {"input": "我想当女骑士"})
        await self.turn("/regenerate")

        self.assertIn("<start_scene>我想当女骑士</start_scene>",
                      self.section_content(self.rec.inputs[-1]))
        self.assertEqual([e["role"] for e in (await self.get_session())["history"]],
                         ["assistant"])

    async def test_first_message_in_responses_api(self):
        """两种 api_type 的 section 形状不同，但首句都只出现在 section 里。"""
        self.settings = make_settings(
            endpoints=[{**TEST_CONFIG["endpoints"][0], "api_type": "responses"}]
        )
        await self.new_session(preset="opening")
        await self.turn("/chat", {"input": "第一句"})

        items = self.rec.inputs[0]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["type"], "message")
        self.assertEqual(items[1]["role"], "user")
        self.assertIn("<start_scene>第一句</start_scene>", items[1]["content"][0]["text"])
        self.assertEqual([e["role"] for e in (await self.get_session())["history"]],
                         ["assistant"])

    async def test_options_request_is_strict_continuation(self):
        await self.new_session(preset="opening")
        r = await self.client.post("/api/sessions/s1/options", json={"enabled": True})
        self.assertEqual(r.status_code, 200, r.text)

        seen = []

        async def fake_options(options_input, turn):
            seen.append(copy.deepcopy(options_input))
            return ["选项一"], None, ""

        with mock.patch.object(server, "generate_options", fake_options):
            await self.turn("/chat", {"input": "第一句"})

        body = self.rec.inputs[0]
        self.assertEqual(len(seen), 1)
        # 选项请求必须是正文请求的严格续写：section 与 assistant 前缀逐字一致
        self.assertEqual(seen[0][:len(body)], body)
        self.assertEqual([m["role"] for m in seen[0]][len(body):], ["assistant", "user"])

    async def test_preview_first_input_stays_in_section(self):
        await self.new_session(preset="opening")
        r = await self.client.post(
            "/api/preview", json={"session": "s1", "input": "预览的首句"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["messages"]
        self.assertEqual([m["role"] for m in items], ["system", "user"])
        self.assertIn("<start_scene>预览的首句</start_scene>", self.section_content(items))
        self.assertEqual((await self.get_session())["opening_input"], "",
                         "预览不能落盘")

    async def test_edit_opening_input_changes_next_request(self):
        await self.new_session(preset="opening")
        await self.turn("/chat", {"input": "旧设定"})
        r = await self.client.post(
            "/api/sessions/s1/opening_input", json={"text": "新设定"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["opening_input"], "新设定")

        await self.turn("/regenerate")
        self.assertIn("<start_scene>新设定</start_scene>",
                      self.section_content(self.rec.inputs[-1]))

    async def test_preset_without_macro_ignores_but_keeps_input(self):
        await self.new_session(preset="plain")
        code, events = await self.turn("/chat", {"input": "这句不进上下文"})
        self.assertEqual(code, 200)
        self.assertIn("saved", [e["type"] for e in events])
        self.assertNotIn("这句不进上下文",
                         json.dumps(self.rec.inputs[0], ensure_ascii=False))
        # 静默忽略 ≠ 丢掉：玩家还能在界面上看到并改它
        state = await self.get_session()
        self.assertEqual(state["opening_input"], "这句不进上下文")
        self.assertEqual([e["role"] for e in state["history"]], ["assistant"])

    async def test_empty_first_message_is_rejected(self):
        await self.new_session(preset="opening")
        code, events = await self.turn("/chat", {"input": "   "})
        self.assertEqual(code, 200)
        self.assertTrue([e for e in events if e["type"] == "error"])
        state = await self.get_session()
        self.assertEqual(state["history"], [])
        self.assertEqual(state["opening_input"], "")

    async def test_unstarted_session_exposes_full_card_beginnings(self):
        await self.new_session(preset="opening")
        state = await self.get_session()
        card = core.load_cards()[state["card"]]
        self.assertEqual(state["card_beginnings"], card["beginnings"])
        self.assertGreater(len(state["card_beginnings"][0]), 60,
                           "开局候选必须是全文，不能像旧下拉框那样截断")

        await self.turn("/chat", {"input": "第一句"})
        self.assertNotIn("card_beginnings", await self.get_session(),
                         "已经开始（或已有开场输入）就不该再传开局候选")


if __name__ == "__main__":
    unittest.main()
