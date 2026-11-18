"""大模型 API 流式调用封装，支持 Responses API 与 Chat Completions API。

一轮回复分两段，都是普通请求：
1. 正文：模型可多次调用启用的工具（执行结果追加进输入继续请求），正文是普通文本输出，
   某一轮不再调用工具即视为说完，回合结束。
2. 选项：正文结束后用 generate_options 再发一次请求，让它只产出选项行（一行一条）。指令
   来自预设的 <preset_options> 块（未定义时回退到 core.OPTIONS_INSTRUCTION），格式要求由
   预设作者按 parse_options 的固定协议写清楚。这次请求必须与正文请求共用同一份 TurnConfig
   （同 model / tools / thinking 配置、不加 response_format），否则端点侧的前缀缓存整体
   失效（实测：这三者任一变化，命中率从 98% 掉到 6%）。
"""
import logging

from app import core
from app.config import TurnConfig
from app.core import get_tools
from app.llm_transport import LLMClient

logger = logging.getLogger("airp.llm")

# 一轮回复中工具循环的轮数上限，防止模型陷入无限工具调用
MAX_ROUNDS = 25


# 选项请求的输出上限（选项很短）。只影响输出、不参与前缀缓存，所以可以随便压。
OPTIONS_MAX_TOKENS = 2048

# 选项请求第一次没给出可解析的选项行时追加的提醒：作为上下文末尾的续写，不影响前缀缓存。
# 它属于 parse_options 的固定协议，不随预设的 <preset_options> 变化；重试只追加、不改前缀。
_OPTIONS_RETRY_HINT = """
上一次输出不是合法的选项列表。请一行一个选项、每行以「- 」开头，除选项行外不要输出任何文字，例如：
- 选项一
- 选项二
"""

# 压缩请求的输出上限（摘要很短，但仍要留够）。只影响输出、不参与前缀缓存。
COMPACT_MAX_TOKENS = 8192

# 压缩请求第一次没产出可用摘要时追加的提醒（同样只追加、不改前缀）。
_COMPACT_RETRY_HINT = """
上一次输出不能当摘要用。请只输出摘要正文：不要调用任何工具，不要标题，不要说明文字。
"""


# 同一端点（连接参数相同）的正文 / 选项 / 压缩请求复用同一个 httpx.AsyncClient：
# 后两次请求省掉 TCP+TLS 握手。上限只是兜底——config.yaml 每改一次连接参数会多一个
# 客户端，正常用不到这么多。
_MAX_CACHED_CLIENTS = 32
_clients: dict[tuple, LLMClient] = {}


def _endpoint_path(ep) -> str:
    return "/chat/completions" if ep.api_type == "chat_completions" else "/responses"


def _client_key(turn: TurnConfig) -> tuple:
    """连接参数快照：api_key / UA / api_type 改了就不能复用旧客户端。"""
    ep = turn.endpoint
    return (ep.api_base, ep.api_key, _endpoint_path(ep), turn.user_agent or "")


class _ClientLease:
    """`_make_client` 的返回值：借出缓存的客户端，退出时不关它。

    调用点仍是 `async with _make_client(turn) as client`，但关闭交给进程退出时的
    `close_clients()`；否则正文结束后连接池就被销毁，选项请求又要重新握手。
    """

    def __init__(self, turn: TurnConfig) -> None:
        self._turn = turn

    async def __aenter__(self) -> LLMClient:
        return await _get_client(self._turn)

    async def __aexit__(self, *exc) -> bool:
        return False


async def _get_client(turn: TurnConfig) -> LLMClient:
    key = _client_key(turn)
    client = _clients.get(key)
    if client is not None:
        return client
    ep = turn.endpoint
    headers = {"User-Agent": turn.user_agent} if turn.user_agent else {}
    client = LLMClient(
        base_url=ep.api_base,
        api_key=ep.api_key,
        path=_endpoint_path(ep),
        headers=headers,
    )
    _clients[key] = client
    while len(_clients) > _MAX_CACHED_CLIENTS:
        old_key = next(iter(_clients))
        old = _clients.pop(old_key)
        logger.warning(
            "LLM 客户端缓存超过 %d 个，关闭最旧的一个: %s", _MAX_CACHED_CLIENTS, old_key[0]
        )
        await old.aclose()
    return client


