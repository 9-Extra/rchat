"""落盘原子性：同一个文件被多个线程并发写时，内容必须完整、没有残留临时文件。

背景（为什么要有那把按路径的锁 + 唯一临时名）：FastAPI 的同步端点跑在工作线程池里，
同进程两个线程写同一个文件是真会发生的（典型：两个人同时登录写 auth_sessions.json）。
旧的「临时名只带 pid」实现实测 40 线程 × 25 次里：
  Linux   每 1000 次约 39 次 FileNotFoundError（后写者把临时文件搬走了）
  Windows 每 1000 次约 39 次 PermissionError / WinError 32（同一个临时文件被占用）
两者都会让一次落盘直接失败——在应用里就是那一轮 history 没保存。
"""
import json
import os
import pathlib
import tempfile
import threading
import unittest

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core

THREADS = 20
PER_THREAD = 20


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-atomic-")
        self.dir = pathlib.Path(self._tmp.name)
        self.path = self.dir / "target.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, fn):
        """并发写同一个文件。每人的载荷长度都不同，便于发现半截/交错写入。"""
        payloads, sizes = [], {}
        for i in range(THREADS):
            body = json.dumps({"writer": i, "pad": "x" * (1000 + i * 137)})
            payloads.append(body)
            sizes[len(body)] = i
        errors = []

        def worker(i):
            try:
                for _ in range(PER_THREAD):
                    fn(self.path, payloads[i])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        raw = self.path.read_text(encoding="utf-8")
        leftover = [p.name for p in self.dir.glob("target.json.*")]
        return raw, sizes, errors, leftover

    def test_concurrent_writes_stay_intact(self):
        raw, sizes, errors, leftover = self._run(core._atomic_write_text)

        self.assertEqual(errors, [], f"并发写报了异常：{errors[:3]}")
        self.assertEqual(leftover, [], f"留下了临时文件：{leftover}")
        obj = json.loads(raw)  # 半截 JSON 会在这里炸
        self.assertIn(len(raw), sizes, f"落盘长度 {len(raw)} 不属于任何写入者")
        self.assertEqual(obj["writer"], sizes[len(raw)], "内容不是某一次完整写入")

    def test_old_pid_only_tmpname_is_actually_broken(self):
        """记录旧实现的失败模式：只带 pid 的临时名在并发下必然出问题。

        复现不了就 skip（并发能不能撞上跟机器调度有关），不让它变成假红。
        """
        def old_atomic(path: pathlib.Path, text: str) -> None:
            tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)

        _, _, errors, _ = self._run(old_atomic)
        if not errors:
            self.skipTest("这次没撞上（并发调度相关），旧实现的失败模式见文件头注释")
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
