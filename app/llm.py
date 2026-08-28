"""大模型 API 流式调用封装，支持 Responses API 与 Chat Completions API。

正文是模型的普通文本输出，选项通过 respond 工具提交：
一轮回复 = 可选的多次 world_run/read_file（执行结果追加进输入继续请求），
最后一轮输出正文并调用 respond 提交选项收尾。
"""
import json

from openai import AsyncOpenAI

from app.core import get_tools


# 一轮回复中工具循环的轮数上限，防止模型陷入无限工具调用
MAX_ROUNDS = 25


def build_request(input_items: list, config: dict) -> dict:
    """构造实际发送给模型的请求参数（/api/preview 也用它展示上下文）。"""
    api_type = config.get("api_type", "responses")
    if api_type == "chat_completions":
        payload = {
            "model": config["model"],
            "messages": input_items,
            "tools": get_tools("chat_completions"),
            "tool_choice": "auto",
            "stream": True,
        }
        if config.get("temperature") is not None:
            payload["temperature"] = config["temperature"]
        if config.get("max_tokens") is not None:
            payload["max_tokens"] = config["max_tokens"]
        # Chat Completions 通常不支持 reasoning_effort，保留 temperature/max_tokens 即可
        return payload

    payload = {
        "model": config["model"],
        "input": input_items,
        "tools": get_tools("responses"),
        "tool_choice": "auto",
    }
    if config.get("temperature") is not None:
        payload["temperature"] = config["temperature"]
    if config.get("max_tokens") is not None:
        payload["max_output_tokens"] = config["max_tokens"]
    if config.get("reasoning_effort") is not None:
        payload["reasoning"] = {"effort": config["reasoning_effort"]}
    return payload


async def stream_respond(input_items: list, config: dict, run_tool):
    """根据 config['api_type'] 选择 Responses API 或 Chat Completions API 流式调用。

    两种模式对外 yield 的事件格式一致：
      {"type": "reasoning", "delta": str}
      {"type": "content", "delta": str}
      {"type": "tool", "name": str, "arguments": str, "result": str, "reasoning": str}
      {"type": "done", "content": str, "options": list, "reasoning": str}
    """
    api_type = config.get("api_type", "responses")
    if api_type == "chat_completions":
        async for event in _stream_respond_chat_completions(input_items, config, run_tool):
            yield event
        return
    async for event in _stream_respond_responses(input_items, config, run_tool):
        yield event