async def close_clients() -> None:
    """关闭所有缓存客户端（进程退出 / 测试清理用）。"""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.aclose()


def _make_client(turn: TurnConfig) -> _ClientLease:
    """借出到该端点的传输客户端（按端点缓存），返回的是「退出不关闭」的租约。

    - user_agent：客户端级常驻头，覆盖默认的 "rchat/0.1"（部分端点风控拒绝语言默认 UA）
    - x_opencode_session + chat_id：逐请求头（见 `_turn_headers`）——它随会话变，不能
      塞进按端点缓存的客户端
    """
    return _ClientLease(turn)


def _turn_headers(turn: TurnConfig) -> dict | None:
    """只随会话变化、因此必须逐请求附带的头。"""
    ep = turn.endpoint
    if ep.x_opencode_session and turn.chat_id:
        return {"X-Opencode-Session": turn.chat_id}
    return None


def _mapped_effort(turn: TurnConfig):
    """该端点实际要发的 reasoning_effort：端点可配 reasoning_effort_map 给它改名。

    OpenRouter 的 effort 枚举只到 xhigh（另有 minimal），没有本项目的 max，配置里写
    `reasoning_effort_map: {max: xhigh}` 就落在这里。只认端点自己那份配置，不影响别的端点。
    """
    effort = turn.reasoning_effort
    mapping = turn.endpoint.reasoning_effort_map
    if not mapping:
        return effort
    return mapping.get(effort, effort)


def _vendor_fields(turn: TurnConfig, fields: dict) -> dict:
    """端点私有的请求体字段，直接并进请求体（不属于 OpenAI 的标准参数）。

    - provider：OpenRouter 的路由偏好（排序、最低吞吐），端点里配了才发
    - session_id：端点开了 session_id 就把本会话的 chat_id 发过去——OpenRouter 拿它做粘性路由
      与上游缓存亲和（正文与选项两条请求才可能落到同一个上游、命中同一份前缀缓存）
    - fields：调用方按需附上的厂商私有参数（如禁用思考的 thinking.disabled）
    """
    extra = {}
    if turn.endpoint.provider:
        extra["provider"] = turn.endpoint.provider
    if turn.endpoint.session_id and turn.chat_id:
        extra["session_id"] = turn.chat_id
    extra.update(fields)
    return extra


def build_request(input_items: list, turn: TurnConfig) -> dict:
    """构造实际发送给模型的请求体（/api/preview 也用它展示上下文）。

    返回的就是线上 JSON 的字段（含 stream），传输层不再加工。工具 schema 来自 turn 里的
    预设工具配置（core.build_turn 写入的 ptc/tools/bindings）：ptc 模式只含 world_run，
    普通模式是白名单里的直接工具。选项请求（generate_options）也走这个函数：同一份 turn
    才能命中前缀缓存。
    """
    api_type = turn.endpoint.api_type
    schemas = get_tools(api_type, turn.ptc, turn.tools, turn.bindings)
    if api_type == "chat_completions":
        payload = {
            "model": turn.endpoint.model,
            "messages": input_items,
            "stream": True,
        }
        # 白名单为空时整个工具字段都不发（空 tools 数组会被部分端点拒绝）
        if schemas:
            payload["tools"] = schemas
            payload["tool_choice"] = "auto"
        if turn.temperature is not None:
            payload["temperature"] = turn.temperature
        if turn.max_tokens is not None:
            payload["max_tokens"] = turn.max_tokens
        effort = _mapped_effort(turn)
        fields = {}
        if effort == "none":
            # 禁用思考要发厂商私有的 thinking.disabled。实测 deepseek 官方、OpenRouter、
            # kimicode 都认它；反过来 OpenRouter 上 reasoning.effort / enabled = none 会
            # 400（部分端强制思考）。
            fields["thinking"] = {"type": "disabled"}
        elif effort is not None:
            payload["reasoning_effort"] = effort
        payload.update(_vendor_fields(turn, fields))
        return payload

    payload = {"model": turn.endpoint.model, "input": input_items, "stream": True}
    # 白名单为空时整个工具字段都不发（空 tools 数组会被部分端点拒绝）
    if schemas:
        payload["tools"] = schemas
        payload["tool_choice"] = "auto"
    if turn.temperature is not None:
        payload["temperature"] = turn.temperature
    if turn.max_tokens is not None:
        payload["max_output_tokens"] = turn.max_tokens
    effort = _mapped_effort(turn)
    fields = {}
    if effort == "none":
        # 与 chat_completions 相同的厂商私有参数，见上。
        fields["thinking"] = {"type": "disabled"}
    elif effort is not None:
        payload["reasoning"] = {"effort": effort}
    payload.update(_vendor_fields(turn, fields))
    return payload


