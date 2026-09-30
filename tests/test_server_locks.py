"""会话锁：改会话的端点与「开始一轮生成」必须互斥。

原来塌在哪：`_busy()` 跑在工作线程池里（同步端点），`_active[key]` 在事件循环里写，
两者之间有个毫秒级窗口 —— 撞上时回滚刚截断的 history 会被那一轮的 `save_history`
用内存里的旧副本覆盖回去（表现：点了回滚，历史又回来了）。

现在：这些端点都是 `async def`，和 `_generate` 共用 `server._lock_for(会话键)`，
并且 `_generate` 在锁里完成「查 `_active` + `_prepare` + 注册」。

两种验证：
1) 结构：全都持锁、全是 async、锁体里没有 await（有 await 等于没锁）；
2) 行为：并发跑「生成 + 回滚」，断言不变量 —— 回滚成功过的那一轮，历史不能超过截断点。
"""
import ast
import asyncio
import inspect
import pathlib
import unittest

import tests.support  # noqa: F401

from app import server
from tests.support import AppTestCase

MUTATORS = [
    "delete_session", "switch_preset", "set_reasoning_effort", "set_endpoint", "set_params",
    "set_options", "edit_beginning", "edit_ai", "rollback", "fork",
]


class LockStructureTest(unittest.TestCase):
    """静态检查：锁用得对不对。"""

    def setUp(self):
        self.tree = ast.parse(pathlib.Path(server.__file__).read_text(encoding="utf-8"))

    def test_all_mutators_are_async_and_locked(self):
        locked, problems = set(), []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.AsyncWith) or not inner.items:
                    continue
                call = inner.items[0].context_expr
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_lock_for"):
                    continue
                locked.add(node.name)
                if not isinstance(node, ast.AsyncFunctionDef):
                    problems.append(f"{node.name} 用了会话锁却不是 async def")
                for sub in ast.walk(inner):
                    if sub is inner:
                        continue
                    if isinstance(sub, (ast.Await, ast.AsyncFor)):
                        problems.append(f"{node.name} 锁体内有 {type(sub).__name__}（持锁时让出控制权）")
                    if isinstance(sub, ast.AsyncWith):
                        problems.append(f"{node.name} 锁体内嵌套 async with")

        self.assertEqual(problems, [])
        self.assertIn("_generate", locked, "_generate 没把注册放进锁里")
        for name in MUTATORS:
            self.assertIn(name, locked, f"{name} 没持会话锁")
            self.assertTrue(inspect.iscoroutinefunction(getattr(server, name)),
                            f"{name} 还是同步 def，会跑在工作线程池里、绕过锁")


class RaceInvariantTest(AppTestCase):
    """行为检查：生成与回滚同时发，不变量不能被打破。"""

    async def _prepare_history(self):
        await self.new_session()
        await self.turn("/start")
        await self.turn("/chat", {"input": "往前走"})
        state = await self.get_session()
        self.assertEqual([e["role"] for e in state["history"]],
                         ["assistant", "user", "assistant"])

    async def test_rollback_survives_a_concurrent_turn(self):
        await self._prepare_history()

        for round_no in range(4):
            gen = asyncio.ensure_future(self.client.post("/api/sessions/s1/regenerate"))
            await asyncio.sleep(0)          # 让两边都有机会先拿到锁
            rb = await self.client.post("/api/sessions/s1/rollback", json={"index": 1})
            await gen

            self.assertIn(rb.status_code, (200, 409), rb.text)
            if rb.status_code == 200:
                history = (await self.get_session())["history"]
                self.assertLessEqual(
                    len(history), 2,
                    f"第 {round_no + 1} 轮：回滚成功过，历史却被那一轮覆盖回来了："
                    f"{[e['role'] for e in history]}",
                )
            # 复位成 [ai,user,ai]，让下一轮 index=1 仍是用户块
            state = await self.get_session()
            if not state["history"] or state["history"][-1]["role"] != "assistant":
                await self.turn("/regenerate")
            if len((await self.get_session())["history"]) < 3:
                await self.turn("/chat", {"input": "补一句"})

    async def test_mutations_are_rejected_while_generating(self):
        await self.new_session()
        # 让假模型慢一点，好在生成期间插进去改会话
        self.llm.delay = 0.15
        self.llm.chunks = ["一", "二", "三", "四", "五", "六"]

        gen = asyncio.ensure_future(self.client.post("/api/sessions/s1/start"))
        await asyncio.sleep(0.1)
        try:
            r = await self.client.post("/api/sessions/s1/params", json={"temperature": 1.2})
            self.assertEqual(r.status_code, 409, "生成期间的改参数请求应当被 409 挡住")
            r = await self.client.delete("/api/sessions/s1")
            self.assertEqual(r.status_code, 409, "生成期间不能删会话")
        finally:
            await gen


if __name__ == "__main__":
    unittest.main()
