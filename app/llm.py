"""DeepSeek Responses API 流式调用,以及 respond 工具参数的增量解析。"""
import json

from openai import AsyncOpenAI

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
                    break  # 转义序列被分片截断,等待更多数据
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


def build_request(input_items: list, config: dict) -> dict:
    """构造 responses.create 的完整请求参数(/api/preview 也用它展示上下文)。

    input_items 由 core.build_input 直接产出。输出格式为纯文本:
    不传 text(默认 {"type": "text"})。不使用任何内置工具。
    """
    payload = {
        "model": config["model"],
        "input": input_items,
        "tools": [RESPOND_TOOL],
        # 思考模式下不支持强制 tool_choice,用 auto 并由 AIRP 提示词约束其调用 respond
        "tool_choice": "auto",
    }
    if config.get("temperature") is not None:
        payload["temperature"] = config["temperature"]
    if config.get("max_tokens") is not None:
        payload["max_output_tokens"] = config["max_tokens"]
    if config.get("reasoning_effort") is not None:
        payload["reasoning"] = {"effort": config["reasoning_effort"]}
    return payload


async def stream_respond(input_items: list, config: dict):
    """调用 Responses API 流式接口。

    yield 事件 dict(与旧版 chat/completions 实现一致):
      {"type": "reasoning", "delta": str}  思考内容
      {"type": "content", "delta": str}    正文增量
      {"type": "done", "content": str, "options": list}
    出错时抛出异常,由调用方转成 error 事件。
    """
    extractor = ContentExtractor()
    arguments = ""
    reasoning_full = ""
    # 显式传入 api_key/base_url,不读取 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量
    async with AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["api_base"].rstrip("/"),
        timeout=600.0,
    ) as client:
        stream = await client.responses.create(**build_request(input_items, config), stream=True)
        async for event in stream:
            etype = event.type
            if etype == "response.reasoning_text.delta":
                reasoning_full += event.delta
                yield {"type": "reasoning", "delta": event.delta}
            elif etype == "response.function_call_arguments.delta":
                arguments += event.delta
                text = extractor.feed(event.delta)
                if text:
                    yield {"type": "content", "delta": text}
            elif etype == "response.output_item.done":
                item = event.item
                if getattr(item, "type", None) == "function_call" and getattr(item, "arguments", None):
                    arguments = item.arguments  # 以完整 arguments 为准,纠正增量累积的误差
            elif etype == "response.failed":
                error = getattr(event.response, "error", None)
                raise RuntimeError(f"Responses API 失败: {error}")
            # response.completed / response.incomplete: 流结束,用已累积内容收尾

    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        args = {"content": arguments}
    options = args.get("options")
    if not isinstance(options, list):
        options = []
    yield {"type": "done", "content": args.get("content", ""), "options": [str(o) for o in options], "reasoning": reasoning_full}
