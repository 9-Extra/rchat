"""FastAPI 服务端：页面、会话管理、流式对话。"""

import asyncio
import json
import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import core, tools, world
from app.llm import build_request, generate_options, stream_body

logger = logging.getLogger("airp")

STATIC = core.ROOT / "app" / "static"

# 单页应用、文件少且常改：禁用缓存，避免浏览器拿旧版 JS
NO_CACHE = {"Cache-Control": "no-cache"}

app = FastAPI(title="AIRP")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/preview.html")
def preview_page():
    return FileResponse(STATIC / "preview.html", headers=NO_CACHE)


# ---------- 资源列表 ----------


@app.get("/api/presets")
def get_presets():
    return [
        {"id": p["id"], "name": p["name"], "description": p["description"]}
        for p in core.load_presets().values()
    ]


@app.get("/api/cards")
def get_cards():
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "description": c["description"],
            "beginnings": [b[:60] for b in c["beginnings"]],
        }
        for c in core.load_cards().values()
    ]


@app.get("/api/sessions")
def get_sessions():
    """侧栏列表：除会话名外，附上角色卡显示名与已运行轮数供前端直接展示。"""
    sessions = core.list_sessions()
    for s in sessions:
        s["card_name"] = s.get("card_name") or s.get("card") or ""
        s["turns"] = core.count_turns(s["name"])
    return sessions


@app.get("/api/endpoints")
def get_endpoints():
    """前端模型下拉用：config.yaml 里定义的端点列表（不含 api_key）。"""
    try:
        eps = core.endpoints(core.load_config())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [
        {"index": i, "display_name": ep["display_name"], "model": ep["model"]}
        for i, ep in enumerate(eps)
    ]


# ---------- 会话管理 ----------


class CreateSession(BaseModel):
    name: str = ""
    preset: str
    card: str
    beginning_index: int | None = None


