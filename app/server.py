"""FastAPI 服务端：页面、会话管理、流式对话。"""

import asyncio
import json
import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import core, tools, world
from app.llm import build_request, stream_respond

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
    return core.list_sessions()


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
        return state
    except FileNotFoundError:
        raise HTTPException(404, "session 不存在")


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
    except ValueError as e:
        # 预设宏执行失败：用户侧错误，返回 400 而非 500
        raise HTTPException(400, str(e))
    # 展示实际发送给 Responses API 的请求参数
    return build_request(input_items, core.load_config())


# ---------- 流式对话 ----------

# 每个 session 当前正在进行的生成任务，用于打断
_active: dict = {}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _persist(name, history, mode, user_input, draft, content, options, reasoning, tool_calls):
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
    # 回合内 respond 之前的 world_run/read_file 调用,重放上下文时用
    if tool_calls:
        entry["tool_calls"] = tool_calls
    history.append(entry)
    core.save_history(name, history)


async def _generate(name: str, mode: str, user_input: str | None):
    """mode: start（首轮）/ chat（带用户输入）/ regenerate（重发最后一轮）。"""
    _active[name] = asyncio.current_task()
    history = []
    draft = None
    tool_calls = []
    partial_content = ""
    # 当前未完成轮(自上次工具事件后)累积的思维链;打断落盘时作为 respond 前的 reasoning
    tail_reasoning = ""
    streaming_started = False
    try:
        state = core.load_state(name)
        history = core.load_history(name)
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
        config = core.load_config()
        done = None
        streaming_started = True
        run_tool = lambda tool_name, arguments: tools.execute_tool(name, tool_name, arguments)
        async for event in stream_respond(input_items, config, run_tool):
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
            yield _sse(event)
        if done is None:
            raise RuntimeError("API 未返回完整结果")
        # 成功后一次性落盘
        _persist(
            name,
            history,
            mode,
            user_input,
            draft,
            done["content"],
            done["options"],
            done.get("reasoning", ""),
            tool_calls,
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
        yield _sse({"type": "error", "message": str(e), "popup": True})
    except Exception as e:
        # 后端/API 错误：控制台保留完整堆栈，同时发给前端内联显示
        world.abort_turn(name)
        logger.exception("会话 %s 生成失败", name)
        yield _sse({"type": "error", "message": str(e)})
    finally:
        _active.pop(name, None)


@app.post("/api/sessions/{name}/interrupt")
async def interrupt(name: str):
    task = _active.get(name)
    if task is not None:
        task.cancel()
        try:
            await task  # 等半截输出落盘后再返回
        except BaseException:
            pass
    return {"ok": True}


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
