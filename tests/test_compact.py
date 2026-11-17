"""上下文压缩（/compact）：切点、摘要块、旧历史副本、world 快照平移。

最要紧的一条回归是**快照平移**：world 的快照 key 是「落盘时的历史长度」，压缩把长度
改短后如果不平移，下一轮 `_prepare` 的 `world.sync(name, len(history))` 会查到一条很早
的快照，把世界状态静默回滚到开局（HP、道具、关系全没），再下一轮 `commit_turn` 的
prune 还会把之后的快照全删掉。这里直接跑三段 world_run 验证状态没有被退回去。

全部离线：假的 stream_body + 假的 generate_summary，不连任何模型端点。
"""
import asyncio
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core, llm, server, world
from tests.support import AppTestCase, make_settings

SUMMARY = (
    "（摘要）主角在灰港的酒馆遇到了一名自称商人的龙娘，两人约定次日同行去北境；"
    "途中遇到山贼，主角负伤，龙娘用随身的药膏处理了伤口。"
)


async def fake_generate_summary(input_items, turn):
    """假的压缩请求：固定给一段摘要，不走真模型。"""
    await asyncio.sleep(0)
    return SUMMARY, ""


class CompactCutTest(unittest.TestCase):
    """切点计算：纯函数，先把边界钉住。"""

    def test_cut_keeps_whole_turns(self):
        h = [{"role": "user"}, {"role": "assistant"}] * 3
        # 保留最近 1 轮 → 前两轮（4 条）被压掉
        self.assertEqual(core.compact_cut(h, 1), 4)
        self.assertEqual(core.compact_cut(h, 2), 2)
        # 0 轮 = 全压
        self.assertEqual(core.compact_cut(h, 0), 6)
        # 要保留的比现有的还多：没有可压的
        self.assertEqual(core.compact_cut(h, 9), 0)

    def test_cut_with_opening_block(self):
        # 「开始」生成的 assistant 块前面没有 user 块，切点不能落到它身上
        h = [{"role": "assistant"}, {"role": "user"}, {"role": "assistant"}]
        self.assertEqual(core.compact_cut(h, 1), 1)
        self.assertEqual(core.compact_cut(h, 0), 3)
        self.assertEqual(core.compact_cut([], 1), 0)


class ShortSummaryTest(unittest.IsolatedAsyncioTestCase):
    """摘要不设长度下限：一句话也是摘要，不该当作失败让用户重来。"""

    async def test_tiny_summary_is_accepted(self):
        calls = []

        class FakeClient:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        async def fake_once(client, items, turn):
            calls.append(items)
            return "两人继续赶路。", ""

        with mock.patch.object(llm, "_make_client", lambda turn: FakeClient()), \
                mock.patch.object(llm, "_request_compact_once", fake_once):
            text, error = await llm.generate_summary([], None)

        self.assertEqual(error, "")
        self.assertEqual(text, "两人继续赶路。")
        self.assertEqual(len(calls), 1, "短摘要被当失败重试了")


