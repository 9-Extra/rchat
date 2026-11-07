"""文件工具（read_file / write_file / edit_file）的逐字匹配与换行处理。

守住的坑：`Path.read_text` 的 `newline` 参数是 **Python 3.13** 才加的，而 pyproject
声明 `requires-python = ">=3.10"`。旧实现 `read_text(encoding="utf-8", newline="")`
在 3.10~3.12（Debian 12 的系统 python3 就是 3.11）上直接 TypeError，edit_file
一调就废——read_file/write_file 没事，所以只在稍微旧一点的环境里暴露。
下面处理 CRLF 的用例在那些版本上一律拿到 `错误：TypeError...`，会红。
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

    def test_edit_does_not_need_read_text_newline(self):
        """模拟 3.10~3.12：`Path.read_text` 不认 newline 关键字也能改文件。"""
        self.path.write_bytes("a\r\nb\r\n".encode("utf-8"))
        real = pathlib.Path.read_text

        def old_signature(self, encoding=None, errors=None):
            return real(self, encoding, errors)

        pathlib.Path.read_text = old_signature
        try:
            self.assert_ok(tools.edit_file(str(self.path), "a\nb\n", "A\nB\n"))
        finally:
            pathlib.Path.read_text = real
        self.assertEqual(self.path.read_bytes(), b"A\r\nB\r\n")

    def test_relative_path_uses_base_dir(self):
        (self.dir / "sub").mkdir()
        self.assert_ok(tools.write_file("sub/a.txt", "旧\n", base_dir=self.dir))
        self.assert_ok(tools.edit_file("sub/a.txt", "旧", "新", base_dir=self.dir))
        self.assertEqual((self.dir / "sub" / "a.txt").read_bytes(), "新\n".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
