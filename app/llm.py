"""大模型 API 流式调用封装，支持 Responses API 与 Chat Completions API。

一轮回复分两段，都是普通请求：
1. 正文：模型可多次调用启用的工具（执行结果追加进输入继续请求），正文是普通文本输出，
   某一轮不再调用工具即视为说完，回合结束。
2. 选项：正文结束后用 generate_options 再发一次请求，让它只产出选项 JSON。这次请求必须
   与正文请求共用同一份 config（同 model / tools / thinking 配置、不加 response_format），
   否则端点侧的前缀缓存整体失效（实测：这三者任一变化，命中率从 98% 掉到 6%）。
"""
import json
import logging

from openai import AsyncOpenAI

from app import core
from app.core import get_tools

logger = logging.getLogger("airp.llm")

# 一轮回复中工具循环的轮数上限，防止模型陷入无限工具调用
MAX_ROUNDS = 25


# 选项请求的输出上限（选项很短）。只影响输出、不参与前缀缓存，所以可以随便压。
OPTIONS_MAX_TOKENS = 2048

# 选项请求第一次没给出合法 JSON 时追加的提醒：作为上下文末尾的续写，不影响前缀缓存。
_OPTIONS_RETRY_HINT = """
上一次输出不是合法的 JSON 对象。请输出一个严格的 JSON 对象，形如
```
{"options": ["选项\"一\"", "选项\"二\""]}
```
"""


def _make_client(config: dict) -> AsyncOpenAI:
    """构造 OpenAI 客户端，按需附加风控要求的请求头。

    - user_agent：覆盖 SDK 默认的 "OpenAI/Python ..." UA（部分端点风控拒绝语言默认 UA）
    - x_opencode_session + chat_id：opencode-go 要求每个对话带固定的 UUID v4 会话头
      （同一对话所有请求同一个值，用于 GPU KV 缓存亲和调度）
    """
    headers = {}
    if config.get("user_agent"):
        headers["User-Agent"] = config["user_agent"]
    if config.get("x_opencode_session") and config.get("chat_id"):
        headers["X-Opencode-Session"] = config["chat_id"]
    return AsyncOpenAI(
        api_key=config["api_key"],
        base_url=config["api_base"].rstrip("/"),
        timeout=600.0,
        default_headers=headers or None,
    )


def _mapped_effort(config: dict):
    """该端点实际要发的 reasoning_effort：端点可配 reasoning_effort_map 给它改名。

    OpenRouter 的 effort 枚举只到 xhigh（另有 minimal），没有本项目的 max，配置里写
    `reasoning_effort_map: {max: xhigh}` 就落在这里。只认端点自己那份 config，不影响别的端点。
    """
    effort = config.get("reasoning_effort")
    if effort is None:
        return None
    return (config.get("reasoning_effort_map") or {}).get(effort, effort)


def _extra_body(config: dict, fields: dict):
    """SDK 不认的字段只能经 extra_body 透传（直接当 kwarg 传会 TypeError），没有内容时返回 None。

    - provider：OpenRouter 的路由偏好（排序、最低吞吐），端点里配了才发
    - session_id：端点开了 session_id 就把本会话的 chat_id 发过去——OpenRouter 拿它做粘性路由
      与上游缓存亲和（正文与选项两条请求才可能落到同一个上游、命中同一份前缀缓存）
    - fields：调用方按需附上的厂商私有参数（如禁用思考的 thinking.disabled）
    """
    extra = {}
    if config.get("provider"):
        extra["provider"] = config["provider"]
    if config.get("session_id") and config.get("chat_id"):
        extra["session_id"] = config["chat_id"]
    extra.update(fields)
    return extra or None