class CompactAppTest(AppTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        patch = mock.patch.object(server, "generate_summary", fake_generate_summary)
        self.addCleanup(patch.stop)
        patch.start()

    # ---------- 小工具 ----------

    async def _prepare(self, turns=2):
        await self.new_session()
        await self.turn("/start")
        for i in range(turns):
            await self.turn("/chat", {"input": f"行动{i}"})

    async def _set_keep(self, turns: int):
        r = await self.client.post("/api/sessions/s1/params", json={"compact_keep_turns": turns})
        self.assertEqual(r.status_code, 200, r.text)

    def _state_on_disk(self):
        path = core._session_dir(self.key(None)) / "world" / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    # ---------- 用例 ----------

    async def test_compact_rewrites_history_and_keeps_a_copy(self):
        await self._prepare(turns=2)
        self.assertEqual(len((await self.get_session())["history"]), 5)
        await self._set_keep(1)

        code, events = await self.turn("/compact", {})
        self.assertEqual(code, 200)
        self.assertIn("compacted", [e["type"] for e in events])

        history = (await self.get_session())["history"]
        # [摘要] + 保留的最后一轮
        self.assertEqual([e["role"] for e in history], ["assistant", "user", "assistant"])
        summary = history[0]
        self.assertTrue(summary.get("compacted"))
        self.assertIn("【前情摘要】", summary["content"])
        self.assertIn(SUMMARY, summary["content"])
        # world 状态与函数源码跟在摘要后面（没跑过 world 就是一句说明，不能是空 dict）
        self.assertIn("【本轮state快照】", summary["content"])
        self.assertIn("尚未使用 world_run", summary["content"])

        # 压缩前的完整历史留成一份普通会话副本，压缩前的块一条都没少
        archived = await self.get_session(summary["archive"])
        self.assertEqual(
            [e["role"] for e in archived["history"]],
            ["assistant", "user", "assistant", "user", "assistant"],
        )

        # 摘要块不算一轮
        self.assertEqual(core.count_turns(self.key(None)), 1)

    async def test_world_state_survives_compaction(self):
        """快照平移的回归：压缩后 state 不能被 sync 退回开局。"""
        await self.new_session()
        self.llm.tool_programs = ["state['hp'] = 100", "state['hp'] = 80", "state['hp'] = 55"]
        await self.turn("/start")
        await self.turn("/chat", {"input": "打一场"})
        await self.turn("/chat", {"input": "再打一场"})
        self.assertEqual(self._state_on_disk()["hp"], 55)
        await self._set_keep(1)

        await self.turn("/compact", {})
        # 压缩本身不动 state，而且 state 全量被附在摘要后面（模型压缩后还知道账本）
        self.assertEqual(self._state_on_disk()["hp"], 55)
        summary = (await self.get_session())["history"][0]
        self.assertIn("【本轮state快照】", summary["content"])
        self.assertIn('"hp": 55', summary["content"])
        # 历史 [A,U,A,U,A] 保留最后 1 轮 → 压掉前 3 条，新历史 [摘要,U,A] 共 3 条；
        # 老快照 1/3/5 平移成 1/3（5 = 最后一条，落在新长度上）
        snaps = world._snapshots(self.key(None))
        self.assertEqual(sorted(snaps, key=int), ["1", "3"],
                         f"快照没平移到新坐标系：{sorted(snaps, key=int)}")

        # 再跑一轮：_prepare 会 world.sync(新长度)，状态必须还是 55 而不是退回 80/100
        self.llm.tool_programs = ["state['hp'] -= 5"]
        await self.turn("/chat", {"input": "再砍一刀"})
        self.assertEqual(self._state_on_disk()["hp"], 50)

    async def test_summary_block_cannot_be_regenerated(self):
        """keep=0 时历史只剩摘要块，点重掷会把整段前情删掉并从空历史重开。"""
        await self._prepare(turns=2)
        await self._set_keep(0)
        await self.turn("/compact", {})

        history = (await self.get_session())["history"]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].get("compacted"))

        code, events = await self.turn("/regenerate")
        self.assertTrue(any(e["type"] == "error" for e in events), events)
        # 历史原样保留，没有被 pop 掉
        history = (await self.get_session())["history"]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].get("compacted"))

    async def test_compact_refused_when_round_is_unfinished(self):
        """末尾是用户块 = 上一轮没写出来，这时压缩会把玩家刚打的字一起吞掉。"""
        await self._prepare(turns=1)
        key = self.key(None)
        history = core.load_history(key)
        core.save_history(key, history + [{"role": "user", "content": "还没回我"}])

        code, events = await self.turn("/compact", {})
        self.assertTrue(any(e["type"] == "error" for e in events), events)
        self.assertEqual([e["role"] for e in core.load_history(key)][-1], "user")

    async def test_existing_options_are_carried_over(self):
        """压缩前那一轮已经有选项：摘要块沿用它，不再花钱重问一次。"""
        self.settings = make_settings(options_enabled=True)
        calls = []

        async def counting(options_input, turn):
            calls.append(1)
            return [f"第{len(calls)}次"], None, ""

        with mock.patch.object(server, "generate_options", counting):
            await self._prepare(turns=1)   # /start 与 /chat 各问一次选项
            before = len(calls)
            self.assertEqual(before, 2)
            await self._set_keep(0)
            code, events = await self.turn("/compact", {})
            self.assertEqual(code, 200, events)
            self.assertEqual(len(calls), before, "压缩时不该再问一次选项")

        history = (await self.get_session())["history"]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].get("compacted"))
        self.assertEqual(history[0]["options"], [f"第{before}次"])

        # 摘要块作为最后一轮，支持只重掷选项（正文不动）
        code, events = await self.turn("/regenerate_options")
        self.assertEqual(code, 200, events)
        self.assertIn("options", [e["type"] for e in events])

    async def test_options_are_requested_when_the_last_turn_had_none(self):
        """压缩前没生成过选项（或生成失败）：摘要块得补问一次，否则只能打字。"""
        await self._prepare(turns=1)   # 默认不开选项 → 两个块都没有选项
        self.assertEqual((await self.get_session())["history"][-1]["options"], [])

        # 打开选项开关（会话级），再压缩：这一步必须补问，不能把「没有选项」直接搬过去
        r = await self.client.post("/api/sessions/s1/options", json={"enabled": True})
        self.assertEqual(r.status_code, 200, r.text)
        await self._set_keep(0)
        code, events = await self.turn("/compact", {})
        self.assertEqual(code, 200, events)
        history = (await self.get_session())["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["options"], ["先去公会看看", "直接出城"])

    async def test_options_stay_on_the_kept_tail(self):
        """保留原文时末块还是原来那一轮，别拿摘要块去顶掉它的选项。"""
        self.settings = make_settings(options_enabled=True)
        await self._prepare(turns=2)
        await self._set_keep(1)

        await self.turn("/compact", {})
        history = (await self.get_session())["history"]
        self.assertEqual([e["role"] for e in history], ["assistant", "user", "assistant"])
        self.assertEqual(history[0]["options"], [])
        self.assertEqual(history[-1]["options"], ["先去公会看看", "直接出城"])

    async def test_fork_from_user_block_only_for_users(self):
        """core 支持任意位置 fork，但面向用户的入口仍然只认用户块。"""
        await self._prepare(turns=1)
        # index 0 是「开始」生成的 assistant 块
        r = await self.client.post("/api/sessions/s1/fork", json={"index": 0})
        self.assertEqual(r.status_code, 400, r.text)
        r = await self.client.post("/api/sessions/s1/fork", json={"index": 1})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