@app.post("/api/sessions")
def post_session(req: CreateSession):
    name = req.name.strip()
    if not name:
        # 自动生成：角色卡 id + 时间戳（目录名由 core 在落盘时安全化，显示名保持原样）
        name = f"{req.card}-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        return core.create_session(name, req.preset, req.card, req.beginning_index)
    except (ValueError, IndexError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/sessions/{name}")
def get_session(name: str):
    try:
        state = core.load_state(name)
        state["history"] = core.load_history(name)
        config = core.load_config()
        # 返回实际生效值（会话值非法/缺失时回退默认），供前端选中/填显当前配置
        state["reasoning_effort"] = core.resolve_reasoning_effort(
            state.get("reasoning_effort"), config
        )
        state["endpoint"] = core.resolve_endpoint_index(state.get("endpoint"), config)
        state["temperature"] = core.resolve_temperature(state.get("temperature"), config)
        state["max_tokens"] = core.resolve_max_tokens(state.get("max_tokens"), config)
        state["options_enabled"] = core.resolve_options_enabled(
            state.get("options_enabled"), config
        )
        # 正在进行的一轮生成：本轮还没落盘（用户输入、被替换的旧 AI 块），
        # 前端靠这两个字段把界面还原出来，再接 /stream 续看
        gen = _gen_of(name)
        state["generating"] = (
            {"mode": gen["mode"], "user_input": gen["user_input"]} if gen else None
        )
        return state
    except FileNotFoundError:
        raise HTTPException(404, "session 不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/sessions/{name}")
def delete_session(name: str):
    import shutil

    try:
        shutil.rmtree(core._session_dir(name))
    except FileNotFoundError:
        raise HTTPException(404, "session 不存在")
    world.drop(name)
    return {"ok": True}


class SwitchPreset(BaseModel):
    preset: str


@app.post("/api/sessions/{name}/preset")
def switch_preset(name: str, req: SwitchPreset):
    if req.preset not in core.load_presets():
        raise HTTPException(400, "预设不存在")
    state = core.load_state(name)
    state["preset"] = req.preset
    core.save_state(state)
    return state


class SetReasoningEffort(BaseModel):
    effort: str


@app.post("/api/sessions/{name}/reasoning_effort")
def set_reasoning_effort(name: str, req: SetReasoningEffort):
    if req.effort not in core.EFFORT_LEVELS:
        raise HTTPException(
            400, f"非法的思考强度: {req.effort}（可选值: {', '.join(core.EFFORT_LEVELS)}）"
        )
    state = core.load_state(name)
    state["reasoning_effort"] = req.effort
    core.save_state(state)
    return state


class SetEndpoint(BaseModel):
    index: int


@app.post("/api/sessions/{name}/endpoint")
def set_endpoint(name: str, req: SetEndpoint):
    try:
        eps = core.endpoints(core.load_config())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not (0 <= req.index < len(eps)):
        raise HTTPException(400, f"端点下标越界: {req.index}（共 {len(eps)} 个端点）")
    state = core.load_state(name)
    state["endpoint"] = req.index
    core.save_state(state)
    return state


class SetParams(BaseModel):
    # 传 null 清除覆盖（回退全局默认）；未传的键不动
    temperature: float | None = None
    max_tokens: int | None = None


@app.post("/api/sessions/{name}/params")
def set_params(name: str, req: SetParams):
    state = core.load_state(name)
    if "temperature" in req.model_fields_set:
        if req.temperature is None:
            state.pop("temperature", None)
        elif 0 <= req.temperature <= 2:
            state["temperature"] = req.temperature
        else:
            raise HTTPException(400, f"temperature 必须在 0-2 之间: {req.temperature}")
    if "max_tokens" in req.model_fields_set:
        if req.max_tokens is None:
            state.pop("max_tokens", None)
        elif req.max_tokens >= 1:
            state["max_tokens"] = req.max_tokens
        else:
            raise HTTPException(400, f"max_tokens 必须是正整数: {req.max_tokens}")
    core.save_state(state)
    return state


class SetOptions(BaseModel):
    enabled: bool


@app.post("/api/sessions/{name}/options")
def set_options(name: str, req: SetOptions):
    """选项生成开关：关掉后正文照常生成，只是不再发那次选项请求。"""
    state = core.load_state(name)
    state["options_enabled"] = req.enabled
    core.save_state(state)
    return state


class EditBeginning(BaseModel):
    text: str


@app.post("/api/sessions/{name}/beginning")
def edit_beginning(name: str, req: EditBeginning):
    state = core.load_state(name)
    state["beginning_text"] = req.text
    core.save_state(state)
    return state


class EditAI(BaseModel):
    index: int
    content: str
    options: list[str] = []


@app.post("/api/sessions/{name}/edit_ai")
def edit_ai(name: str, req: EditAI):
    history = core.load_history(name)
    if not (0 <= req.index < len(history)) or history[req.index]["role"] != "assistant":
        raise HTTPException(400, "目标不是 AI 块")
    history[req.index]["content"] = req.content
    history[req.index]["options"] = req.options
    core.save_history(name, history)
    return {"ok": True}


class Rollback(BaseModel):
    index: int


@app.post("/api/sessions/{name}/rollback")
def rollback(name: str, req: Rollback):
    history = core.load_history(name)
    if not (0 <= req.index < len(history)) or history[req.index]["role"] != "user":
        raise HTTPException(400, "目标不是用户块")
    text = history[req.index]["content"]
    core.save_history(name, history[: req.index])
    # 世界状态跟着历史回滚到同一位置
    world.sync(name, req.index)
    return {"input": text}


class Fork(BaseModel):
    index: int


@app.post("/api/sessions/{name}/fork")
def fork(name: str, req: Fork):
    try:
        state = core.fork_session(name, req.index)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 世界状态复制到同一断点
    world.fork(name, state["id"], req.index)
    return state


# ---------- 上下文预览 ----------


class Preview(BaseModel):
    session: str
    input: str = ""


@app.post("/api/preview")
def preview(req: Preview):
    state = core.load_state(req.session)
    history = core.load_history(req.session)
    try:
        input_items = core.build_input(state, history, draft=req.input or None)
        # 合并选中端点与会话生成参数覆盖，预览展示实际生效的请求参数
        config = core.effective_config(state, core.load_config())
        # 预设决定的生成模式与工具白名单（ptc / tools / bindings）
        core.apply_preset_tools(config, state)
        config["reasoning_effort"] = core.resolve_reasoning_effort(
            state.get("reasoning_effort"), config
        )
    except ValueError as e:
        # 预设宏执行失败：用户侧错误，返回 400 而非 500
        raise HTTPException(400, str(e))
    # 展示实际发送给 Responses API 的请求参数
    return build_request(input_items, config)


# ---------- 流式对话 ----------

# 每个 session 本轮生成的状态：转发（events/wake）、重连续看（重放 events）、打断（task）
_active: dict = {}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _gen_of(name: str) -> dict | None:
    """该会话当前正在进行的生成；没在跑（或刚结束）时为 None。"""
    gen = _active.get(name)
    return None if gen is None or gen["finished"] else gen


def _emit(gen: dict, event: dict) -> None:
    """向本轮事件缓冲追加一个事件，并唤醒正在转发的请求。"""
    gen["events"].append(_sse(event))
    gen["wake"].set()


async def _tail(gen: dict):
    """把本轮已产出的事件转发给这个客户端。

    客户端断开只打断本请求(Starlette 取消的是请求任务),生成任务不受影响;
    重连时从头重放即可,所以不需要偏移量。
    """
    i = 0
    while True:
        gen["wake"].clear()  # 先清后查，避免漏掉刚好追加的事件
        while i < len(gen["events"]):
            yield gen["events"][i]
            i += 1
        if gen["finished"]:
            return
        try:
            await asyncio.wait_for(gen["wake"].wait(), timeout=15)
        except asyncio.TimeoutError:
            # 心跳：长思考/慢接口期间别让手机 NAT 掐掉空闲连接
            yield ": ping\n\n"


def _persist(name, history, mode, user_input, draft, content, options, reasoning,
             tool_calls, error=None, options_error=None):
    if mode == "chat":
        history.append({"role": "user", "content": user_input})
    elif mode == "regenerate" and draft is not None:
        history.append({"role": "user", "content": draft})
    entry = {
        "role": "assistant",
        "content": content,
        "options": options,
        "reasoning": reasoning,
    }
    # 生成失败时落盘的错误说明(前端显示;build_input 回放时忽略,不进模型上下文)
    if error:
        entry["error"] = error
    # 选项请求失败:正文照常保留,只标注选项没拿到,前端据此给「重新生成选项」
    if options_error:
        entry["options_error"] = options_error
    # 本轮的 world_run/read_file 调用,重放上下文时用
    if tool_calls:
        entry["tool_calls"] = tool_calls
    history.append(entry)
    core.save_history(name, history)


def _prepare(name: str, mode: str, user_input: str | None) -> dict:
    """流式开始前的全部准备与校验；用户侧错误以 ValueError 抛出。"""
    state = core.load_state(name)
    history = core.load_history(name)
    draft = None
    if mode == "start":
        if history:
            raise ValueError("会话已开始，不能再次开局")
    elif mode == "chat":
        if not history or history[-1]["role"] != "assistant":
            raise ValueError("当前不能发送：没有待回复的 AI 块")
        draft = user_input
    else:  # regenerate：丢弃最后一个 AI 块，用它回复的用户输入重新生成
        if history and history[-1]["role"] == "assistant":
            history.pop()
        if history and history[-1]["role"] == "user":
            draft = history.pop()["content"]
    # 世界状态对齐到当前历史长度（重生成/回滚后状态跟着回退；
    # 上一轮被打断时丢弃未提交的内存改动）
    world.sync(name, len(history))
    input_items = core.build_input(state, history, draft=draft)
    # 合并选中端点与会话生成参数覆盖，得到 llm 使用的扁平 config
    config = core.effective_config(state, core.load_config())
    # 会话标识随 config 传给 llm，作为 X-Opencode-Session 请求头
    config["chat_id"] = state.get("chat_id", "")
    # PTC 实验模式与工具白名单由预设 frontmatter 决定（ptc / tools）
    core.apply_preset_tools(config, state)
    # 思考强度按会话覆盖（会话值非法/缺失时回退 config 默认；非法默认在此报错）
    config["reasoning_effort"] = core.resolve_reasoning_effort(
        state.get("reasoning_effort"), config
    )
    return {
        "name": name,
        "mode": mode,
        "user_input": user_input,
        "state": state,
        "history": history,
        "draft": draft,
        "input_items": input_items,
        "config": config,
        # 正文落盘后要不要再发一次选项请求（会话开关，见 core.resolve_options_enabled）
        "options_enabled": core.resolve_options_enabled(state.get("options_enabled"), config),
    }


async def _run(gen: dict, prep: dict) -> None:
    """本轮生成的生产者：事件写进 gen["events"]，结束时（被打断/出错时亦然）落盘。"""
    name = prep["name"]
    mode = prep["mode"]
    user_input = prep["user_input"]
    draft = prep["draft"]
    history = prep["history"]
    tool_calls = []
    partial_content = ""
    # 当前未完成轮(自上次工具事件后)累积的思维链;打断落盘时作为最后一轮的 reasoning
    tail_reasoning = ""
    streaming_started = False
    try:
        done = None
        streaming_started = True
        options: list = []
        options_error = None
        async def run_tool(tool_name, arguments):
            return await tools.execute_tool(name, tool_name, arguments)
        async for event in stream_body(prep["input_items"], prep["config"], run_tool):
            if event["type"] == "done":
                done = event
            elif event["type"] == "content":
                partial_content += event["delta"]
            elif event["type"] == "reasoning":
                tail_reasoning += event["delta"]
            elif event["type"] == "tool":
                tool_calls.append({
                    "name": event["name"],
                    "arguments": event["arguments"],
                    "result": event["result"],
                    # 产生该调用的那一轮思维链,重放时放在它的 function_call 前
                    "reasoning": event.get("reasoning", ""),
                })
                tail_reasoning = ""
            _emit(gen, event)
        if done is None:
            raise RuntimeError("API 未返回完整结果")
        # 正文已经是完整一轮：选项是它之后的第二次请求，失败也不影响正文落盘
        if prep["options_enabled"]:
            _emit(gen, {"type": "options_start"})
            options_input = core.build_options_input(
                prep["state"], history, done["content"], tool_calls, done.get("reasoning", "")
            )
            options, options_error = await generate_options(options_input, prep["config"])
            if options_error:
                _emit(gen, {"type": "options_error", "message": options_error})
            else:
                _emit(gen, {"type": "options", "options": options})
        # 成功后一次性落盘
        _persist(
            name,
            history,
            mode,
            user_input,
            draft,
            done["content"],
            options,
            done.get("reasoning", ""),
            tool_calls,
            options_error=options_error,
        )
        # 世界状态随历史提交,快照键为落盘后的历史长度
        world.commit_turn(name, len(history))
    except asyncio.CancelledError:
        # 用户打断：半截输出落盘，由用户手动回滚或修改
        if streaming_started:
            _persist(
                name,
                history,
                mode,
                user_input,
                draft,
                partial_content or "（输出已被打断）",
                [],
                tail_reasoning,
                tool_calls,
            )
            # 半截回合已进历史,其世界状态改动一并提交,保持叙事与状态一致
            world.commit_turn(name, len(history))
        else:
            world.abort_turn(name)
    except ValueError as e:
        # 用户侧错误（会话状态、预设宏执行失败）：前端弹窗提示，不是后端内部错误
        world.abort_turn(name)
        logger.warning("会话 %s 用户侧错误: %s", name, e)
        _emit(gen, {"type": "error", "message": str(e), "popup": True})
    except Exception as e:
        # 后端/API 错误：控制台保留完整堆栈。半截结果照打断的先例落盘（带 error
        # 标记），让用户看到到底发生了什么，可修改/回滚/重新输出；已执行的工具
        # 调用改动一并提交，保持叙事与世界状态一致。尚未开始流式输出时无内容可
        # 落盘，只丢弃未提交的世界状态改动。
        logger.exception("会话 %s 生成失败", name)
        if streaming_started:
            _persist(
                name,
                history,
                mode,
                user_input,
                draft,
                partial_content or "（生成失败，无正文输出）",
                [],
                tail_reasoning,
                tool_calls,
                error=str(e),
            )
            world.commit_turn(name, len(history))
        else:
            world.abort_turn(name)
        _emit(gen, {"type": "error", "message": str(e), "persisted": streaming_started})
    finally:
        gen["finished"] = True
        gen["wake"].set()
        if _active.get(name) is gen:
            _active.pop(name, None)


def _prepare_options(name: str) -> dict:
    """只重新生成选项：正文不动、不发正文请求。用户侧错误以 ValueError 抛出。"""
    state = core.load_state(name)
    history = core.load_history(name)
    if not history or history[-1]["role"] != "assistant":
        raise ValueError("最后一轮不是 AI 块，没有可以重新生成选项的对象")
    entry = history[-1]
    config = core.effective_config(state, core.load_config())
    config["chat_id"] = state.get("chat_id", "")
    core.apply_preset_tools(config, state)
    config["reasoning_effort"] = core.resolve_reasoning_effort(
        state.get("reasoning_effort"), config
    )
    return {
        "name": name,
        "config": config,
        "input_items": core.build_options_input(
            state,
            history[:-1],
            entry.get("content", ""),
            entry.get("tool_calls", []),
            entry.get("reasoning", ""),
        ),
    }


async def _run_options(gen: dict, prep: dict) -> None:
    """只重新生成选项的生产者：改写历史最后一轮的 options / options_error。"""
    name = prep["name"]
    try:
        _emit(gen, {"type": "options_start"})
        options, options_error = await generate_options(prep["input_items"], prep["config"])
        history = core.load_history(name)
        if not history or history[-1]["role"] != "assistant":
            raise RuntimeError("最后一轮已不是 AI 块，选项无处可写")
        entry = history[-1]
        if options_error:
            entry["options_error"] = options_error
            _emit(gen, {"type": "options_error", "message": options_error})
        else:
            entry["options"] = options
            entry.pop("options_error", None)
            _emit(gen, {"type": "options", "options": options})
        core.save_history(name, history)
    except asyncio.CancelledError:
        raise  # 用户打断：选项没生成出来，历史保持原样
    except Exception as e:
        logger.exception("会话 %s 重新生成选项失败", name)
        _emit(gen, {"type": "error", "message": f"重新生成选项失败：{e}", "popup": True})
    finally:
        gen["finished"] = True
        gen["wake"].set()
        if _active.get(name) is gen:
            _active.pop(name, None)


async def _generate(name: str, mode: str, user_input: str | None):
    """mode: start（首轮）/ chat（带用户输入）/ regenerate（重发最后一轮）/ options（只重生成选项）。

    生成跑在会话级后台任务 _run 里，本请求只把事件转发给客户端：客户端断开
    （刷新、掉线、锁屏）只停这一次转发，生成继续跑完并落盘，重连走 /stream 续看。
    """
    if _gen_of(name) is not None:
        yield _sse({"type": "error", "message": "该会话已有生成在进行中", "popup": True})
        return
    try:
        prep = _prepare_options(name) if mode == "options" else _prepare(name, mode, user_input)
    except ValueError as e:
        # 用户侧错误（会话状态、预设宏执行失败）：前端弹窗提示，不是后端内部错误
        if mode != "options":
            world.abort_turn(name)
        logger.warning("会话 %s 用户侧错误: %s", name, e)
        yield _sse({"type": "error", "message": str(e), "popup": True})
        return
    gen = {
        "task": None,
        "events": [],
        "wake": asyncio.Event(),
        "finished": False,
        "mode": mode,
        "user_input": user_input,
    }
    gen["task"] = asyncio.create_task(
        _run_options(gen, prep) if mode == "options" else _run(gen, prep)
    )
    _active[name] = gen
    async for chunk in _tail(gen):
        yield chunk


@app.post("/api/sessions/{name}/interrupt")
async def interrupt(name: str):
    gen = _gen_of(name)
    if gen is not None:
        gen["task"].cancel()
        try:
            await gen["task"]  # 等半截输出落盘后再返回
        except BaseException:
            pass
    return {"ok": True}


@app.get("/api/sessions/{name}/stream")
async def stream(name: str):
    """接上该会话正在进行的生成：从头重放本轮事件，直到本轮结束。幂等，可多个客户端同时接。

    没有进行中的生成时返回立即结束的空流（前端据此回到按 history 渲染）。
    """
    gen = _gen_of(name)
    if gen is None:
        return StreamingResponse(iter(()), media_type="text/event-stream")
    return StreamingResponse(_tail(gen), media_type="text/event-stream")


class ChatInput(BaseModel):
    input: str


@app.post("/api/sessions/{name}/chat")
def chat(name: str, req: ChatInput):
    return StreamingResponse(
        _generate(name, "chat", req.input), media_type="text/event-stream"
    )


@app.post("/api/sessions/{name}/start")
def start(name: str):
    return StreamingResponse(
        _generate(name, "start", None), media_type="text/event-stream"
    )


@app.post("/api/sessions/{name}/regenerate")
def regenerate(name: str):
    return StreamingResponse(
        _generate(name, "regenerate", None), media_type="text/event-stream"
    )


@app.post("/api/sessions/{name}/regenerate_options")
def regenerate_options(name: str):
    """只重新生成最后一轮的选项：正文不动，也不重新跑正文请求。"""
    return StreamingResponse(
        _generate(name, "options", None), media_type="text/event-stream"
    )
