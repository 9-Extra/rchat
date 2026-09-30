"""打断 world_run 时的收尾：线程杀不掉，取消必须先等它跑完再抛出。

不等会怎样（旧写法：裸 `asyncio.to_thread`）：
  - 取消立刻返回（实测 0.00s），线程还在改 `state`；
  - 与此同时 server 的取消处理器在跑 `world.abort_turn` / `world.commit_turn`，
    它们要 `json.dumps(rt["ns"]["state"])` —— 撞上就是
    `RuntimeError: dictionary changed size during iteration`，或者被取消那一轮定义的
    函数留进 lib、世界状态与历史对不上。
现在的实现（tools.WORLD_RUN_CANCEL_GRACE 秒内等它收尾）：
  - 取消返回时线程已经结束，state 不再变化；
  - 紧接着的 abort/commit 是安全的。
"""
import asyncio
import json
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

import tests.support  # noqa: F401

from app import core, tools, world
from tests.support import program_for

KEY = "alice/cancelcheck"


def make_session():
    shutil.rmtree(core._session_dir(KEY), ignore_errors=True)
    core._session_dir(KEY).mkdir(parents=True)
    core.save_state({
        "id": "cancelcheck", "name": "cancelcheck", "owner": "alice", "preset": "GM-safe",
        "card": "龙娘x猫娘", "card_name": "x", "beginning_index": None, "beginning_text": "",
        "created_at": 1.0, "chat_id": "c", "endpoint": 0, "temperature": 0.7,
        "max_tokens": 1000, "reasoning_effort": "low", "options_enabled": False,
    })
    core.save_history(KEY, [])


class WorldCancelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp_orig = core.SESSIONS_DIR
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-cancel-")
        core.SESSIONS_DIR = pathlib.Path(self._tmp.name)
        world._runtimes.clear()

    def tearDown(self):
        core.SESSIONS_DIR = self._tmp_orig
        world._runtimes.clear()
        self._tmp.cleanup()

    async def _measure(self, start):
        """返回 (取消返回耗时, 取消那一刻的 ticks, 再等 0.3s 后的 ticks)。"""
        make_session()
        world.drop(KEY)
        task = start()
        await asyncio.sleep(0.3)          # 让它跑到中途
        t0 = time.time()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        elapsed = time.time() - t0

        rt = world.runtime_for(KEY)
        at_cancel = rt["ns"]["state"].get("ticks")
        await asyncio.sleep(0.3)
        after = rt["ns"]["state"].get("ticks")
        return elapsed, at_cancel, after

    async def test_cancel_waits_for_the_thread(self):
        program = program_for(ticks=30, sleep=0.05)   # 约 1.5 秒

        elapsed, at_cancel, after = await self._measure(
            lambda: asyncio.ensure_future(
                tools.execute_tool(KEY, "world_run", json.dumps({"program": program}))
            )
        )

        self.assertEqual(after, at_cancel,
                         "取消返回后线程还在改 state —— 取消处理器会和它抢")
        self.assertGreaterEqual(elapsed, 0.5,
                                "取消立刻返回了，说明没等线程收尾")
        # 收尾（真实路径就是这两个调用）必须安全
        world.abort_turn(KEY)
        world.commit_turn(KEY, 1)

    async def test_old_raw_to_thread_still_races(self):
        """守住认知：裸 to_thread 的写法取消后线程仍在跑（所以必须 shield + 等待）。"""
        program = program_for(ticks=30, sleep=0.05)
        _, at_cancel, after = await self._measure(
            lambda: asyncio.ensure_future(asyncio.to_thread(world.run, KEY, program))
        )
        if after == at_cancel:
            self.skipTest("这次没撞上（调度相关），失败模式见文件头注释")
        self.assertNotEqual(after, at_cancel)

    async def test_cancel_gives_up_after_grace(self):
        """死循环一样的程序：超过宽限要放行，而不是把打断挂死。

        这里故意用「只睡觉、不碰 state」的程序：宽限用完我们就抛取消了，线程还会在后台
        跑完这段时间，不碰 state 就不会和别的用例互相干扰。
        """
        grace = tools.WORLD_RUN_CANCEL_GRACE
        tools.WORLD_RUN_CANCEL_GRACE = 0.3
        self.addCleanup(setattr, tools, "WORLD_RUN_CANCEL_GRACE", grace)
        program = "import time\nfor _ in range(40):\n    time.sleep(0.05)"   # 约 2 秒

        elapsed, _, _ = await self._measure(
            lambda: asyncio.ensure_future(
                tools.execute_tool(KEY, "world_run", json.dumps({"program": program}))
            )
        )
        self.assertLess(elapsed, 1.5, "超过宽限还没放行，打断被挂住了")


if __name__ == "__main__":
    unittest.main()
