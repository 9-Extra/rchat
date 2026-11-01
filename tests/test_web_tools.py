"""联网工具（web_search / web_fetch）：配置解析、参数校验、结果格式化与工具分发。

全部离线：真正发 HTTP 的那一层（`tools._web_api`）被换成假的，不连任何接口。
"""
import copy
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import core, tools, world

CONFIG = {
    "endpoints": [{"api_base": "https://api.deepseek.com", "model": "m", "api_key": "endpoint-key"}],
    "web_tools": {"api_key": "search-key", "count": 5},
}

SEARCH_RESPONSE = {
    "results": [
        {
            "title": "DeepSeek V4 发布",
            "url": "https://example.com/v4",
            "snippet": "短摘要",
            "summary": "长摘要内容",
            "content": "正文内容",
            "site_name": "Example News",
            "date": "2026-06-01T00:00:00+00:00",
            "authority": 3,
        }
    ]
}


class WebToolConfigTest(unittest.TestCase):

    def test_defaults_and_validation(self):
        cfg = core.web_tool_config({"web_tools": {"api_key": " k "}})
        self.assertEqual(cfg["api_base"], core.WEB_TOOL_API_BASE)
        self.assertEqual(cfg["api_key"], "k")
        self.assertEqual(cfg["count"], core.WEB_TOOL_COUNT)
        self.assertEqual(cfg["timeout"], float(core.WEB_TOOL_TIMEOUT))
        for bad, needle in (
            ({"web_tools": {}}, "web_tools.api_key"),
            ({"endpoints": []}, "web_tools.api_key"),
            ({"web_tools": {"api_key": "k", "count": 99}}, "web_tools.count"),
            ({"web_tools": {"api_key": "k", "timeout": 0}}, "web_tools.timeout"),
            ({"web_tools": "x"}, "不是映射"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    core.web_tool_config(bad)
                self.assertIn(needle, str(ctx.exception))


class WebSearchTest(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(core, "load_config", lambda: copy.deepcopy(CONFIG))
        patch.start()
        self.addCleanup(patch.stop)

    def test_uses_config_count_and_formats_results(self):
        with mock.patch.object(tools, "_web_api", return_value=SEARCH_RESPONSE) as api:
            text = tools.web_search("DeepSeek V4")
        self.assertEqual(api.call_args.args[1], "/v1/tools/search")
        # count 不传时用 config.yaml 里 web_tools.count
        self.assertEqual(
            api.call_args.args[2],
            {"query": "DeepSeek V4", "count": 5, "need_content": False},
        )
        self.assertIn("DeepSeek V4 发布", text)
        self.assertIn("https://example.com/v4", text)
        self.assertIn("长摘要内容", text)
        self.assertIn("Example News", text)
        self.assertIn("2026-06-01", text)
        self.assertIn("权威性 3/4", text)

    def test_need_content_flag_and_ori_question_pass_through(self):
        with mock.patch.object(tools, "_web_api", return_value={"results": []}) as api:
            tools.web_search("q", 3, True, "玩家问的原始问题")
        payload = api.call_args.args[2]
        self.assertEqual(payload["count"], 3)
        self.assertIs(payload["need_content"], True)
        self.assertEqual(payload["ori_question"], "玩家问的原始问题")

    def test_no_results_is_not_an_error(self):
        with mock.patch.object(tools, "_web_api", return_value={"results": []}):
            self.assertIn("没有匹配的结果", tools.web_search("啥也没有"))

    def test_query_and_count_validation(self):
        self.assertIn("query 不能为空", tools.web_search("   "))
        self.assertIn("最多 128", tools.web_search("长" * 129))
        self.assertIn("count", tools.web_search("q", count=51))
        self.assertIn("count", tools.web_search("q", count=0))

    def test_missing_key_reports_config_error_instead_of_raising(self):
        with mock.patch.object(core, "load_config", return_value={"endpoints": []}):
            self.assertIn("web_tools.api_key", tools.web_search("q"))

    def test_api_failure_is_returned_as_text(self):
        with mock.patch.object(tools, "_web_api", side_effect=RuntimeError("HTTP 429，超过 QPS 限制")):
            text = tools.web_search("q")
        self.assertIn("web_search 失败", text)
        self.assertIn("429", text)


class WebFetchTest(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(core, "load_config", lambda: copy.deepcopy(CONFIG))
        patch.start()
        self.addCleanup(patch.stop)

    def test_returns_title_and_markdown(self):
        with mock.patch.object(tools, "_web_api", return_value={"title": "标题", "content": "# 正文"}) as api:
            text = tools.web_fetch("https://example.com/a")
        self.assertEqual(api.call_args.args[1], "/v1/tools/fetch")
        self.assertEqual(api.call_args.args[2], {"url": "https://example.com/a"})
        self.assertIn("<title>标题</title>", text)
        self.assertIn("# 正文", text)

    def test_long_content_is_truncated(self):
        with mock.patch.object(tools, "_web_api", return_value={"title": "", "content": "正文" * 20000}):
            text = tools.web_fetch("https://example.com/a")
        self.assertIn("已截断", text)
        self.assertLess(len(text), tools.WEB_FETCH_MAX_CHARS + 200)

    def test_url_validation(self):
        for bad in ("", "ftp://example.com/a", "https:///a", "https://user:pw@example.com/a"):
            with self.subTest(bad=bad):
                self.assertIn("错误", tools.web_fetch(bad))

    def test_empty_content_reports_error(self):
        with mock.patch.object(tools, "_web_api", return_value={"title": "", "content": ""}):
            self.assertIn("没有取到正文", tools.web_fetch("https://example.com/a"))

    def test_missing_key_reports_config_error_instead_of_raising(self):
        with mock.patch.object(core, "load_config", return_value={"endpoints": []}):
            self.assertIn("web_tools.api_key", tools.web_fetch("https://example.com/a"))


class WebToolWiringTest(unittest.TestCase):
    """schema、预设白名单与 world 绑定：两个工具要能像其它工具一样被启用。"""

    def test_schema_and_whitelist(self):
        preset = {"id": "p", "tools": ["world_run", "web_search", "web_fetch"]}
        direct, bindings = core.preset_tools(preset)
        self.assertEqual(direct, ["world_run", "web_search", "web_fetch"])
        self.assertEqual(bindings, ["web_search", "web_fetch"])
        schemas = core.get_tools("responses", False, direct, bindings)
        self.assertEqual([s["name"] for s in schemas], ["world_run", "web_search", "web_fetch"])
        names = list(core._TOOL_DEFS["web_search"]["parameters"]["properties"])
        self.assertEqual(names, ["query", "count", "need_content", "ori_question"])

    def test_ptc_preset_gets_them_as_bindings(self):
        ptc = {"id": "p", "ptc": True, "tools": ["web_search", "web_fetch"]}
        self.assertEqual(core.preset_tools(ptc), (["world_run"], ["web_search", "web_fetch"]))
        schema = core.get_tools("chat_completions", True, ["world_run"], ["web_search"])[0]
        self.assertIn("web_search", schema["function"]["description"])

    def test_world_bindings_expose_web_tools(self):
        rt = {"name": "s", "logs": []}
        with mock.patch.object(core, "load_state", return_value={"preset": "p"}), \
                mock.patch.object(
                    core, "session_tools",
                    return_value=(["world_run"], ["web_search", "web_fetch"]),
                ):
            bindings = world._bindings_for(rt, "s")
        self.assertEqual(set(bindings), {"web_search", "web_fetch"})
        with mock.patch.object(tools, "web_search", return_value="搜索结果"):
            self.assertEqual(bindings["web_search"]("q", 3), "搜索结果")
        with mock.patch.object(tools, "web_fetch", return_value="抓取结果"):
            self.assertEqual(bindings["web_fetch"]("https://e.com"), "抓取结果")
        self.assertEqual(rt["logs"], ["搜索结果", "抓取结果"])


class ExecuteWebToolTest(unittest.IsolatedAsyncioTestCase):
    """execute_tool 的分发：参数按 schema 传到位，且不需要角色卡目录就能跑。"""

    async def test_dispatch(self):
        preset = {"id": "p", "tools": ["web_search", "web_fetch"]}
        with mock.patch.object(core, "load_state", return_value={"preset": "p"}), \
                mock.patch.object(core, "load_presets", return_value={"p": preset}), \
                mock.patch.object(tools, "web_search", return_value="搜索 ok") as search, \
                mock.patch.object(tools, "web_fetch", return_value="抓取 ok") as fetch:
            out = await tools.execute_tool(
                "s", "web_search",
                json.dumps({"query": "q", "count": 3, "need_content": True}),
            )
            self.assertEqual(out, "搜索 ok")
            search.assert_called_once_with("q", 3, True, None)
            out = await tools.execute_tool(
                "s", "web_fetch", json.dumps({"url": "https://e.com"})
            )
            self.assertEqual(out, "抓取 ok")
            fetch.assert_called_once_with("https://e.com")

    async def test_unknown_tool_lists_enabled_tools(self):
        preset = {"id": "p", "tools": ["web_search"]}
        with mock.patch.object(core, "load_state", return_value={"preset": "p"}), \
                mock.patch.object(core, "load_presets", return_value={"p": preset}):
            out = await tools.execute_tool("s", "web_fetch", json.dumps({"url": "https://e.com"}))
        self.assertIn("未知工具 web_fetch", out)
        self.assertIn("web_search", out)


if __name__ == "__main__":
    unittest.main()
