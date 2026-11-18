"""LLM 传输层：SSE 解析、错误与重试、请求体形状。

全部离线：httpx.MockTransport 回放字节流，不连任何端点。这里覆盖的是以前被 SDK 挡住、
从来没测过的部分——SSE 的分包/换行/流内错误，以及重试的边界。
"""
import json
import unittest
from unittest import mock

import httpx

import tests.support  # noqa: F401  （把仓库根加进 sys.path）

from app import config, llm, llm_transport
from app.llm_transport import LLMClient, LLMError
from tests.support import make_turn

USER_ITEM = [{"role": "user", "content": "看看"}]
USER_INPUT_ITEM = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "看看"}]}]

CHAT_SSE = (
    b": ping\r\n\r\n"
    b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x83\xb3\xe4\xb8\x80\xe4\xb8\x8b"}}]}\r\n\r\n'
    b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
    # 一条事件的两行 data（SSE 规范允许，拼起来仍是合法 JSON）
    b'data: {"choices":\n'
    b'data: [{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n'
    b'data: [DONE]\n\n'
)

RESP_SSE = (
    b'event: response.reasoning_text.delta\n'
    b'data: {"type":"response.reasoning_text.delta","item_id":"rs_1","delta":"\xe6\x83\xb3"}\n\n'
    b'event: response.output_text.delta\n'
    # 故意不发结尾空行：代理断流时最后一条事件也要能解出来
    b'data: {"type":"response.output_text.delta","item_id":"m1","delta":"\xe5\xa5\xbd"}'
)


class _ChunkedBody(httpx.AsyncByteStream):
    """按给定字节边界切开响应体，模拟 TCP 分包（包括把多字节 UTF-8 切开）。"""

    def __init__(self, data: bytes, cuts: list[int]):
        self.data = data
        self.cuts = cuts

    async def __aiter__(self):
        prev = 0
        for cut in self.cuts + [len(self.data)]:
            if cut > prev:
                yield self.data[prev:cut]
            prev = cut


class _TrackingBody(_ChunkedBody):
    """记录响应体是否被读完（验证 [DONE] 之后的 drain）。"""

    def __init__(self, data: bytes, cuts: list[int]):
        super().__init__(data, cuts)
        self.finished = False

    async def __aiter__(self):
        async for chunk in super().__aiter__():
            yield chunk
        self.finished = True


def _sse(body: bytes, cuts: list[int] | None = None, status: int = 200):
    return httpx.Response(
        status,
        content=_ChunkedBody(body, cuts or []),
        headers={"content-type": "text/event-stream"},
    )


