"""预设可定义的选项指令：<preset_options> 的解析、渲染与拼装。

不连模型、不碰真 sessions：`load_presets` 指向临时目录，`build_options_input` 用假的
preset/card/config。重点守三件事：

1. `<preset_options>` 被提取成 `options_instruction`，且不会混进正文 section；
2. 宏按本会话的角色卡渲染，块缺失时回退默认指令；
3. 空块/宏错误直接抛 ValueError（选项阶段由 server 降级成 options_error）。
"""
import pathlib
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core


class PresetOptionsInstructionTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-options-")
        self.addCleanup(self._tmp.cleanup)
        self.preset_dir = pathlib.Path(self._tmp.name) / "preset"
        self.preset_dir.mkdir()
        patch = mock.patch.object(core, "PRESET_DIR", self.preset_dir)
        patch.start()
        self.addCleanup(patch.stop)
        core._CACHE.pop("presets", None)
        self.addCleanup(core._CACHE.pop, "presets", None)

    def write_preset(self, stem: str, body: str) -> None:
        (self.preset_dir / f"{stem}.md").write_text(
            f"---\nname: {stem}\n---\n{body}", encoding="utf-8"
        )

    def test_load_presets_extracts_options_block(self):
        self.write_preset(
            "with-options",
            """\
<preset_section role="system">
系统正文
</preset_section>

<preset_options>
为 {{user_setting}} 生成选项
- 甲
- 乙
</preset_options>
""",
        )
        preset = core.load_presets()["with-options"]
        # 块本身不进入正文 section，正文照旧只有 preset_section 的内容
        self.assertEqual([s["content"] for s in preset["sections"]], ["系统正文"])
        self.assertEqual(
            preset["options_instruction"],
            "为 {{user_setting}} 生成选项\n- 甲\n- 乙",
        )
        self.assertEqual(
            core.options_instruction(preset, {"user_setting": "勇者"}),
            "为 勇者 生成选项\n- 甲\n- 乙",
        )

    def test_missing_block_falls_back_to_default(self):
        self.write_preset("plain", '<preset_section role="system">系统</preset_section>\n')
        preset = core.load_presets()["plain"]
        self.assertIsNone(preset["options_instruction"])
        self.assertEqual(core.options_instruction(preset, {}), core.OPTIONS_INSTRUCTION)

    def test_empty_block_raises(self):
        self.write_preset("empty", "<preset_options>\n  \n</preset_options>\n")
        self.write_preset("only-comment", "<preset_options>{{# 空块}}</preset_options>\n")
        for stem in ("empty", "only-comment"):
            with self.subTest(stem=stem):
                preset = core.load_presets()[stem]
                with self.assertRaises(ValueError) as ctx:
                    core.options_instruction(preset, {})
                self.assertIn("<preset_options>", str(ctx.exception))
                self.assertIn(stem, str(ctx.exception))

    def test_macro_error_raises_with_preset_context(self):
        self.write_preset("broken", "<preset_options>{{不存在的变量}}</preset_options>\n")
        preset = core.load_presets()["broken"]
        with self.assertRaises(ValueError) as ctx:
            core.options_instruction(preset, {})
        self.assertIn("broken", str(ctx.exception))
        self.assertIn("<preset_options>", str(ctx.exception))
        self.assertIn("不存在的变量", str(ctx.exception))

    def test_build_options_input_appends_rendered_instruction(self):
        """两种 api_type 下，最后一条都是预设块渲染出的 user 消息。"""
        preset = {
            "id": "p",
            "sections": [],
            "options_instruction": "请给 {{user_setting}} 选项\n- 甲\n- 乙",
        }
        card = {"id": "c", "setting": "世界", "user_setting": "勇者", "beginnings": []}
        state = {"preset": "p", "card": "c", "beginning_text": ""}
        expected = "请给 勇者 选项\n- 甲\n- 乙"
        for api_type in ("chat_completions", "responses"):
            with self.subTest(api_type=api_type):
                with (
                    mock.patch.object(core, "load_presets", return_value={"p": preset}),
                    mock.patch.object(core, "load_cards", return_value={"c": card}),
                ):
                    items = core.build_options_input(
                        state, [], "正文", [], "思考", tests.support.make_turn(api_type)
                    )
                self.assertEqual(items[-2]["role"], "assistant")
                if api_type == "chat_completions":
                    self.assertEqual(items[-1], {"role": "user", "content": expected})
                else:
                    self.assertEqual(
                        items[-1],
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": expected}],
                        },
                    )

    def test_build_options_input_uses_default_when_missing(self):
        preset = {"id": "p", "sections": []}  # 没有 options_instruction
        card = {"id": "c", "setting": "世界", "user_setting": "勇者", "beginnings": []}
        state = {"preset": "p", "card": "c", "beginning_text": ""}
        with (
            mock.patch.object(core, "load_presets", return_value={"p": preset}),
            mock.patch.object(core, "load_cards", return_value={"c": card}),
        ):
            items = core.build_options_input(
                state, [], "正文", [], "思考", tests.support.make_turn("chat_completions")
            )
        self.assertEqual(items[-1], {"role": "user", "content": core.OPTIONS_INSTRUCTION})


if __name__ == "__main__":
    unittest.main()
