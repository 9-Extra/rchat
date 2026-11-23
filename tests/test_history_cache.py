"""history.jsonl 的解析缓存：命中时不再读盘，写入/外部改动后必须立刻读到新内容。

缓存是按「文件身份」(mtime_ns, size) 认的，而磁盘上的 history 既会被应用改（走
save_history），也可能被外部直接改。这里的用例守住三条：

1. 同一份文件第二次解析不再读盘（这是缓存存在的唯一理由）；
2. save_history 之后即使把 mtime 改回去，也不能拿旧的缓存顶包；
3. load_history 给调用方的是副本——正文里对 history 的 append / pop / 改顶层键
   不能污染缓存（server 的 _prepare / _persist 就是这么改 history 的）。
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core


class HistoryCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-hist-cache-")
        self.dir = pathlib.Path(self._tmp.name)
        self._patch = mock.patch.object(core, "SESSIONS_DIR", self.dir)
        self._patch.start()
        with core._HISTORY_CACHE_GUARD:
            core._HISTORY_CACHE.clear()
            core._HISTORY_CACHE_BYTES = 0

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def path(self, key: str) -> pathlib.Path:
        return self.dir / key / "history.jsonl"

    def make_session(self, key: str) -> None:
        """save_history 不负责建目录（真实流程里 create_session 建），测试里补上。"""
        self.path(key).parent.mkdir(parents=True, exist_ok=True)

    def test_hit_does_not_touch_disk(self):
        key = "s-hit"
        self.make_session(key)
        core.save_history(key, [{"role": "user", "content": "a"},
                                {"role": "assistant", "content": "b"}])
        core.load_history(key)  # 首次：解析并进缓存

        # 之后无论是再取一次正文，还是侧栏轮询只数轮数，都只该用缓存
        with mock.patch.object(core.Path, "read_text",
                               side_effect=AssertionError("命中缓存时不该读盘")):
            self.assertEqual([e["content"] for e in core.load_history(key)], ["a", "b"])
            self.assertEqual(core.count_turns(key), 1)

    def test_save_invalidates_even_with_same_size_and_mtime(self):
        key = "s-save"
        self.make_session(key)
        p = self.path(key)
        core.save_history(key, [{"role": "assistant", "content": "aaaa"}])
        st = p.stat()
        core.load_history(key)  # 让旧内容进缓存（stamp 就是 st）

        core.save_history(key, [{"role": "assistant", "content": "bbbb"}])
        # 长度相同，再把 mtime 改回旧值：只靠 (mtime, size) 是分不出新旧的，
        # 必须靠 save_history 里的「写后失效」。
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(core.load_history(key)[0]["content"], "bbbb")

    def test_caller_mutation_does_not_poison_cache(self):
        key = "s-copy"
        self.make_session(key)
        original = [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回复", "options": ["- 一", "- 二"]},
        ]
        core.save_history(key, original)

        mine = core.load_history(key)
        mine.append({"role": "user", "content": "偷偷加的"})
        mine[0]["content"] = "改坏了"
        mine[1]["options"] = ["- 换掉"]
        mine.pop(1)

        # 缓存里还是磁盘上那份：再取一次和刚写进去的完全一致
        self.assertEqual(core.load_history(key), original)
        self.assertEqual(core.count_turns(key), 1)

    def test_external_edit_is_picked_up(self):
        key = "s-ext"
        p = self.path(key)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"role": "user", "content": "旧"}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        self.assertEqual(core.load_history(key)[0]["content"], "旧")

        # 外部直接改文件（没走 save_history）：mtime/size 变了就该重新解析
        p.write_text(json.dumps({"role": "user", "content": "外部改过的"}, ensure_ascii=False)
                     + "\n", encoding="utf-8")
        self.assertEqual(core.load_history(key)[0]["content"], "外部改过的")

    def test_deleted_history_returns_empty_and_drops_cache(self):
        key = "s-del"
        self.make_session(key)
        core.save_history(key, [{"role": "user", "content": "x"}])
        core.load_history(key)

        p = self.path(key)
        p.unlink()
        self.assertEqual(core.load_history(key), [])
        self.assertNotIn(p, core._HISTORY_CACHE)

    def test_count_turns_ignores_compacted_summary(self):
        key = "s-turns"
        self.make_session(key)
        core.save_history(key, [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "摘要", "compacted": True},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ])
        self.assertEqual(core.count_turns(key), 2)

    def test_over_budget_evicts_oldest(self):
        """总量超上限时按插入顺序淘汰，缓存不会无限长（单进程长期开着也不吃内存）。"""
        keys = [f"s-evict-{i}" for i in range(3)]
        for k in keys:
            self.make_session(k)
            core.save_history(k, [{"role": "user", "content": "x" * 400}])
        size = self.path(keys[0]).stat().st_size

        with mock.patch.object(core, "_HISTORY_CACHE_MAX_BYTES", size * 2 + 1):
            for k in keys:
                core.load_history(k)

        self.assertEqual(len(core._HISTORY_CACHE), 2)
        self.assertNotIn(self.path(keys[0]), core._HISTORY_CACHE)
        self.assertIn(self.path(keys[2]), core._HISTORY_CACHE)
        self.assertLessEqual(core._HISTORY_CACHE_BYTES, size * 2)


if __name__ == "__main__":
    unittest.main()