def _client(handler, *, path: str = "/chat/completions", **kwargs) -> LLMClient:
    return LLMClient(
        base_url="https://fake.test/v1",
        api_key="test-key",
        path=path,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def _collect(client: LLMClient, payload: dict | None = None) -> list[dict]:
    return [obj async for obj in client.stream(payload or {"model": "m"})]


class StreamParseTest(unittest.IsolatedAsyncioTestCase):

    async def test_chat_sse_across_byte_cuts(self):
        """CRLF、注释、多行 data、UTF-8 跨包、[DONE] 都要正确切分。"""
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["ua"] = request.headers.get("user-agent")
            return _sse(CHAT_SSE, cuts=[1, 3, 7, 11, 40, 41, 42, 80, 160])

        async with _client(handler) as client:
            events = await _collect(client)

        self.assertEqual(len(events), 3, events)
        self.assertEqual(events[0]["choices"][0]["delta"]["reasoning_content"], "想一下")
        self.assertEqual(events[1]["choices"][0]["delta"]["content"], "你")
        self.assertEqual(events[2]["choices"][0]["delta"]["content"], "好")
        self.assertEqual(seen["url"], "https://fake.test/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["ua"], "rchat/0.1")

    async def test_responses_events_and_eof_without_blank_line(self):
        def handler(request):
            return _sse(RESP_SSE, cuts=[5, 17, 60, 61, 62])

        async with _client(handler, path="/responses") as client:
            events = await _collect(client)

        self.assertEqual([e["type"] for e in events], [
            "response.reasoning_text.delta", "response.output_text.delta",
        ])
        self.assertEqual(events[1]["delta"], "好")

    async def test_done_drains_rest_of_stream(self):
        """读到 [DONE] 后要继续把响应体读完，连接才会回到池里。"""
        body = _TrackingBody(b'data: [DONE]\n\ndata: {"late": true}\n\n', [1, 5, 9])

        def handler(request):
            return httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )

        async with _client(handler) as client:
            events = await _collect(client)
        self.assertEqual(events, [])
        self.assertTrue(body.finished, "收到 [DONE] 后没有 drain 剩余字节")

    async def test_midstream_error_raises(self):
        body = b'data: {"error":{"message":"quota exceeded","code":402}}\n\n'

        def handler(request):
            return _sse(body)

        async with _client(handler) as client:
            with self.assertRaises(LLMError) as ctx:
                await _collect(client)
        self.assertIn("quota exceeded", str(ctx.exception))

    async def test_http_error_includes_status_and_body_without_retry(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(400, json={"error": {"message": "bad param"}})

        async with _client(handler) as client:
            with self.assertRaises(LLMError) as ctx:
                await _collect(client)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("bad param", str(ctx.exception))
        self.assertEqual(len(calls), 1, "400 不该重试")

    async def test_retries_transient_status(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, text="busy")
            return _sse(CHAT_SSE)

        async with _client(handler) as client:
            with mock.patch.object(llm_transport, "_sleep", new=mock.AsyncMock()) as sleep:
                events = await _collect(client)

        self.assertEqual(len(events), 3)
        self.assertEqual(len(calls), 2)
        sleep.assert_awaited_once()

    async def test_gives_up_after_max_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, text="busy")

        async with _client(handler, max_retries=1) as client:
            with mock.patch.object(llm_transport, "_sleep", new=mock.AsyncMock()):
                with self.assertRaises(LLMError) as ctx:
                    await _collect(client)
        self.assertIn("HTTP 503", str(ctx.exception))
        self.assertEqual(len(calls), 2)

    async def test_connection_error_is_retried_then_wrapped(self):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("boom")

        async with _client(handler, max_retries=1) as client:
            with mock.patch.object(llm_transport, "_sleep", new=mock.AsyncMock()):
                with self.assertRaises(LLMError) as ctx:
                    await _collect(client)
        self.assertIn("连接失败", str(ctx.exception))
        self.assertEqual(len(calls), 2)

    async def test_200_json_is_not_treated_as_stream(self):
        def handler(request):
            return httpx.Response(200, json={"error": {"message": "no stream here"}})

        async with _client(handler) as client:
            with self.assertRaises(LLMError) as ctx:
                await _collect(client)
        self.assertIn("JSON 而不是流", str(ctx.exception))
        self.assertIn("no stream here", str(ctx.exception))

    async def test_per_request_headers_are_sent(self):
        """逐请求头（X-Opencode-Session）不能固化进按端点缓存的客户端。"""
        seen = {}

        def handler(request):
            seen["session"] = request.headers.get("x-opencode-session")
            seen["ua"] = request.headers.get("user-agent")
            return _sse(CHAT_SSE)

        async with _client(handler) as client:
            events = [obj async for obj in client.stream(
                {"model": "m"}, headers={"X-Opencode-Session": "chat-1"}
            )]
        self.assertEqual(len(events), 3)
        self.assertEqual(seen["session"], "chat-1")
        self.assertEqual(seen["ua"], "rchat/0.1")

    def test_retry_after_too_long_is_not_retried(self):
        self.assertIsNone(llm_transport._retry_delay(0, {"retry-after": "999"}))
        self.assertEqual(llm_transport._retry_delay(0, {"retry-after": "2"}), 2.0)


class RequestTest(unittest.IsolatedAsyncioTestCase):

    async def test_request_returns_json_dict(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        async with _client(handler, path="/responses") as client:
            data = await client.request({"model": "m", "input": []})
        self.assertEqual(data["choices"][0]["message"]["content"], "hi")

    async def test_request_non_json_raises(self):
        def handler(request):
            return httpx.Response(200, text="<html>oops</html>")

        async with _client(handler) as client:
            with self.assertRaises(LLMError) as ctx:
                await client.request({"model": "m"})
        self.assertIn("不是 JSON", str(ctx.exception))


class ClientCacheTest(unittest.IsolatedAsyncioTestCase):
    """按端点缓存 httpx.AsyncClient：同一连接参数复用，配置变更换新，退出可统一关闭。"""

    async def asyncSetUp(self):
        await llm.close_clients()
        self.addAsyncCleanup(llm.close_clients)

    async def test_reuses_client_for_same_endpoint(self):
        turn = make_turn("chat_completions")
        async with llm._make_client(turn) as first:
            async with llm._make_client(turn) as second:
                self.assertIs(first, second)
        self.assertFalse(first.is_closed, "租约退出时不该关闭缓存的客户端")

    async def test_distinct_client_per_connection_params(self):
        async with llm._make_client(make_turn("chat_completions")) as chat:
            async with llm._make_client(make_turn("responses")) as responses:
                self.assertIsNot(chat, responses)
            async with llm._make_client(make_turn("chat_completions", user_agent="ua-2")) as ua2:
                self.assertIsNot(chat, ua2)
                self.assertEqual(ua2._http.headers["user-agent"], "ua-2")

    async def test_close_clients_closes_and_forgets(self):
        turn = make_turn("chat_completions")
        async with llm._make_client(turn) as client:
            pass
        await llm.close_clients()
        self.assertTrue(client.is_closed)
        async with llm._make_client(turn) as again:
            self.assertIsNot(client, again)

    def test_opencode_header_is_per_request(self):
        ep = config.parse_endpoint({
            "api_base": "https://fake.test/v1",
            "model": "m",
            "api_key": "k",
            "api_type": "chat_completions",
            "x_opencode_session": True,
        }, 0)
        turn = make_turn("chat_completions", endpoint=ep, chat_id="chat-1")
        self.assertEqual(llm._turn_headers(turn), {"X-Opencode-Session": "chat-1"})
        self.assertIsNone(llm._turn_headers(make_turn("chat_completions")))


class BuildRequestTest(unittest.TestCase):
    """build_request 返回的就是线上请求体：不再有 extra_body，stream 也在里面。"""

    def test_chat_body_is_wire_body(self):
        body = llm.build_request(USER_ITEM, make_turn("chat_completions"))
        json.dumps(body)  # /api/preview 直接返回它，必须可序列化
        self.assertTrue(body["stream"])
        self.assertNotIn("extra_body", body)
        self.assertEqual(body["max_tokens"], 256)
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["tool_choice"], "auto")
        self.assertTrue(body["tools"])

    def test_responses_body_is_wire_body(self):
        body = llm.build_request(USER_INPUT_ITEM, make_turn("responses"))
        json.dumps(body)  # /api/preview 直接返回它，必须可序列化
        self.assertTrue(body["stream"])
        self.assertNotIn("extra_body", body)
        self.assertEqual(body["max_output_tokens"], 256)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["input"], USER_INPUT_ITEM)

    def test_vendor_fields_are_top_level(self):
        ep = config.parse_endpoint({
            "api_base": "https://fake.test/v1",
            "model": "m",
            "api_key": "k",
            "api_type": "chat_completions",
            "provider": {"sort": "throughput", "preferred_min_throughput": 30},
            "session_id": True,
        }, 0)
        turn = make_turn(
            "chat_completions", endpoint=ep, chat_id="chat-1", reasoning_effort="none"
        )
        body = llm.build_request(USER_ITEM, turn)
        self.assertEqual(body["provider"], {"sort": "throughput", "preferred_min_throughput": 30})
        self.assertEqual(body["session_id"], "chat-1")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("extra_body", body)


