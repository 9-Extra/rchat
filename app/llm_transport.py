"""LLM 需要的极简 HTTP 传输层：POST、SSE 解析、重试。

只做三件事，业务逻辑不要往这里堆：

1. 按端点的 api_type 把请求发到 `/chat/completions` 或 `/responses`；
2. 把 SSE 的 `data:` 行解成 dict——两个 API 的每条 JSON 里都自带 `type`，所以 `event:`
   行不需要；
3. 连接错误 / 408 / 409 / 429 / 5xx 按指数退避重试，其余错误带着状态码和响应正文抛出。

流式请求的重试只发生在「还没开始读流」之前：流一旦开始，重试就会把已经输出的正文再发
一遍，所以读流阶段出错直接抛给调用方（由上层决定落半截还是重来）。
"""
import asyncio
import json
import logging
from contextlib import suppress

import httpx

logger = logging.getLogger("airp.llm.transport")

# 单次请求的超时（秒）。思考型模型可能很久才吐第一个 token，沿用 SDK 时代的 600s。
DEFAULT_TIMEOUT = 600.0

# 与 OpenAI SDK 的默认行为对齐：最多重试 2 次，重试这些状态码。
MAX_RETRIES = 2
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0
# 上游给的 Retry-After 超过这个值就不重试了（SDK 也是 120s），免得把请求挂太久。
MAX_RETRY_AFTER = 120.0

# `[DONE]` 之后把剩余字节读干净，连接才能回到池里复用于下一个请求。正常实现后面就是 EOF
# （DeepSeek 实测 1ms 内读完）；给个上限，免得个别代理不结束响应体把这一轮挂到读超时。
DRAIN_TIMEOUT = 2.0

# 没配 user_agent 时的默认 UA。之前 SDK 发的是 "OpenAI/Python x.y.z"；用自己的名字
# 更诚实，配置里的 user_agent 会覆盖它（opencode-go 等端点风控拒绝语言默认 UA）。
DEFAULT_USER_AGENT = "rchat/0.1"


class LLMError(RuntimeError):
    """上游返回的错误：HTTP 非 2xx、流内 error、响应不是预期的 JSON/SSE。"""


