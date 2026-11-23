"""会话增量同步：saved 自带落盘块 + GET ?history_from=N。

前端一轮结束不再整份拉 history，而是拿 saved.entry 就地定稿；只有重连/打断这类
「本地缺块」的场合才按 history_from 补尾部。这里守住服务端契约：

1. 完整快照永远带 history_total / history_from（0 = 全量），越界请求退化成全量；
2. history_from=N 只返回 history[N:]，元信息照常给（重连还要靠它判断 generating）；
3. saved.entry 就是刚落盘的那一块，时间上加在 done 之后；
4. chat 的 saved 带上本轮新写的 user 块（前端本地 history 里还没有它），
   regenerate 的 saved 是原地替换（user 为 None）——前端按 index 决定补还是换。
"""
import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from tests.support import AppTestCase


class SessionSyncTest(AppTestCase):
    async def test_full_snapshot_marks_full_range(self):
        await self.new_session()
        s = await self.get_session()
        self.assertEqual(s["history"], [])
        self.assertEqual(s["history_from"], 0)
        self.assertEqual(s["history_total"], 0)

        await self.turn("/start")
        s = await self.get_session()
        self.assertEqual(s["history_total"], 1)
        self.assertEqual(s["history_from"], 0, "不带参数时必须是整份")

    async def test_history_from_returns_only_missing_tail(self):
        await self.new_session()
        await self.turn("/start")
        await self.turn("/chat", {"input": "第二句"})
        full = await self.get_session()
        self.assertEqual(full["history_total"], 3)

        r = await self.client.get("/api/sessions/s1?history_from=2")
        self.assertEqual(r.status_code, 200, r.text)
        part = r.json()
        self.assertEqual(part["history_from"], 2)
        self.assertEqual(part["history_total"], 3)
        self.assertEqual(part["history"], full["history"][2:])
        # 重连还要靠这些元信息决定「接不接得上」和怎么画 live 块
        self.assertIn("generating", part)
        self.assertIn("preset", part)

        # N == 总长度：空增量，但仍是合法切片（前端据此只更新元信息）
        r = await self.client.get(f"/api/sessions/s1?history_from={full['history_total']}")
        self.assertEqual(r.json()["history"], [])
        self.assertEqual(r.json()["history_from"], 3)

    async def test_history_from_out_of_range_falls_back_to_full(self):
        await self.new_session()
        await self.turn("/start")
        await self.turn("/chat", {"input": "第二句"})
        full = await self.get_session()

        for bad in ("999", "-5"):
            r = await self.client.get(f"/api/sessions/s1?history_from={bad}")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["history_from"], 0, f"history_from={bad} 应为全量")
            self.assertEqual(body["history"], full["history"])
            self.assertEqual(body["history_total"], 3)

    async def test_saved_carries_persisted_entry_and_new_user_block(self):
        await self.new_session()
        code, events = await self.turn("/start")
        self.assertEqual(code, 200)
        kinds = [e["type"] for e in events]
        self.assertGreater(kinds.index("saved"), kinds.index("done"))

        saved = next(e for e in events if e["type"] == "saved")
        hist = (await self.get_session())["history"]
        self.assertEqual(saved["index"], 0)
        self.assertEqual(saved["entry"], hist[0], "saved 带的必须就是落盘的那一块")
        self.assertIsNone(saved["user"], "start 没有 user 块")

        code, events = await self.turn("/chat", {"input": "我向前走"})
        saved = next(e for e in events if e["type"] == "saved")
        hist = (await self.get_session())["history"]
        self.assertEqual([e["role"] for e in hist], ["assistant", "user", "assistant"])
        self.assertEqual(saved["index"], 2)
        self.assertEqual(saved["entry"], hist[2])
        self.assertEqual(saved["user"], hist[1], "chat 要把这轮新写的 user 块一起带来")
        self.assertEqual(saved["user"]["content"], "我向前走")

    async def test_saved_for_regenerate_replaces_same_index(self):
        await self.new_session()
        await self.turn("/start")
        before = (await self.get_session())["history"]
        code, events = await self.turn("/regenerate")
        self.assertEqual(code, 200)
        saved = next(e for e in events if e["type"] == "saved")
        hist = (await self.get_session())["history"]
        self.assertEqual(len(hist), len(before), "regenerate 不新增块")
        self.assertEqual(saved["index"], len(hist) - 1)
        self.assertEqual(saved["entry"], hist[-1])
        self.assertIsNone(saved["user"])

    async def test_saved_for_options_regen_carries_updated_entry(self):
        await self.new_session()
        await self.client.post("/api/sessions/s1/options", json={"enabled": True})
        await self.turn("/start")
        code, events = await self.turn("/regenerate_options")
        self.assertEqual(code, 200)
        saved = next(e for e in events if e["type"] == "saved")
        hist = (await self.get_session())["history"]
        self.assertEqual(saved["index"], len(hist) - 1)
        self.assertEqual(saved["entry"], hist[-1])
        self.assertEqual(saved["entry"]["options"], ["先去公会看看", "直接出城"])

    async def test_saved_index_matches_history_after_each_turn(self):
        """前端就是按 (index, entry) 补本地 history 的：每一步都必须对得上。"""
        await self.new_session()
        steps = [
            ("/start", None, 1),
            ("/chat", {"input": "一步"}, 3),
            ("/chat", {"input": "再一步"}, 5),
            ("/regenerate", None, 5),
        ]
        for path, body, expect_len in steps:
            _, events = await self.turn(path, body)
            saved = next(e for e in events if e["type"] == "saved")
            hist = (await self.get_session())["history"]
            self.assertEqual(len(hist), expect_len, path)
            self.assertEqual(saved["index"], expect_len - 1, path)
            self.assertEqual(saved["entry"], hist[-1], path)

    async def test_error_event_carries_last_error(self):
        """没有收尾快照了，错误条只能靠事件里的 last_error 保持准确。"""
        await self.new_session()
        self.llm.fail = True
        _, events = await self.turn("/start")
        err = next(e for e in events if e["type"] == "error")
        self.assertEqual(err["last_error"]["message"], "假模型故意失败")