async def stream_body(input_items: list, turn: TurnConfig, run_tool):
    """按 turn.endpoint.api_type 选择 Responses API 或 Chat Completions API 流式跑正文。

    两种模式对外 yield 的事件格式一致（选项不在这里，见 generate_options）：
      {"type": "reasoning", "delta": str}
      {"type": "content", "delta": str}
      {"type": "tool", "name": str, "arguments": str, "result": str, "reasoning": str}
      {"type": "done", "content": str, "reasoning": str}
    """
    if turn.endpoint.api_type == "chat_completions":
        async for event in _stream_body_chat_completions(input_items, turn, run_tool):
            yield event
        return
    async for event in _stream_body_responses(input_items, turn, run_tool):
        yield event


def _empty_output_error(status: str, *, streamed: bool) -> str:
    """整轮没吐出任何内容的报错文本。

    多数情况是模型被输出上限压空（max_tokens 太小，或思考 token 吃满了额度），这时笼统的
    「模型未输出任何内容」会把人引向错误的排查方向（去看预设、看端点），所以把上游给的
    结束原因一并写出来；确实什么都没说时 status 为空，退回原来的说法。
    """
    where = "流式响应" if streamed else "响应"
    if status:
        return f"模型未输出任何内容（{where} {status}）"
    return "模型未输出任何内容"


def _note_reasoning(reasonings: dict, round_output: list, item_id) -> None:
    """按第一次出现的位置记录一个 reasoning 项（只记一次，文本后填）。"""
    if item_id not in reasonings:
        reasonings[item_id] = ""
        round_output.append({"kind": "reasoning", "ref": item_id})


async def _stream_body_responses(input_items: list, turn: TurnConfig, run_tool):
    """Responses API 流式调用实现（内含工具循环，某一轮没有工具调用即回合结束）。"""
    # 思维链按模型实际产生的那一项回放，位置照原样；但 deepseek-flash 的 /responses 还要求
    # 「每个 function_call 前都有各自的一份 reasoning_text」，缺了直接 400 —— 实测一轮里两个
    # function_call 只回放一份真文本会 400，连它自己产出的 trace 原样回放也会 400。
    # 所以每轮按调用逐个护航：轮首那份真实思考算第一份，其后每个调用再补一个单空格占位
    # （已实测合法；空字符串不合法），不重复长文本。老的写法是把整轮思考在每个项前各挂一份，
    # 约束是对的，但同一段长文本会被重复喂回去好几遍。
    last_reasoning = ""  # 本回合最近一轮的非空思考，仅用于前端展示与收尾事件的兜底
    PLACEHOLDER = " "
    async with _make_client(turn) as client:
        content = ""  # 整个回合累积的正文(跨轮次,全部对用户可见)
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""  # 本轮所有 reasoning 项的文本拼接（展示用）
            reasonings = {}  # reasoning 项 id -> 该项自己的思考文本
            calls = []  # 本轮的 function_call: [{"call_id", "name", "arguments"}]
            by_item = {}  # function_call item_id -> calls 中的下标
            texts = {}  # message item_id -> 本轮该消息项累积的文本
            round_output = []
            cut = ""  # 本轮为什么没跑完（response.incomplete，如 max_output_tokens）
            async for event in client.stream(
                build_request(input_items, turn), headers=_turn_headers(turn)
            ):
                etype = event.get("type")
                if etype == "response.reasoning_text.delta":
                    delta = event.get("delta") or ""
                    item_id = event.get("item_id")
                    round_reasoning += delta
                    _note_reasoning(reasonings, round_output, item_id)
                    reasonings[item_id] += delta
                    yield {"type": "reasoning", "delta": delta}
                elif etype == "response.reasoning_text.done":
                    # done 带完整文本（有的实现只发 done 不发 delta），以它为准
                    item_id = event.get("item_id")
                    _note_reasoning(reasonings, round_output, item_id)
                    reasonings[item_id] = event.get("text") or reasonings[item_id]
                elif etype == "response.output_item.added":
                    item = event.get("item") or {}
                    itype = item.get("type")
                    if itype == "reasoning":
                        _note_reasoning(reasonings, round_output, item.get("id"))
                    elif itype == "function_call":
                        by_item[item.get("id")] = len(calls)
                        c = {"call_id": item.get("call_id"), "name": item.get("name"), "arguments": ""}
                        calls.append(c)
                        round_output.append({"kind": "call", "call": c})
                    elif itype == "message":
                        texts[item.get("id")] = ""
                        round_output.append({"kind": "text", "ref": item.get("id")})
                elif etype == "response.output_text.delta":
                    delta = event.get("delta") or ""
                    item_id = event.get("item_id")
                    content += delta
                    yield {"type": "content", "delta": delta}
                    if item_id not in texts:
                        # 少数实现不发 message 项的 added 事件；不补登记的话这段正文就进不了
                        # 本轮的 input（用户仍能看到——它已经进了 content）
                        texts[item_id] = ""
                        round_output.append({"kind": "text", "ref": item_id})
                    texts[item_id] += delta
                elif etype == "response.function_call_arguments.delta":
                    idx = by_item.get(event.get("item_id"))
                    if idx is None:
                        continue
                    calls[idx]["arguments"] += event.get("delta") or ""
                elif etype == "response.output_item.done":
                    item = event.get("item") or {}
                    idx = by_item.get(item.get("id"))
                    if idx is not None and item.get("arguments"):
                        calls[idx]["arguments"] = item["arguments"]
                elif etype == "response.incomplete":
                    # 被输出上限/内容过滤提前收尾：流照样正常结束，只有这个事件说明原因
                    cut = _response_status(event.get("response") or {})
                elif etype == "response.failed":
                    error = (event.get("response") or {}).get("error")
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
                    raise RuntimeError(_empty_output_error(cut, streamed=True))
                yield {"type": "done", "content": content,
                       "reasoning": round_reasoning or last_reasoning or PLACEHOLDER}
                return
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")


