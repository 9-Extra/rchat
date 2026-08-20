"""预设/角色卡解析、session 存储、上下文拼装。"""
import datetime
import json
import math
import random
import re
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PRESET_DIR = ROOT / "preset"
GAMES_DIR = ROOT / "games"
SESSIONS_DIR = ROOT / "sessions"

# AIRP 任务提示词：使 AI 明确自身任务（通过 respond 工具输出，world_run/read_file 辅助）。
# 通过预设中的 {{respond_tool}} 宏显式插入，代码不会自动注入任何额外系统提示词。
# 角色设定由预设中的 {{game_setting}} 宏注入，预设内部已用 <dream_setting> 等标签包裹。
# 实际上用户可以看到思考内容，但不需要告诉模型
AIRP_PROMPT = """\
你的输出通道与可用工具：
- respond：唯一对用户可见的输出通道。你必须通过调用 respond 输出剧情正文（content）与后续选项（options），工具之外的任何文本用户都看不到。它必须是一轮回复中最后一次工具调用。
- world_run：持久的 Python 环境，是你的计算器兼笔记本。所有数值与随机性判定（战斗、检定、经济、时间流逝……）用它写代码完成；随机性操作（如掷骰）必须用代码生成，口头编点数的随机性很糟糕。所有需要追踪的游戏数据（生命、资源、物品、位置、旗标……）放进全局对象 state；重复的流程（骰子判定、伤害公式等）定义为顶层 def 函数，跨调用自动保留。查看具体值用 print(...) 或给 result 变量赋值；定义 normalize() 函数可在每次执行后自动整理（变量钳制、阈值提醒）。代码出错会自动回滚，传 dry=true 可试运行。
- read_file：读取文本文件（设定文档、笔记、规则书等），大文件用 offset/limit 分页。
- 一轮回复中可以先多次调用 world_run / read_file，最后调用 respond 结束。用户的新一轮输入会作为 respond 调用的结果（tool 消息）返回给你。
"""

# Responses API 风格的 function 工具定义
RESPOND_TOOL = {
    "type": "function",
    "name": "respond",
    "description": "输出剧情正文与后续选项。只有此工具内的内容对用户可见。每轮回复必须调用本工具。",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "正文"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "正文后的剧情推进选项",
            },
        },
        "required": ["content"],
    },
}

WORLD_RUN_TOOL = {
    "type": "function",
    "name": "world_run",
    "description": (
        "在持久的 Python 环境中执行一段代码，用于一切涉及数值与规则的判定与状态更新。"
        "跨调用保留：全局对象 state 与顶层 def 定义的函数自动持久化。"
        "约定：所有需要追踪的游戏数据放进 state；辅助函数在程序顶层用 def 定义即可跨调用保留（函数内部的 def 是局部的，调用结束即消失）。"
        "输出：print(...值) 写入当次日志返回；给 result 变量赋值作为返回值返回。注意 normalize 之外的 print/result 是 normalize 执行前的值，normalize 执行后的结果在 state diff 中自动返回。"
        "原子执行：代码出错时自动回滚到执行前（state、函数定义全部还原），不会留下半更新的状态。"
        "自动整理：如果你定义了 normalize() 函数，每次代码成功执行后、生成 state diff 之前框架会自动调用它一次；"
        "把变量钳制（如 if state['hp'] < 0: state['hp'] = 0）、阈值提醒（函数里 print 即提醒）、派生量自动更新写在里面避免遗忘；"
        "normalize 出错只回滚它自己的改动并记录，不影响本次代码的成果。"
        "每次执行返回：返回值、日志、state 的变化 diff、normalize 错误（如有）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "program": {
                "type": "string",
                "description": "要执行的 Python 代码。读取/修改 state，或在顶层定义函数供后续调用使用",
            },
            "dry": {
                "type": "boolean",
                "description": "试运行：照常执行并返回完整结果（含 normalize 效果与 state diff），但不提交任何变化（state、函数定义全部还原）。用于复杂更新前排错确认。",
            },
        },
        "required": ["program"],
    },
}