async def _stream_respond_responses(input_items: list, config: dict, run_tool):
    """Responses API 流式调用实现（内含工具循环）。"""
    # 本回合内最近一轮非空思维链。模型偶尔在个别轮(甚至整个回合)不输出思维链,而
    # 工具循环中场(请求以 function_call_output 结尾)仍要求每个 function_call 前都
    # 有非空 reasoning,否则 400:先用前轮思维链兜底,全都没有时只能用单空格占位
    # (已实测合法,空字符串不合法)。
    last_reasoning = ""
    PLACEHOLDER = " "
    async with AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["api_base"].rstrip("/"),
        timeout=600.0,
    ) as client:
        content = ""  # 整个回合累积的正文(跨轮次,全部对用户可见)
        respond_repairs = 0  # respond 契约校验失败的自动修复次数(把调用作为工具错误回传)
        options_confirmed = False  # 空选项已提示过一次:再传空数组视为模型有意为之
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""
            calls = []  # 本轮的 function_call: [{"call_id", "name", "arguments"}]
            by_item = {}  # function_call item_id -> calls 中的下标
            texts = {}  # message item_id -> 本轮该消息项累积的文本
            round_output = []
            stream = await client.responses.create(**build_request(input_items, config), stream=True)
            async for event in stream:
                etype = event.type
                if etype == "response.reasoning_text.delta":
                    round_reasoning += event.delta
                    yield {"type": "reasoning", "delta": event.delta}
                elif etype == "response.output_item.added":
                    item = event.item
                    itype = getattr(item, "type", None)
                    if itype == "function_call":
                        by_item[item.id] = len(calls)
                        c = {"call_id": item.call_id, "name": item.name, "arguments": ""}
                        calls.append(c)
                        round_output.append({"kind": "call", "call": c})
                    elif itype == "message":
                        texts[item.id] = ""
                        round_output.append({"kind": "text", "ref": item.id})
                elif etype == "response.output_text.delta":
                    content += event.delta
                    yield {"type": "content", "delta": event.delta}
                    if event.item_id in texts:
                        texts[event.item_id] += event.delta
                elif etype == "response.function_call_arguments.delta":
                    idx = by_item.get(event.item_id)
                    if idx is None:
                        continue
                    calls[idx]["arguments"] += event.delta
                elif etype == "response.output_item.done":
                    item = event.item
                    idx = by_item.get(getattr(item, "id", None))
                    if idx is not None and getattr(item, "arguments", None):
                        calls[idx]["arguments"] = item.arguments
                elif etype == "response.failed":
                    error = getattr(event.response, "error", None)
                    raise RuntimeError(f"Responses API 失败: {error}")

            respond_call = next((c for c in calls if c["name"] == "respond"), None)
            for out in round_output:
                if out["kind"] == "text":
                    text = texts.get(out["ref"], "")
                    if text:
                        call_reasoning = round_reasoning or last_reasoning or PLACEHOLDER
                        if call_reasoning:
                            input_items.append({
                                "type": "reasoning",
                                "content": [{"type": "reasoning_text", "text": call_reasoning}],
                            })
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                else:
                    c = out["call"]
                    if c is respond_call:
                        break
                    result = run_tool(c["name"], c["arguments"])
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
            if respond_call is not None:
                try:
                    args = json.loads(respond_call["arguments"]) if respond_call["arguments"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                options = args.get("options")
                if not isinstance(options, list):
                    options = []
                options = [str(o) for o in options]
                if not content and isinstance(args.get("content"), str) and args["content"]:
                    content = args["content"]
                    yield {"type": "content", "delta": content}
                problems = []
                if not content:
                    problems.append("正文为空，用户什么都看不到（思考内容用户不可见，正文必须作为普通文本输出）")
                if not options and not options_confirmed:
                    problems.append("选项为空，请提供剧情推进选项；若确实没有合适的选项，再次传入空数组即可")
                if problems:
                    if respond_repairs >= 2:
                        raise RuntimeError("模型多次未能完成「正文+选项」的完整回复，已中止")
                    respond_repairs += 1
                    if not options:
                        options_confirmed = True
                    call_reasoning = round_reasoning or last_reasoning or PLACEHOLDER
                    if call_reasoning:
                        input_items.append({
                            "type": "reasoning",
                            "content": [{"type": "reasoning_text", "text": call_reasoning}],
                        })
                    input_items.append({
                        "type": "function_call",
                        "call_id": respond_call["call_id"],
                        "name": "respond",
                        "arguments": respond_call["arguments"],
                    })
                    input_items.append({
                        "type": "function_call_output",
                        "call_id": respond_call["call_id"],
                        "output": "错误：" + "；".join(problems) + "。请补齐后重新调用 respond。",
                    })
                    continue
                yield {"type": "done", "content": content, "options": options,
                       "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
                return
            if not calls:
                if not content:
                    raise RuntimeError("模型未输出任何内容")
                if respond_repairs >= 2:
                    yield {"type": "done", "content": content, "options": [],
                           "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
                    return
                respond_repairs += 1
                input_items.append({
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text",
                                 "text": "（系统提示：正文已收到。你还没有调用 respond——请立即调用 respond 提交剧情推进选项以完成本轮，不要再输出更多正文。）"}],
                })
                continue
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")


async def _stream_respond_chat_completions(messages: list, config: dict, run_tool):
    """Chat Completions API 流式调用实现（内含工具循环）。"""
    last_reasoning = ""
    PLACEHOLDER = " "
    async with AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["api_base"].rstrip("/"),
        timeout=600.0,
    ) as client:
        content = ""  # 整个回合累积的正文（跨轮次）
        respond_repairs = 0
        options_confirmed = False
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""
            calls = []  # 本轮工具调用：[{"id", "type", "name", "arguments"}]
            stream = await client.chat.completions.create(**build_request(messages, config))
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    round_reasoning += reasoning
                    yield {"type": "reasoning", "delta": reasoning}
                text = getattr(delta, "content", None)
                if text:
                    content += text
                    yield {"type": "content", "delta": text}
                for tc in delta.tool_calls or []:
                    idx = tc.index
                    while len(calls) <= idx:
                        calls.append({"id": None, "type": "function", "name": "", "arguments": ""})
                    call = calls[idx]
                    if tc.id:
                        call["id"] = tc.id
                    func = tc.function
                    if func:
                        if func.name:
                            call["name"] = func.name
                        if func.arguments:
                            call["arguments"] += func.arguments

            if round_reasoning:
                last_reasoning = round_reasoning

            reasoning_for_items = round_reasoning or last_reasoning or PLACEHOLDER

            # 组装本轮 assistant message（可能同时包含正文与 tool_calls）
            assistant_msg = {"role": "assistant"}
            if content:
                assistant_msg["content"] = content
            # Chat Completions 中 assistant 的 reasoning_content 需要回传
            assistant_msg["reasoning_content"] = reasoning_for_items
            if calls:
                assistant_msg["tool_calls"] = [
                    {"id": c["id"], "type": c["type"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in calls if c["id"]
                ]
            messages.append(assistant_msg)

            # 执行非 respond 工具调用
            respond_call = None
            for call in calls:
                if call["name"] == "respond":
                    respond_call = call
                    continue
                result = run_tool(call["name"], call["arguments"])
                yield {"type": "tool", "name": call["name"], "arguments": call["arguments"],
                       "result": result, "reasoning": reasoning_for_items}
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

            if respond_call is not None:
                try:
                    args = json.loads(respond_call["arguments"]) if respond_call["arguments"].strip() else {}
                except json.JSONDecodeError:
                    args = {}
                options = args.get("options")
                if not isinstance(options, list):
                    options = []
                options = [str(o) for o in options]
                problems = []
                if not content:
                    problems.append("正文为空，用户什么都看不到（思考内容用户不可见，正文必须作为普通文本输出）")
                if not options and not options_confirmed:
                    problems.append("选项为空，请提供剧情推进选项；若确实没有合适的选项，再次传入空数组即可")
                if problems:
                    if respond_repairs >= 2:
                        raise RuntimeError("模型多次未能完成「正文+选项」的完整回复，已中止")
                    respond_repairs += 1
                    if not options:
                        options_confirmed = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": respond_call["id"],
                        "content": "错误：" + "；".join(problems) + "。请补齐后重新调用 respond。",
                    })
                    continue
                yield {"type": "done", "content": content, "options": options, "reasoning": reasoning_for_items}
                return

            if not calls:
                if not content:
                    raise RuntimeError("模型未输出任何内容")
                if respond_repairs >= 2:
                    yield {"type": "done", "content": content, "options": [], "reasoning": reasoning_for_items}
                    return
                respond_repairs += 1
                messages.append({
                    "role": "developer",
                    "content": "（系统提示：正文已收到。你还没有调用 respond——请立即调用 respond 提交剧情推进选项以完成本轮，不要再输出更多正文。）",
                })
                continue

            # 本轮全是辅助工具调用，进入下一轮
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")