def _delta_reasoning(delta: dict) -> str:
    """取 chat_completions 流式 delta 里的思考链文本：字段名各家不一，按出现情况自动兜底。

    - reasoning_content：deepseek 与多数 OpenAI 兼容端点的叫法
    - reasoning：OpenRouter 归一化出来的纯文本；它同一块里还会带内容相同的 reasoning_details，
      所以优先认它，否则同一段思考会被拼两遍（实测两块文本逐字相同）
    - reasoning_details：OpenRouter 的结构化块，只在上两个都没给文本时用；取 reasoning.text /
      reasoning.summary 的文本，reasoning.encrypted 只有密文、跳过

    判断依据只有「这个字段有没有文本」，不用按端点配置。
    """
    for key in ("reasoning_content", "reasoning"):
        text = delta.get(key)
        if isinstance(text, str) and text:
            return text
    details = delta.get("reasoning_details")
    if not details:
        return ""
    parts = []
    for item in details:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("summary")
        if item.get("type") in ("reasoning.text", "reasoning.summary") and text:
            parts.append(text)
    return "".join(parts)


async def _stream_body_chat_completions(messages: list, turn: TurnConfig, run_tool):
    """Chat Completions API 流式调用实现（内含工具循环，某一轮没有工具调用即回合结束）。"""
    last_reasoning = ""
    PLACEHOLDER = " "
    async with _make_client(turn) as client:
        content = ""  # 整个回合累积的正文（跨轮次）
        for _ in range(MAX_ROUNDS):
            round_reasoning = ""
            round_text = ""  # 本轮新增的正文（回合级累积的是 content）
            calls = []  # 本轮工具调用：[{"id", "type", "name", "arguments"}]
            finish_reason = ""  # 本轮上游给的结束原因（length = 被输出上限截断）
            async for chunk in client.stream(
                build_request(messages, turn), headers=_turn_headers(turn)
            ):
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice.get("finish_reason")
                delta = choice.get("delta") or {}
                reasoning = _delta_reasoning(delta)
                if reasoning:
                    round_reasoning += reasoning
                    yield {"type": "reasoning", "delta": reasoning}
                text = delta.get("content")
                if text:
                    content += text
                    round_text += text
                    yield {"type": "content", "delta": text}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index") or 0
                    while len(calls) <= idx:
                        calls.append({"id": None, "type": "function", "name": "", "arguments": ""})
                    call = calls[idx]
                    if tc.get("id"):
                        call["id"] = tc["id"]
                    func = tc.get("function") or {}
                    if func.get("name"):
                        call["name"] = func["name"]
                    if func.get("arguments"):
                        call["arguments"] += func["arguments"]

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
                    raise RuntimeError(_empty_output_error(
                        f"finish_reason={finish_reason}" if finish_reason else "", streamed=True
                    ))
                yield {"type": "done", "content": content, "reasoning": reasoning_for_items}
                return

            # 本轮全是工具调用，进入下一轮
        raise RuntimeError(f"工具循环超过 {MAX_ROUNDS} 轮仍未结束回复,已中止")


