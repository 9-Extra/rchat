"""bash 工具：shell 探测（跳过 WSL 启动器）、超时杀进程树、stdin 接空设备。

依赖本机装有可用的 bash（Windows 上是 Git Bash）；没有就 skip，不让没装 bash 的环境红。
"""
import sys
import time
import unittest
from pathlib import Path

import tests.support  # noqa: F401  （把仓库根加进 sys.path，并关掉测试期间的日志）

from app import tools


class ShellDetectionTest(unittest.TestCase):

    def test_skips_wsl_stub(self):
        if sys.platform != "win32":
            self.skipTest("只有 Windows 有 WindowsApps 启动器")
        stub = Path.home() / "AppData/Local/Microsoft/WindowsApps/bash.exe"
        self.assertTrue(tools._is_wsl_bash_stub(stub))
        self.assertFalse(tools._is_wsl_bash_stub(r"C:\Program Files\Git\bin\bash.exe"))

    def test_shell_is_not_wsl_stub(self):
        shell = tools._shell()
        if shell is None:
            self.skipTest("本机没有可用 bash")
        if sys.platform == "win32":
            self.assertNotIn("windowsapps", shell.lower())


class BashToolTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if tools._shell() is None:
            raise unittest.SkipTest("本机没有可用 bash")

    def test_echo_and_exit_code(self):
        result = tools.bash("echo hi")
        self.assertIn("<exit_code>0</exit_code>", result)
        self.assertIn("hi", result)

    def test_exit_code_is_reported(self):
        self.assertIn("<exit_code>3</exit_code>", tools.bash("exit 3"))

    def test_stdout_before_stderr(self):
        result = tools.bash("echo out; echo err >&2")
        self.assertLess(result.index("out"), result.index("err"))

    def test_stdin_is_closed(self):
        """没有 stdin 的命令（cat）应该立刻 EOF，而不是挂到超时。"""
        started = time.perf_counter()
        result = tools.bash("cat", timeout=2)
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertIn("<exit_code>0</exit_code>", result)
        self.assertNotIn("超过", result)

    def test_timeout_kills_tree_and_returns_quickly(self):
        """超时后要连孙进程一起杀掉：只杀 bash 的话 sleep 会攥着管道继续等。"""
        started = time.perf_counter()
        result = tools.bash("sleep 30", timeout=1)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3, f"超时没有及时返回，用了 {elapsed:.1f}s")
        self.assertIn("超过 1s", result)

    def test_timeout_keeps_partial_output(self):
        result = tools.bash("echo before; sleep 30", timeout=1)
        self.assertIn("before", result)
        self.assertIn("超过 1s", result)

    def test_argument_validation(self):
        self.assertIn("timeout 必须是", tools.bash("echo hi", timeout=0))
        self.assertIn("timeout 必须是", tools.bash("echo hi", timeout="3"))
        self.assertIn("command 不能为空", tools.bash("  "))


if __name__ == "__main__":
    unittest.main()