READ_FILE_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": (
        "读取一个 UTF-8 文本文件并返回其内容。"
        "用于读取玩家提供的设定文档、笔记、角色卡、存档等文本文件；"
        "相对路径以项目根目录为基准，也支持绝对路径；"
        "大文件可用 offset/limit 分页继续读取（分页信息在返回末尾）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径（相对项目根目录，或绝对路径）。",
            },
            "offset": {"type": "number", "description": "起始行号（1 起），默认 1。"},
            "limit": {"type": "number", "description": "最多返回行数，默认 2000，最大 2000。"},
        },
        "required": ["file_path"],
    },
}

TOOLS = [RESPOND_TOOL, WORLD_RUN_TOOL, READ_FILE_TOOL]

SECTION_RE = re.compile(
    r'<preset_section\s+role="(system|user|assistant)"\s*>(.*?)</preset_section>', re.S
)
USER_INPUT_RE = re.compile(r"<preset_user_input>(.*?)</preset_user_input>", re.S)
SETTING_RE = re.compile(r"<game_setting>(.*?)</game_setting>", re.S)
USER_SETTING_RE = re.compile(r"<user_setting>(.*?)</user_setting>", re.S)
BEGINNING_RE = re.compile(r"<game_beginning>(.*?)</game_beginning>", re.S)
# 宏: {{表达式}}。所有预设文本统一按 Python 表达式求值
MACRO_RE = re.compile(r"\{\{(.*?)\}\}")
# 宏求值环境中预置的模块;另有上下文变量 game_setting / game_beginning /
# user_setting / respond_tool(四个固定宏)和 user_input(仅 preset_user_input 块)
MACRO_MODULES = {"random": random, "time": time, "math": math, "datetime": datetime}


def render_template(text: str, env: dict) -> str:
    """把 text 中的 {{表达式}} 逐个 eval 求值并替换为结果的 str。

    严格模式: 未知变量或执行出错直接抛 ValueError,不做静默兜底。
    预设是本机可信文件,eval 保留完整 builtins,不构成安全边界。
    """
    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        try:
            return str(eval(expr, dict(env)))
        except Exception as e:
            raise ValueError(f"预设宏执行失败 {expr!r}: {e}")
    return MACRO_RE.sub(repl, text)
# 文件系统明确不允许的字符（Windows 保留字符 + 控制字符）
INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 保留设备名
RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_dir_name(name: str) -> str:
    """把任意 session 名映射为可正常创建的文件夹名。幂等：对结果再次调用结果不变。

    只影响磁盘上的目录名；session 的原始名不做任何修改，保存在 state.json 中。
    """
    safe = INVALID_NAME_RE.sub("_", name).rstrip(". ")
    if not safe:
        safe = "session"
    if safe.split(".")[0].lower() in RESERVED_NAMES:
        safe = "_" + safe
    return safe


def _split_frontmatter(text: str):
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m:
            return (yaml.safe_load(m.group(1)) or {}), text[m.end():]
    return {}, text


def load_presets() -> dict:
    presets = {}
    for p in sorted(PRESET_DIR.glob("*.md")):
        meta, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        sections = [
            {"role": m.group(1), "content": m.group(2).strip()}
            for m in SECTION_RE.finditer(body)
        ]
        uim = USER_INPUT_RE.search(body)
        presets[p.stem] = {
            "id": p.stem,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description") or "",
            "sections": sections,
            # 用户输入后处理模板,渲染时提供 user_input 变量;缺省 None 表示原样透传
            "user_input_template": uim.group(1).strip() if uim else None,
        }
    return presets


def load_cards() -> dict:
    cards = {}
    for p in sorted(GAMES_DIR.glob("*.md")):
        meta, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        m = SETTING_RE.search(body)
        um = USER_SETTING_RE.search(body)
        cards[p.stem] = {
            "id": p.stem,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description") or "",
            "setting": m.group(1).strip() if m else "",
            "user_setting": um.group(1).strip() if um else "",
            "beginnings": [b.group(1).strip() for b in BEGINNING_RE.finditer(body)],
        }
    return cards


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


# ---------- session 存储 ----------

def _session_dir(name: str) -> Path:
    return SESSIONS_DIR / safe_dir_name(name)


def list_sessions() -> list:
    if not SESSIONS_DIR.exists():
        return []
    out = []
    for d in sorted(SESSIONS_DIR.iterdir()):
        if (d / "state.json").exists():
            out.append(load_state(d.name))
    return out


