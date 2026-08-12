"""手写 OpenAI 风格 API 流式调用，以及 respond 工具参数的增量解析。"""
import json

import httpx

from app.core import RESPOND_TOOL

_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b', 'f': '\f', '"': '"', '\\': '\\', '/': '/'}


class ContentExtractor:
    """从流式的工具调用 arguments JSON 中增量解出 content 字段的字符串值。"""

    def __init__(self):
        self.buf = ""
        self.pos = None  # content 值起始引号之后的下标
        self.emitted = 0

    def feed(self, fragment: str) -> str:
        self.buf += fragment
        if self.pos is None:
            i = self.buf.find('"content"')
            if i == -1:
                return ""
            colon = self.buf.find(':', i)
            if colon == -1:
                return ""
            quote = self.buf.find('"', colon)
            if quote == -1:
                return ""
            self.pos = quote + 1
        decoded = self._decode_available()
        delta = decoded[self.emitted:]
        self.emitted = len(decoded)
        return delta

    def _decode_available(self) -> str:
        s, i, out = self.buf, self.pos, []
        while i < len(s):
            c = s[i]
            if c == '"':  # 字符串结束
                break
            if c == '\\':
                if i + 1 >= len(s):
                    break  # 转义序列被分片截断，等待更多数据
                n = s[i + 1]
                if n == 'u':
                    if i + 6 > len(s):
                        break
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                out.append(_ESCAPES.get(n, n))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)


async def stream_respond(messages: list, config: dict):
    """调用 chat/completions 流式接口。

    yield 事件 dict：
      {"type": "reasoning", "delta": str}  思考内容
      {"type": "content", "delta": str}    正文增量
      {"type": "done", "content": str, "options": list}
    出错时抛出异常，由调用方转成 error 事件。
    """
    base = config["api_base"].rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": config["model"],
        "messages": messages,
        "tools": [RESPOND_TOOL],
        # 思考模式下 DeepSeek 不支持强制 tool_choice，用 auto 并由 AIRP 提示词约束其调用 respond
        "tool_choice": "auto",
        "stream": True,
    }
    for key in ("temperature", "max_tokens", "reasoning_effort"):
        if config.get(key) is not None:
            payload[key] = config[key]

    extractor = ContentExtractor()
    arguments = ""
    reasoning_full = ""
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Accept": "text/event-stream",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"API 错误 {resp.status_code}: {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content")
                    if reasoning:
                        reasoning_full += reasoning
                        yield {"type": "reasoning", "delta": reasoning}
                    for tc in delta.get("tool_calls") or []:
                        frag = (tc.get("function") or {}).get("arguments") or ""
                        if frag:
                            arguments += frag
                            text = extractor.feed(frag)
                            if text:
                                yield {"type": "content", "delta": text}

    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        args = {"content": arguments}
    options = args.get("options")
    if not isinstance(options, list):
        options = []
    yield {"type": "done", "content": args.get("content", ""), "options": [str(o) for o in options], "reasoning": reasoning_full}
