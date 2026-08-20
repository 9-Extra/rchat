"""DeepSeek Responses API 流式调用(含 world_run/read_file 工具循环),以及 respond 工具参数的增量解析。"""
import json

from openai import AsyncOpenAI

from app.core import TOOLS

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
        "tools": TOOLS,
        # 思考模式下不支持强制 tool_choice,用 auto 并由 AIRP 提示词约束工具调用顺序
        "tool_choice": "auto",
    }
    if config.get("temperature") is not None:
        payload["temperature"] = config["temperature"]
    if config.get("max_tokens") is not None:
        payload["max_output_tokens"] = config["max_tokens"]
    if config.get("reasoning_effort") is not None:
        payload["reasoning"] = {"effort": config["reasoning_effort"]}
    return payload


# 一轮回复中工具循环的轮数上限,防止模型陷入无限工具调用
MAX_ROUNDS = 25


async def stream_respond(input_items: list, config: dict, run_tool):
    """调用 Responses API 流式接口,内含工具循环。

    模型在一轮回复中可以先多次调用 world_run/read_file(执行后把调用与结果
    追加进 input 继续请求),最终以 respond 调用结束回合——只有 respond 的
    content 会流式产出。

    run_tool(name, arguments_str) -> str: 执行非 respond 工具调用的回调。

    yield 事件 dict:
      {"type": "reasoning", "delta": str}            思考内容
      {"type": "content", "delta": str}              正文增量(respond 的 content)
      {"type": "tool", "name": str, "arguments": str, "result": str, "reasoning": str}
                                                     工具调用与结果;reasoning 为产生该调用的那一轮思维链
      {"type": "done", "content": str, "options": list, "reasoning": str}
                                                     reasoning 为 respond 轮思维链(该轮没有则取前一轮)
    出错时抛出异常,由调用方转成 error 事件。
    """
    # 本回合内最近一轮非空思维链。模型偶尔在个别轮(甚至整个回合)不输出思维链,而
    # DeepSeek 思考模式仍要求回传时每个 function_call 前都有非空 reasoning,否则 400:
    # 先用前轮思维链兜底,全都没有时只能用单空格占位(已实测合法,空字符串不合法)。
    last_reasoning = ""
    PLACEHOLDER = " "
    # 显式传入 api_key/base_url,不读取 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量
    async with AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["api_base"].rstrip("/"),
        timeout=600.0,
    ) as client:
        for _ in range(MAX_ROUNDS):
            extractor = ContentExtractor()
            round_reasoning = ""
            calls = []  # 本轮的 function_call: [{"call_id", "name", "arguments"}]
            by_item = {}  # item_id -> calls 中的下标
            stream = await client.responses.create(**build_request(input_items, config), stream=True)
            async for event in stream:
                etype = event.type
                if etype == "response.reasoning_text.delta":
                    round_reasoning += event.delta
                    yield {"type": "reasoning", "delta": event.delta}
                elif etype == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", None) == "function_call":
                        by_item[item.id] = len(calls)
                        calls.append({"call_id": item.call_id, "name": item.name, "arguments": ""})
                elif etype == "response.function_call_arguments.delta":
                    idx = by_item.get(event.item_id)
                    if idx is None:
                        continue
                    calls[idx]["arguments"] += event.delta
                    if calls[idx]["name"] == "respond":
                        text = extractor.feed(event.delta)
                        if text:
                            yield {"type": "content", "delta": text}
                elif etype == "response.output_item.done":
                    item = event.item
                    idx = by_item.get(getattr(item, "id", None))
                    # 以完整 arguments 为准,纠正增量累积的误差
                    if idx is not None and getattr(item, "arguments", None):
                        calls[idx]["arguments"] = item.arguments
                elif etype == "response.failed":
                    error = getattr(event.response, "error", None)
                    raise RuntimeError(f"Responses API 失败: {error}")
                # response.completed / response.incomplete: 本轮流结束,用已累积内容收尾

            respond_call = next((c for c in calls if c["name"] == "respond"), None)
            # respond 之前的辅助调用也要执行并记录(顺序即模型意图)
            for c in calls:
                if c is respond_call:
                    break
                result = run_tool(c["name"], c["arguments"])
                # DeepSeek 思考模式: 回传上下文时每个 function_call 前都必须紧跟产生它的
                # 那一轮思维链,同一轮多个调用则重复传同一段(重复是合法的,缺失会 400);
                # 本轮没有思维链时用前轮兜底,整个回合都没有时用占位符
                call_reasoning = round_reasoning or last_reasoning or PLACEHOLDER
                yield {"type": "tool", "name": c["name"], "arguments": c["arguments"],
                       "result": result, "reasoning": call_reasoning}
                if call_reasoning:
                    input_items.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": call_reasoning}],
                    })
                input_items.append({
                    "type": "function_call",
                    "call_id": c["call_id"],
                    "name": c["name"],
                    "arguments": c["arguments"],
                })
                input_items.append({"type": "function_call_output", "call_id": c["call_id"], "output": result})
            if round_reasoning:
                last_reasoning = round_reasoning
            if respond_call is None:
                if not calls:
                    raise RuntimeError("模型未调用任何工具")
                continue  # 本轮全是辅助调用,进入下一轮请求
            try:
                args = json.loads(respond_call["arguments"]) if respond_call["arguments"].strip() else {}
            except json.JSONDecodeError:
                args = {"content": respond_call["arguments"]}
            options = args.get("options")
            if not isinstance(options, list):
                options = []
            yield {"type": "done", "content": args.get("content", ""), "options": [str(o) for o in options], "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
            return
    raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未调用 respond,已中止")
