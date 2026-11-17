"""文件工具（read_file / write_file / edit_file）的逐字匹配与换行处理。

守住的行为：模型看到的是归一化后的 LF，磁盘上可能是 CRLF，两边都要能对上；
替换完成后原文件的换行风格不变。项目要求 Python ≥3.14，`Path.read_text/write_text`
的 `newline=` 可以直接用，不需要退回 `open`。
"""
import pathlib
import tempfile
import unittest

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import tools


class EditFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rchat-files-")
        self.dir = pathlib.Path(self._tmp.name)
        self.path = self.dir / "note.md"

    def tearDown(self):
        self._tmp.cleanup()

    def assert_ok(self, result):
        self.assertFalse(result.startswith("错误："), result)

    def test_edit_replaces_exact_text(self):
        self.path.write_bytes("第一行\n第二行\n第三行\n".encode("utf-8"))
        self.assert_ok(tools.edit_file(str(self.path), "第二行\n", "第二行（改）\n"))
        self.assertEqual(
            self.path.read_text(encoding="utf-8"), "第一行\n第二行（改）\n第三行\n"
        )

    def test_edit_keeps_crlf_and_accepts_lf_old_string(self):
        """模型看到的是 read_file 归一化后的 LF，磁盘上是 CRLF：两边都要能对上。"""
        self.path.write_bytes("a\r\nb\r\nc\r\n".encode("utf-8"))
        self.assert_ok(tools.edit_file(str(self.path), "b\nc\n", "B\nC\n"))
        self.assertEqual(self.path.read_bytes(), b"a\r\nB\r\nC\r\n")

    def test_edit_rejects_missing_and_duplicate(self):
        self.path.write_bytes(b"x\ny\nx\n")
        missing = tools.edit_file(str(self.path), "zzz", "q")
        self.assertIn("找不到 old_string", missing)
        dup = tools.edit_file(str(self.path), "x", "q")
        self.assertIn("出现 2 次", dup)
        self.assertEqual(self.path.read_bytes(), b"x\ny\nx\n", "报错时不该动文件")

    def test_edit_replace_all(self):
        self.path.write_bytes(b"x\nx\nx\n")
        self.assert_ok(tools.edit_file(str(self.path), "x", "y", True))
        self.assertEqual(self.path.read_bytes(), b"y\ny\ny\n")

    def test_edit_mixed_line_endings(self):
        """文件里混用 LF / CRLF 时，统一按 LF 匹配，写回时仍用原文件的主风格。"""
        self.path.write_bytes(b"a\r\nb\nc\r\n")
        self.assert_ok(tools.edit_file(str(self.path), "b\nc\n", "B\nC\n"))
        self.assertEqual(self.path.read_bytes(), b"a\r\nB\r\nC\r\n")

    def test_relative_path_uses_base_dir(self):
        (self.dir / "sub").mkdir()
        self.assert_ok(tools.write_file("sub/a.txt", "旧\n", base_dir=self.dir))
        self.assert_ok(tools.edit_file("sub/a.txt", "旧", "新", base_dir=self.dir))
        self.assertEqual((self.dir / "sub" / "a.txt").read_bytes(), "新\n".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