async def _sleep(seconds: float) -> None:
    """可被测试替换的 sleep。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _retry_after(headers) -> float | None:
    """解析 Retry-After（只认秒数形式；HTTP 日期形式少见，退回自己的退避）。"""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _retry_delay(attempt: int, headers) -> float | None:
    """这次失败后等多久再试；返回 None 表示不该重试。"""
    after = _retry_after(headers)
    if after is not None:
        return None if after > MAX_RETRY_AFTER else after
    return min(INITIAL_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)


def _error_text(obj: dict) -> str:
    """把流内 error 对象压成一句话（OpenRouter 与 Responses 的形状不一样）。"""
    error = obj.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error:
        return str(error)
    return str(obj.get("message") or obj.get("code") or obj)


def _decode_event(lines: list[str]) -> tuple[bool, dict | None]:
    """解一条 SSE 事件，返回 (是否 [DONE], JSON 对象)。"""
    raw = "\n".join(lines).strip()
    if not raw:
        return False, None
    if raw == "[DONE]":
        return True, None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"上游发来非 JSON 的 SSE 数据: {raw[:200]}") from e
    if not isinstance(obj, dict):
        return False, None
    if obj.get("error") or obj.get("type") == "error":
        raise LLMError(f"上游流内错误: {_error_text(obj)}")
    return False, obj


async def _iter_sse(lines):
    """把 SSE 的 data 行流解成 dict。

    兼容 CRLF、`data:` 后无空格、多行 data、BOM、流末尾没发空行的实现；`event:` /
    `id:` / `retry:` / 注释行全部忽略。
    """
    data_lines: list[str] = []
    async for line in lines:
        if line.startswith("\ufeff"):
            line = line[1:]
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
            continue
        if line:
            continue
        if not data_lines:
            continue
        done, obj = _decode_event(data_lines)
        data_lines = []
        if done:
            return
        if obj is not None:
            yield obj
    if data_lines:
        # 有的代理不发结尾空行就断流：最后一条能解就解，解不出会在这里报错。
        done, obj = _decode_event(data_lines)
        if not done and obj is not None:
            yield obj


async def _alines(lines):
    """把同步的行列表包成异步迭代器（SSE 标错 Content-Type 时按文本重解）。"""
    for line in lines:
        yield line


class LLMClient:
    """一个端点的 HTTP 客户端：POST 一律走 `request()`，流式走 `stream()`。

    构造参数是端点快照，不做配置解析；`transport` 只给测试用（httpx.MockTransport）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        path: str,
        headers: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + path
        self._max_retries = max_retries
        merged = {
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            "Authorization": f"Bearer {api_key}",
        }
        if headers:
            merged.update(headers)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers=merged,
            # 与 SDK 的行为对齐：URL 配错（如少了 /v1）时跟随 301/302，别静默失败。
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        await self.aclose()
        return False

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    async def _send(
        self, payload: dict, *, stream: bool, headers: dict | None = None
    ) -> httpx.Response:
        """发一次请求；可重试的失败按退避重试，最终失败抛 LLMError。"""
        for attempt in range(self._max_retries + 1):
            # headers 是逐请求的（如每会话固定的 X-Opencode-Session）；客户端的常驻头不在这里。
            request = self._http.build_request(
                "POST", self._url, json=payload, headers=headers
            )
            try:
                resp = await self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                if attempt >= self._max_retries:
                    raise LLMError(f"连接失败: {type(e).__name__}: {e}") from e
                delay = _retry_delay(attempt, {})
                logger.warning(
                    "LLM 请求连接失败(%s)，%.1fs 后重试: %s", type(e).__name__, delay, e
                )
                await _sleep(delay)
                continue
            if resp.status_code < 400:
                return resp

            body = (await resp.aread()).decode("utf-8", "replace")
            retryable = resp.status_code in RETRY_STATUS and attempt < self._max_retries
            delay = _retry_delay(attempt, resp.headers) if retryable else None
            error = _http_error(resp, body)
            await resp.aclose()
            if delay is None:
                raise error
            logger.warning("LLM 请求 HTTP %d，%.1fs 后重试: %s", resp.status_code, delay, body[:300])
            await _sleep(delay)
        raise LLMError("LLM 请求重试次数用尽")  # 循环必然在上面的分支里返回或抛出

    async def stream(self, payload: dict, *, headers: dict | None = None):
        """发流式请求，逐个 yield SSE 里的 JSON 对象。"""
        resp = await self._send(payload, stream=True, headers=headers)
        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type.startswith("application/json"):
            # 200 + JSON 通常是网关的错误页；也有代理把 SSE 标成 JSON，按文本再解一遍。
            body = (await resp.aread()).decode("utf-8", "replace")
            await resp.aclose()
            try:
                json.loads(body)
            except json.JSONDecodeError:
                async for obj in _iter_sse(_alines(body.splitlines())):
                    yield obj
                return
            raise LLMError(f"上游返回 JSON 而不是流（HTTP {resp.status_code}）: {body[:800]}")
        lines = resp.aiter_lines()
        try:
            async for obj in _iter_sse(lines):
                yield obj
        except BaseException:
            # 取消/流内错误：不读尾巴，直接关连接（这条连接本来也复用不了），
            # 否则一次打断还要在 2s 的 drain 上限里等。
            with suppress(Exception):
                await resp.aclose()
            raise
        await self._drain_and_close(resp, lines)

    @staticmethod
    async def _drain_and_close(resp: httpx.Response, lines) -> None:
        """把 SSE 剩余字节读干净再关响应：读干净，连接才会回到池里给下一个请求复用。"""
        try:
            async with asyncio.timeout(DRAIN_TIMEOUT):
                async for _ in lines:
                    pass
        except TimeoutError:
            logger.debug("读取流尾部超时，连接不会复用")
        except Exception:
            logger.debug("读取流尾部失败，连接不会复用", exc_info=True)
        finally:
            await resp.aclose()

    async def request(self, payload: dict, *, headers: dict | None = None) -> dict:
        """发非流式请求，返回解析好的 JSON 对象。"""
        resp = await self._send(payload, stream=False, headers=headers)

        raw = (await resp.aread()).decode("utf-8", "replace")
        await resp.aclose()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"HTTP {resp.status_code} 但响应不是 JSON: {raw[:500]}"
            ) from e
        if not isinstance(data, dict):
            raise LLMError(f"HTTP {resp.status_code} 但响应不是 JSON 对象: {raw[:500]}")
        return data


def _http_error(resp: httpx.Response, body: str) -> LLMError:
    """HTTP 错误文本：状态码 + 正文，能拿到 request id 就带上，方便对账。"""
    message = f"HTTP {resp.status_code}: {body[:800]}"
    request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
    if request_id:
        message += f" (request id: {request_id})"
    return LLMError(message)