def create_session(name: str, preset: str, card: str, beginning_index):
    cards = load_cards()
    if preset not in load_presets():
        raise ValueError(f"预设不存在: {preset}")
    if card not in cards:
        raise ValueError(f"角色卡不存在: {card}")
    d = _session_dir(name)
    if d.exists():
        raise ValueError(f"session 已存在: {name}")
    if beginning_index is None:
        text = ""
    else:
        text = cards[card]["beginnings"][beginning_index]
    d.mkdir(parents=True)
    (d / "history.jsonl").touch()
    state = {
        "id": d.name,
        "name": name,
        "preset": preset,
        "card": card,
        "beginning_index": beginning_index,
        "beginning_text": text,
        "created_at": time.time(),
    }
    save_state(state)
    return state


def load_state(name: str) -> dict:
    state = json.loads((_session_dir(name) / "state.json").read_text(encoding="utf-8"))
    # 旧版 state.json 没有 id 字段，按目录名规则补上
    state.setdefault("id", safe_dir_name(state["name"]))
    return state


def save_state(state: dict) -> None:
    (_session_dir(state["name"]) / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_history(name: str) -> list:
    f = _session_dir(name) / "history.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_history(name: str, history: list) -> None:
    f = _session_dir(name) / "history.jsonl"
    f.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in history), encoding="utf-8"
    )


# ---------- 上下文拼装 ----------

def _message_item(role: str, content: str) -> dict:
    """预设 section 对应的 Responses message 项。assistant 用 output_text,其余用 input_text。"""
    part_type = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": part_type, "text": content}]}


def build_input(state: dict, history: list, draft=None) -> list:
    """拼装发送给 Responses API 的完整 input 列表。每次调用都用当前保存的开局重新渲染预设。"""
    preset = load_presets()[state["preset"]]
    card = load_cards()[state["card"]]
    # 宏求值环境: 模块 + 四个固定宏(同名变量,与旧版 .replace() 行为一致)
    env = {
        **MACRO_MODULES,
        "game_setting": card["setting"],
        "game_beginning": state["beginning_text"],
        "user_setting": card["user_setting"],
        "respond_tool": AIRP_PROMPT,
    }
    items = [_message_item(sec["role"], render_template(sec["content"], env)) for sec in preset["sections"]]
    # 对话历史：assistant 块 -> 若干 world_run/read_file 的 function_call/output 对
    # （回合内的工具循环）+ respond 的 function_call 项；user 块 -> respond 的
    # function_call_output 项。call_n 对每个 function_call 项递增。
    # DeepSeek 思考模式要求每个 function_call 前都紧跟产生它的那一轮非空思维链
    # （缺失或空串会 400；同一段文本重复传是合法的），实在没有时用单空格占位。
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            for tc in entry.get("tool_calls", []):
                # 新格式逐项带 reasoning;旧格式没有,用整轮合并的 entry["reasoning"] 兜底
                reasoning = tc.get("reasoning") or entry.get("reasoning") or " "
                items.append({
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": reasoning}],
                })
                call_n += 1
                items.append({
                    "type": "function_call",
                    "call_id": f"call_{call_n}",
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                })
                items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": tc["result"]})
            call_n += 1
            # 产生 respond 的那一轮思维链,放在 respond 调用前;为空时(模型收尾轮没思考,
            # 或旧格式数据)兜底用最后一个工具调用的思维链,避免 respond 调用前缺 reasoning 被 400
            respond_reasoning = entry.get("reasoning") or next(
                (tc["reasoning"] for tc in reversed(entry.get("tool_calls", [])) if tc.get("reasoning")), " ")
            items.append({
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": respond_reasoning}],
            })
            args = {"content": entry["content"]}
            if entry.get("options"):
                args["options"] = entry["options"]
            items.append({
                "type": "function_call",
                "call_id": f"call_{call_n}",
                "name": "respond",
                "arguments": json.dumps(args, ensure_ascii=False),
            })
        else:
            items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": entry["content"]})
    # 输入框中的本次输入：作为最后一个工具调用的结果拼入。
    # 预设含 preset_user_input 块时,先按模板渲染(提供 user_input 变量);
    # 渲染只影响本次发送,落盘的 history 仍是渲染前的原文。
    if draft and history and history[-1]["role"] == "assistant":
        template = preset.get("user_input_template")
        if template is not None:
            draft = render_template(template, {**env, "user_input": draft})
        items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": draft})
    return items