def build_request(input_items: list, config: dict) -> dict:
    """构造实际发送给模型的请求参数（/api/preview 也用它展示上下文）。

    工具 schema 来自 config 里的预设工具配置（core.apply_preset_tools 写入的
    ptc/tools/bindings）：ptc 模式只含 world_run，普通模式是白名单里的直接工具。
    选项请求（generate_options）也走这个函数：同一份 config 才能命中前缀缓存。
    """
    api_type = config.get("api_type", "responses")
    ptc = bool(config.get("ptc"))
    direct = config.get("tools")
    if not isinstance(direct, list):
        raise ValueError(
            f"config 缺少启用的工具名单 tools: {direct!r}（应由 core.apply_preset_tools 写入）"
        )
    schemas = get_tools(api_type, ptc, direct, config.get("bindings") or [])
    if api_type == "chat_completions":
        payload = {
            "model": config["model"],
            "messages": input_items,
            "stream": True,
        }
        # 白名单为空时整个工具字段都不发（空 tools 数组会被部分端点拒绝）
        if schemas:
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        if config.get("temperature") is not None:
            payload["temperature"] = config["temperature"]
        if config.get("max_tokens") is not None:
            payload["max_tokens"] = config["max_tokens"]
        effort = _mapped_effort(config)
        fields = {}
        if effort == "none":
            # 禁用思考要发厂商私有的 thinking.disabled：SDK 不认这个参数名（当 kwarg 传直接
            # TypeError），只能塞 extra_body 透传。实测 deepseek 官方、OpenRouter、kimicode 都
            # 认它；反过来 OpenRouter 上 reasoning.effort / enabled = none 会 400（部分端强制思考）。
            fields["thinking"] = {"type": "disabled"}
        elif effort is not None:
            payload["reasoning_effort"] = effort
        extra = _extra_body(config, fields)
        if extra:
            payload["extra_body"] = extra
        return payload

    payload = {"model": config["model"], "input": input_items}
    # 白名单为空时整个工具字段都不发（空 tools 数组会被部分端点拒绝）
    if schemas:
        payload["tools"] = schemas
        payload["tool_choice"] = "auto"
    if config.get("temperature") is not None:
        payload["temperature"] = config["temperature"]
    if config.get("max_tokens") is not None:
        payload["max_output_tokens"] = config["max_tokens"]
    effort = _mapped_effort(config)
    fields = {}
    if effort == "none":
        # 禁用思考要发厂商私有的 thinking.disabled：SDK 不认这个参数名（当 kwarg 传直接
        # TypeError），只能塞 extra_body 透传。实测 deepseek 官方、OpenRouter、kimicode 都
        # 认它；反过来 OpenRouter 上 reasoning.effort / enabled = none 会 400（部分端强制思考）。
        fields["thinking"] = {"type": "disabled"}
    elif effort is not None:
        payload["reasoning"] = {"effort": effort}
    extra = _extra_body(config, fields)
    if extra:
        payload["extra_body"] = extra
    return payload


async def stream_body(input_items: list, config: dict, run_tool):
    """根据 config['api_type'] 选择 Responses API 或 Chat Completions API 流式跑正文。

    两种模式对外 yield 的事件格式一致（选项不在这里，见 generate_options）：
      {"type": "reasoning", "delta": str}
      {"type": "content", "delta": str}
      {"type": "tool", "name": str, "arguments": str, "result": str, "reasoning": str}
      {"type": "done", "content": str, "reasoning": str}
    """
    api_type = config.get("api_type", "responses")
    if api_type == "chat_completions":
        async for event in _stream_body_chat_completions(input_items, config, run_tool):
            yield event
        return
    async for event in _stream_body_responses(input_items, config, run_tool):
        yield event


def _note_reasoning(reasonings: dict, round_output: list, item_id) -> None:
    """按第一次出现的位置记录一个 reasoning 项（只记一次，文本后填）。"""
    if item_id not in reasonings:
        reasonings[item_id] = ""
        round_output.append({"kind": "reasoning", "ref": item_id})


