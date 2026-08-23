"""DeepSeek Responses API 流式调用(含 world_run/read_file 工具循环)。

正文是模型的普通文本输出(output_text 事件流式直出),选项通过 respond 工具提交:
一轮回复 = 可选的多次 world_run/read_file(执行结果追加进 input 继续请求),
最后一轮输出正文并调用 respond 提交选项收尾。
"""
import json

from openai import AsyncOpenAI

from app.core import TOOLS


def build_request(input_items: list, config: dict) -> dict:
    """构造 responses.create 的完整请求参数(/api/preview 也用它展示上下文)。

    input_items 由 core.build_input 直接产出。正文为普通文本输出:
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
    追加进 input 继续请求),最后一轮输出正文(普通文本)并调用 respond 提交
    选项,以 respond 结束回合。正文通过 output_text 事件流式产出。

    模型会随机忘掉「正文」或「选项」其中一边(正文写进思考里 / 只顾写正文忘了
    respond),对此有自动修复:respond 契约校验失败时把调用作为工具错误回传让
    模型补齐;纯文本收尾(没调 respond)时在正文后追加 developer 元指令让模型
    补上 respond(该消息不落盘);修复次数用尽才宽容接受为无选项结束。

    run_tool(name, arguments_str) -> str: 执行非 respond 工具调用的回调。

    yield 事件 dict:
      {"type": "reasoning", "delta": str}            思考内容
      {"type": "content", "delta": str}              正文增量(普通文本输出)
      {"type": "tool", "name": str, "arguments": str, "result": str, "reasoning": str}
                                                     工具调用与结果;reasoning 为产生该调用的那一轮思维链
      {"type": "done", "content": str, "options": list, "reasoning": str}
                                                     reasoning 为收尾轮思维链(该轮没有则取前一轮)
    出错时抛出异常,由调用方转成 error 事件。
    """
    # 本回合内最近一轮非空思维链。模型偶尔在个别轮(甚至整个回合)不输出思维链,而
    # 工具循环中场(请求以 function_call_output 结尾)仍要求每个 function_call 前都
    # 有非空 reasoning,否则 400:先用前轮思维链兜底,全都没有时只能用单空格占位
    # (已实测合法,空字符串不合法)。
    last_reasoning = ""
    PLACEHOLDER = " "
    # 显式传入 api_key/base_url,不读取 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量
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
            # 本轮输出项按流内顺序: {"kind": "text", "ref": item_id} / {"kind": "call", "call": dict}
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
                    # 以完整 arguments 为准,纠正增量累积的误差
                    if idx is not None and getattr(item, "arguments", None):
                        calls[idx]["arguments"] = item.arguments
                elif etype == "response.failed":
                    error = getattr(event.response, "error", None)
                    raise RuntimeError(f"Responses API 失败: {error}")
                # response.completed / response.incomplete: 本轮流结束,用已累积内容收尾

            respond_call = next((c for c in calls if c["name"] == "respond"), None)
            # 按流内顺序把本轮输出回放进 input,继续工具循环:正文 -> message 项;
            # respond 之前的辅助调用要执行并记录,调用前置本轮思维链(见上方 400 说明)
            for out in round_output:
                if out["kind"] == "text":
                    text = texts.get(out["ref"], "")
                    if text:
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                else:
                    c = out["call"]
                    if c is respond_call:
                        break  # respond 不执行、不回放,回合到此结束
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
                # 防御:模型偶发把正文写进 respond 参数(旧习惯)。没有文本流时用它兜底,
                # 补发一个 content 事件让前端立即显示(落盘用 done.content,不受影响)
                if not content and isinstance(args.get("content"), str) and args["content"]:
                    content = args["content"]
                    yield {"type": "content", "delta": content}
                # respond 契约校验:正文与选项缺一不可。模型会随机忘掉其中一边
                # (正文写进思考里 / 只顾写正文忘了选项)。把这次调用作为工具错误
                # 回传,让模型补齐后重新收尾——残缺回合一旦落盘会被模仿倾向放大。
                # 修复次数用尽仍失败才报错;空选项提示过一次后再传空数组视为有意。
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
                # 纯文本收尾(模型没调 respond):修复——请求以 assistant 正文结尾(前插
                # 本轮思维链,实测 API 要求其前紧跟 reasoning),再追加一条 developer
                # 元指令要求补 respond(实测模型会照做)。该消息只存在于本次工具循环,
                # 不进落盘的 history,下次请求由 build_input 重新拼装,无污染。
                # 修复次数用尽仍失败才宽容接受为无选项结束。
                if respond_repairs >= 2:
                    yield {"type": "done", "content": content, "options": [],
                           "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
                    return
                respond_repairs += 1
                input_items.insert(len(input_items) - 1, {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text",
                                 "text": round_reasoning or last_reasoning or PLACEHOLDER}],
                })
                input_items.append({
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text",
                                 "text": "（系统提示：正文已收到。你还没有调用 respond——请立即调用 respond 提交剧情推进选项以完成本轮，不要再输出更多正文。）"}],
                })
                continue
            # 本轮全是辅助调用,进入下一轮请求
    raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")