class NonStreamReplyTest(unittest.IsolatedAsyncioTestCase):
    """非流式的选项 / 压缩请求：从 dict 响应里取正文、工具调用与空输出原因。"""

    class _OneShot:
        def __init__(self, response):
            self.response = response
            self.payload = None

        async def request(self, payload, headers=None):
            self.payload = payload
            return self.response

    async def test_chat_options_text(self):
        client = self._OneShot({
            "choices": [{"message": {"content": "- 走左边\n- 走右边"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12,
                      "prompt_cache_hit_tokens": 64},
        })
        text, problem = await llm._request_options_once(client, USER_ITEM, make_turn())
        self.assertEqual(text, "- 走左边\n- 走右边")
        self.assertEqual(problem, "")
        self.assertNotIn("stream", client.payload, "非流式请求不该带 stream")

    async def test_chat_tool_call_is_reported(self):
        client = self._OneShot({"choices": [{"message": {
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "a"}'}}],
        }}]})
        text, problem = await llm._request_options_once(client, USER_ITEM, make_turn())
        self.assertIn("read_file", text)
        self.assertIn("调用了工具", problem)

    async def test_chat_empty_output_names_finish_reason(self):
        client = self._OneShot({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        _, problem = await llm._request_options_once(client, USER_ITEM, make_turn())
        self.assertIn("finish_reason=length", problem)

    async def test_responses_summary_text(self):
        client = self._OneShot({"output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": "摘要正文"}]},
        ]})
        text, problem = await llm._request_compact_once(client, USER_INPUT_ITEM, make_turn("responses"))
        self.assertEqual(text, "摘要正文")
        self.assertEqual(problem, "")

    async def test_responses_incomplete_names_reason(self):
        client = self._OneShot({
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        })
        _, problem = await llm._request_compact_once(client, USER_INPUT_ITEM, make_turn("responses"))
        self.assertIn("max_output_tokens", problem)


if __name__ == "__main__":
    unittest.main()