async def generate_summary(input_items: list, turn: TurnConfig) -> tuple[str, str]:
    """压缩请求（/compact）：用与正文请求**完全相同**的参数再问一次，只要一份摘要正文。

    返回 (摘要, 错误文本)；失败不抛异常，由调用方决定「什么都不改」。
    必须与正文请求共用同一份 turn：这次请求是上一次正文请求的严格续写，前缀缓存能整段
    命中，压缩就只付输出的钱；换 model / tools / thinking 任何一样都会让它变成把整段
    上下文重新买一遍。

    输出不像摘要（调了工具、空的）就追加一句提醒重试一次；仍失败则返回错误。
    不设长度下限：短摘要也是摘要，压得太狠是模型的选择，不该当作失败让用户重来。
    """
    error = ""
    async with _make_client(turn) as client:
        for attempt in range(2):
            items = list(input_items)
            if attempt:
                items.append(_followup_item(turn, _COMPACT_RETRY_HINT))
            try:
                text, problem = await _request_compact_once(client, items, turn)
            except Exception as e:
                error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
                logger.warning("压缩请求失败(第 %d 次): %s", attempt + 1, error)
                continue
            stripped = (text or "").strip()
            if not problem:
                return stripped, ""
            error = problem
            logger.warning(
                "压缩请求失败(第 %d 次): %s\n模型原始输出:\n%s", attempt + 1, error, text or "（空输出）"
            )
        return "", error


async def _request_compact_once(client, input_items: list, turn: TurnConfig) -> tuple:
    """发一次非流式的压缩请求，返回 (模型输出文本, 说明)。"""
    api_type = turn.endpoint.api_type
    payload = build_request(input_items, turn)
    payload.pop("stream", None)  # 要完整摘要，用非流式一次拿到
    limit = min(turn.max_tokens or COMPACT_MAX_TOKENS, COMPACT_MAX_TOKENS)
    if api_type == "chat_completions":
        payload["max_tokens"] = limit
    else:
        payload["max_output_tokens"] = limit
    resp = await client.request(payload, headers=_turn_headers(turn))
    logger.info("压缩请求 %s", _usage_note(resp.get("usage")))
    text, calls, why_empty = _non_stream_reply(resp, api_type)
    if calls:
        return _render_calls(calls), "模型在压缩请求里调用了工具，没有给出摘要"
    if not text.strip():
        return "", _empty_output_error(why_empty, streamed=False)
    return text, ""


async def generate_options(input_items: list, turn: TurnConfig) -> tuple:
    """正文结束后的选项请求：用与正文请求**完全相同**的参数再问一次，只要选项列表。

    返回 (options, 错误文本, 原始输出)。解析失败就追加一句提醒重试一次；仍失败则选项为空、
    错误文本非空，由调用方落盘成 options_error（正文不受影响）；原始输出非空时由调用方
    落盘成 options_raw，供前端查看模型这次到底产出了什么。

    沿用同一份 turn 是硬要求：实测 deepseek 官方端点上 tools 数组、thinking /
    reasoning_effort 三者任一与正文请求不一致，前缀缓存命中率就从 98% 掉到 6%。
    response_format 同样会打断（且该端点不支持 json_schema），所以格式约束写在预设的
    <preset_options> 里（未定义时用 core.OPTIONS_INSTRUCTION），靠解析兜底。
    """
    async with _make_client(turn) as client:
        error = ""
        attempts: list[tuple[int, str]] = []
        for attempt in range(2):
            items = list(input_items)
            if attempt:
                items.append(_followup_item(turn, _OPTIONS_RETRY_HINT))
            try:
                text, problem = await _request_options_once(client, items, turn)
            except Exception as e:
                error = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"
                logger.warning("选项请求失败(第 %d 次): %s", attempt + 1, error)
                continue
            if problem:
                # 输出根本不可能带选项行（模型调了工具 / 被输出上限截断），不必再解析
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