async def _stream_body_responses(input_items: list, config: dict, run_tool):
    """Responses API 流式调用实现（内含工具循环，某一轮没有工具调用即回合结束）。"""
    # 思维链按模型实际产生的那一项回放，位置照原样；但 deepseek-flash 的 /responses 还要求
    # 「每个 function_call 前都有各自的一份 reasoning_text」，缺了直接 400 —— 实测一轮里两个
    # function_call 只回放一份真文本会 400，连它自己产出的 trace 原样回放也会 400。
    # 所以每轮按调用逐个护航：轮首那份真实思考算第一份，其后每个调用再补一个单空格占位
    # （已实测合法；空字符串不合法），不重复长文本。老的写法是把整轮思考在每个项前各挂一份，
    # 约束是对的，但同一段长文本会被重复喂回去好几遍。
    last_reasoning = ""  # 本回合最近一轮的非空思考，仅用于前端展示与收尾事件的兜底
    PLACEHOLDER = " "
    async with _make_client(config) as client:
        content = ""  # 整个回合累积的正文(跨轮次,全部对用户可见)
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""  # 本轮所有 reasoning 项的文本拼接（展示用）
            reasonings = {}  # reasoning 项 id -> 该项自己的思考文本
            calls = []  # 本轮的 function_call: [{"call_id", "name", "arguments"}]
            by_item = {}  # function_call item_id -> calls 中的下标
            texts = {}  # message item_id -> 本轮该消息项累积的文本
            round_output = []
            stream = await client.responses.create(**build_request(input_items, config), stream=True)
            async for event in stream:
                etype = event.type
                if etype == "response.reasoning_text.delta":
                    round_reasoning += event.delta
                    _note_reasoning(reasonings, round_output, event.item_id)
                    reasonings[event.item_id] += event.delta
                    yield {"type": "reasoning", "delta": event.delta}
                elif etype == "response.reasoning_text.done":
                    # done 带完整文本（有的实现只发 done 不发 delta），以它为准
                    _note_reasoning(reasonings, round_output, event.item_id)
                    reasonings[event.item_id] = getattr(event, "text", None) or reasonings[event.item_id]
                elif etype == "response.output_item.added":
                    item = event.item
                    itype = getattr(item, "type", None)
                    if itype == "reasoning":
                        _note_reasoning(reasonings, round_output, item.id)
                    elif itype == "function_call":
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
                    if event.item_id not in texts:
                        # 少数实现不发 message 项的 added 事件；不补登记的话这段正文就进不了
                        # 本轮的 input（用户仍能看到——它已经进了 content）
                        texts[event.item_id] = ""
                        round_output.append({"kind": "text", "ref": event.item_id})
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

            n_reasoning = 0  # 本轮已追加的 reasoning 项数
            n_calls = 0  # 本轮已追加的 function_call 数
            for out in round_output:
                if out["kind"] == "reasoning":
                    input_items.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text",
                                     "text": reasonings.get(out["ref"]) or PLACEHOLDER}],
                    })
                    n_reasoning += 1
                elif out["kind"] == "text":
                    text = texts.get(out["ref"], "")
                    if text:
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                else:
                    c = out["call"]
                    result = await run_tool(c["name"], c["arguments"])
                    call_reasoning = round_reasoning or last_reasoning or PLACEHOLDER
                    yield {"type": "tool", "name": c["name"], "arguments": c["arguments"],
                           "result": result, "reasoning": call_reasoning}
                    if n_reasoning <= n_calls:
                        # 这个调用还轮不到一份 reasoning（每个 function_call 都要各自的一份）
                        input_items.append({
                            "type": "reasoning",
                            "content": [{"type": "reasoning_text", "text": PLACEHOLDER}],
                        })
                        n_reasoning += 1
                    n_calls += 1
                    input_items.append({
                        "type": "function_call",
                        "call_id": c["call_id"],
                        "name": c["name"],
                        "arguments": c["arguments"],
                    })
                    input_items.append({"type": "function_call_output", "call_id": c["call_id"], "output": result})
            if round_reasoning:
                last_reasoning = round_reasoning
            if not calls:
                # 本轮没有任何工具调用 = 正文说完了,回合结束(选项另行请求)
                if not content:
                    raise RuntimeError("模型未输出任何内容")
                yield {"type": "done", "content": content,
                       "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
                return
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")


def _delta_reasoning(delta) -> str:
    """取 chat_completions 流式 delta 里的思考链文本：字段名各家不一，按出现情况自动兜底。

    - reasoning_content：deepseek 与多数 OpenAI 兼容端点的叫法
    - reasoning：OpenRouter 归一化出来的纯文本；它同一块里还会带内容相同的 reasoning_details，
      所以优先认它，否则同一段思考会被拼两遍（实测两块文本逐字相同）
    - reasoning_details：OpenRouter 的结构化块，只在上两个都没给文本时用；取 reasoning.text /
      reasoning.summary 的文本，reasoning.encrypted 只有密文、跳过

    判断依据只有「这个字段有没有文本」，不用按端点配置。
    """
    for key in ("reasoning_content", "reasoning"):
        text = getattr(delta, key, None)
        if isinstance(text, str) and text:
            return text
    details = getattr(delta, "reasoning_details", None)
    if not details:
        return ""
    parts = []
    for item in details:
        if isinstance(item, dict):
            dtype, text = item.get("type"), item.get("text") or item.get("summary")
        else:
            dtype = getattr(item, "type", None)
            text = getattr(item, "text", None) or getattr(item, "summary", None)
        if dtype in ("reasoning.text", "reasoning.summary") and text:
            parts.append(text)
    return "".join(parts)


async def _stream_body_chat_completions(messages: list, config: dict, run_tool):
    """Chat Completions API 流式调用实现（内含工具循环，某一轮没有工具调用即回合结束）。"""
    last_reasoning = ""
    PLACEHOLDER = " "
    async with _make_client(config) as client:
        content = ""  # 整个回合累积的正文（跨轮次）
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""
            round_text = ""  # 本轮新增的正文（回合级累积的是 content）
            calls = []  # 本轮工具调用：[{"id", "type", "name", "arguments"}]
            stream = await client.chat.completions.create(**build_request(messages, config))
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                reasoning = _delta_reasoning(delta)
                if reasoning:
                    round_reasoning += reasoning
                    yield {"type": "reasoning", "delta": reasoning}
                text = getattr(delta, "content", None)
                if text:
                    content += text
                    round_text += text
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

            # 组装本轮 assistant message（可能同时包含正文与 tool_calls）。
            # 只回传本轮新增的正文：content 是跨轮的累积量，整段塞回去会把自己的正文
            # 反复喂进上下文（下一轮看到的会是「我上一轮已经写了一整篇」）。
            assistant_msg = {"role": "assistant"}
            if round_text:
                assistant_msg["content"] = round_text
            # Chat Completions 中 assistant 的 reasoning_content 需要回传
            assistant_msg["reasoning_content"] = reasoning_for_items
            if calls:
                assistant_msg["tool_calls"] = [
                    {"id": c["id"], "type": c["type"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in calls if c["id"]
                ]
            messages.append(assistant_msg)

            for call in calls:
                if not call["id"]:
                    continue
                result = await run_tool(call["name"], call["arguments"])
                yield {"type": "tool", "name": call["name"], "arguments": call["arguments"],
                       "result": result, "reasoning": reasoning_for_items}
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

            if not calls:
                # 本轮没有任何工具调用 = 正文说完了,回合结束(选项另行请求)
                if not content:
                    raise RuntimeError("模型未输出任何内容")
                yield {"type": "done", "content": content, "reasoning": reasoning_for_items}
                return

            # 本轮全是工具调用，进入下一轮
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")


async def generate_options(input_items: list, config: dict) -> tuple:
    """正文结束后的选项请求：用与正文请求**完全相同**的参数再问一次，只要选项 JSON。

    返回 (options, 错误文本, 原始输出)。解析失败就追加一句提醒重试一次；仍失败则选项为空、
    错误文本非空，由调用方落盘成 options_error（正文不受影响）；原始输出非空时由调用方
    落盘成 options_raw，供前端查看模型这次到底产出了什么。

    沿用同一份 config 是硬要求：实测 deepseek 官方端点上 tools 数组、thinking /
    reasoning_effort 三者任一与正文请求不一致，前缀缓存命中率就从 98% 掉到 6%。
    response_format 同样会打断（且该端点不支持 json_schema），所以格式约束写在
    core.OPTIONS_INSTRUCTION 里，靠解析兜底。
    """
    async with _make_client(config) as client:
        error = ""
        attempts: list[tuple[int, str]] = []
        for attempt in range(2):
            items = list(input_items)
            if attempt:
                items.append(_followup_item(config, _OPTIONS_RETRY_HINT))
            try:
                text, problem = await _request_options_once(client, items, config)
            except Exception as e:
                error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
                logger.warning("选项请求失败(第 %d 次): %s", attempt + 1, error)
                continue
            if problem:
                # 输出根本不可能带选项 JSON（模型调了工具 / 被输出上限截断），不必再解析
                error = problem
            else:
                try:
                    return core.parse_options(text), "", ""
                except ValueError as e:
                    error = str(e)
            attempts.append((attempt + 1, text or "（空输出）"))
            logger.warning(
                "选项请求失败(第 %d 次): %s\n模型原始输出:\n%s",
                attempt + 1,
                error,
                text or "（空输出）",
            )
        return [], error, _format_raw_attempts(attempts)


def _format_raw_attempts(attempts: list) -> str:
    """把失败的几次原始输出拼成一段文本：只有一次失败时不加次数标题。"""
    parts = [
        text if len(attempts) == 1 else f"--- 第 {n} 次 ---\n{text}"
        for n, text in attempts
    ]
    return "\n\n".join(p for p in parts if p).strip()


def _followup_item(config: dict, text: str) -> dict:
    """按 api_type 构造追加在末尾的 user 消息（只是续写，不影响前缀缓存）。"""
    if config.get("api_type", "responses") == "chat_completions":
        return {"role": "user", "content": text}
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _usage_note(usage) -> str:
    """把 usage 压成一行日志（缓存命中数的字段名各家不一，取不到就不写）。"""
    if usage is None:
        return ""
    data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    prompt = data.get("prompt_tokens", data.get("input_tokens"))
    cached = data.get("prompt_cache_hit_tokens")
    for key in ("prompt_tokens_details", "input_tokens_details"):
        if cached is None:
            cached = (data.get(key) or {}).get("cached_tokens")
    out = data.get("completion_tokens", data.get("output_tokens"))
    # 思考 token 也占输出上限：选项请求被压空时，这里是第一嫌疑人
    think = None
    for key in ("completion_tokens_details", "output_tokens_details"):
        if think is None:
            think = (data.get(key) or {}).get("reasoning_tokens")
    parts = []
    if prompt is not None:
        parts.append(f"prompt={prompt}")
    if cached is not None:
        parts.append(f"缓存命中={cached}")
    if out is not None:
        parts.append(f"输出={out}")
    if think:
        parts.append(f"其中思考={think}")
    return " ".join(parts)


async def _request_options_once(client, input_items: list, config: dict) -> tuple:
    """发一次非流式的选项请求，返回 (模型输出文本, 说明)。

    说明非空表示这次输出不可能是选项 JSON（模型调了工具、或被输出上限截断成空），
    调用方不再拿它去解析、直接把说明当错误。文本始终是模型这次的真实产出，供排查用。
    """
    api_type = config.get("api_type", "responses")
    payload = build_request(input_items, config)
    payload.pop("stream", None)  # 要完整 JSON，用非流式一次拿到
    limit = min(config.get("max_tokens") or OPTIONS_MAX_TOKENS, OPTIONS_MAX_TOKENS)
    if api_type == "chat_completions":
        payload["max_tokens"] = limit
        resp = await client.chat.completions.create(**payload)
        logger.info("选项请求 %s", _usage_note(getattr(resp, "usage", None)))
        choice = resp.choices[0]
        calls = getattr(choice.message, "tool_calls", None)
        if calls:
            return _render_calls(calls), "模型在选项请求里调用了工具，没有给出 JSON"
        text = choice.message.content or ""
        if not text.strip():
            return "", f"模型没输出任何文本（finish_reason={choice.finish_reason}）"
        return text, ""
    payload["max_output_tokens"] = limit
    resp = await client.responses.create(**payload)
    logger.info("选项请求 %s", _usage_note(getattr(resp, "usage", None)))
    calls = [o for o in (getattr(resp, "output", None) or [])
             if getattr(o, "type", "") == "function_call"]
    if calls:
        return _render_calls(calls), "模型在选项请求里调用了工具，没有给出 JSON"
    text = resp.output_text or ""
    if not text.strip():
        return "", f"模型没输出任何文本（{_response_status(resp)}）"
    return text, ""


def _render_calls(calls) -> str:
    """把工具调用渲染成文本：这是选项请求失败时最该被看到的东西。"""
    lines = []
    for c in calls:
        fn = getattr(c, "function", None)
        name = getattr(fn, "name", None) or getattr(c, "name", "")
        args = getattr(fn, "arguments", None) or getattr(c, "arguments", "")
        lines.append(f"{name}({args})")
    return "\n".join(lines)


def _response_status(resp) -> str:
    """响应为什么没有文本：截断、被拒，还是模型什么都没说。"""
    status = getattr(resp, "status", None) or "unknown"
    details = getattr(resp, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details else None
    return f"status={status}, reason={reason}" if reason else f"status={status}"
