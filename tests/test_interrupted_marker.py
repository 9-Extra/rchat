"""打断标记：落盘写 interrupted，回放不进模型上下文。"""
import json

from tests.support import AppTestCase
from app import core, server


class InterruptMarkerTest(AppTestCase):
    async def test_persist_writes_interrupted_phase(self):
        await self.new_session("s1")
        key = self.key(None, "s1")
        self.write_history(None, [{"role": "user", "content": "上一句"}])

        server._persist(
            key, core.load_history(key), "chat", "一句", None,
            "正文被打断", [], "", [], interrupted="body",
        )
        entry = core.load_history(key)[-1]
        self.assertEqual(entry["interrupted"], "body")
        self.assertNotIn("error", entry)

    async def test_interrupted_not_in_model_context(self):
        await self.new_session("s1")
        key = self.key(None, "s1")
        history = [
            {"role": "user", "content": "上一句"},
            {"role": "assistant", "content": "被打断的半截", "interrupted": "body"},
        ]
        self.write_history(None, history)

        state = core.load_state(key)
        turn = core.build_turn(state)
        items = core.build_input(state, history[:-1], "下一句", turn)
        texts = [json.dumps(x, ensure_ascii=False) for x in items]
        joined = "\n".join(texts)
        self.assertNotIn("interrupted", joined)
        self.assertNotIn("被打断的半截", joined)

    async def test_options_phase_marker(self):
        await self.new_session("s1")
        key = self.key(None, "s1")
        self.write_history(None, [{"role": "user", "content": "上一句"}])
        server._persist(
            key, core.load_history(key), "chat", "一句", None,
            "正文是完整的", [], "", [], interrupted="options",
        )
        self.assertEqual(core.load_history(key)[-1]["interrupted"], "options")


if __name__ == "__main__":
    import unittest
    unittest.main()