def _followup_item(turn: TurnConfig, text: str) -> dict:
    """按 api_type 构造追加在末尾的 user 消息（只是续写，不影响前缀缓存）。"""
    if turn.endpoint.api_type == "chat_completions":
        return {"role": "user", "content": text}
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _usage_note(usage) -> str:
    """把 usage 压成一行日志（缓存命中数的字段名各家不一，取不到就不写）。"""
    if not isinstance(usage, dict):
        return ""
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    cached = usage.get("prompt_cache_hit_tokens")
    for key in ("prompt_tokens_details", "input_tokens_details"):
        if cached is None:
            details = usage.get(key)
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
    out = usage.get("completion_tokens", usage.get("output_tokens"))
    # 思考 token 也占输出上限：选项请求被压空时，这里是第一嫌疑人
    think = None
    for key in ("completion_tokens_details", "output_tokens_details"):
        if think is None:
            details = usage.get(key)
            think = details.get("reasoning_tokens") if isinstance(details, dict) else None
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


async def _request_options_once(client, input_items: list, turn: TurnConfig) -> tuple:
    """发一次非流式的选项请求，返回 (模型输出文本, 说明)。

    说明非空表示这次输出不可能是选项列表（模型调了工具、或被输出上限截断成空），
    调用方不再拿它去解析、直接把说明当错误。文本始终是模型这次的真实产出，供排查用。
    """
    api_type = turn.endpoint.api_type
    payload = build_request(input_items, turn)
    payload.pop("stream", None)  # 要完整输出，用非流式一次拿到
    limit = min(turn.max_tokens or OPTIONS_MAX_TOKENS, OPTIONS_MAX_TOKENS)
    if api_type == "chat_completions":
        payload["max_tokens"] = limit
    else:
        payload["max_output_tokens"] = limit
    resp = await client.request(payload, headers=_turn_headers(turn))
    logger.info("选项请求 %s", _usage_note(resp.get("usage")))
    text, calls, why_empty = _non_stream_reply(resp, api_type)
    if calls:
        return _render_calls(calls), "模型在选项请求里调用了工具，没有给出选项列表"
    if not text.strip():
        return "", _empty_output_error(why_empty, streamed=False)
    return text, ""


def _non_stream_reply(resp: dict, api_type: str) -> tuple[str, list, str]:
    """从非流式响应里取 (正文, 工具调用, 空输出原因)。

    chat_completions 与 responses 的响应形状不同，但两个非流式调用（选项 / 压缩）对它们的
    要求一样：要么给正文，要么说明为什么没有。
    """
    if api_type == "chat_completions":
        choice = (resp.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        why = f"finish_reason={finish}" if finish else ""
        return message.get("content") or "", message.get("tool_calls") or [], why
    calls = [o for o in (resp.get("output") or []) if o.get("type") == "function_call"]
    return _output_text(resp), calls, _response_status(resp)


def _output_text(resp: dict) -> str:
    """Responses API 的 output_text：拼接 message 项里的 output_text 片段。"""
    parts = []
    for item in resp.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                parts.append(part.get("text") or "")
    return "".join(parts)


def _render_calls(calls) -> str:
    """把工具调用渲染成文本：这是选项请求失败时最该被看到的东西。"""
    lines = []
    for c in calls:
        fn = c.get("function") or {}
        name = fn.get("name") or c.get("name") or ""
        args = fn.get("arguments") or c.get("arguments") or ""
        lines.append(f"{name}({args})")
    return "\n".join(lines)


def _response_status(resp: dict) -> str:
    """响应为什么没有文本：截断、被拒，还是模型什么都没说。"""
    status = resp.get("status") or "unknown"
    details = resp.get("incomplete_details") or {}
    reason = details.get("reason") if isinstance(details, dict) else None
    return f"status={status}, reason={reason}" if reason else f"status={status}"
