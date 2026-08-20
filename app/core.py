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

# AIRP 任务提示词：使 AI 明确自身任务（只通过 respond 工具输出）。
# 通过预设中的 {{respond_tool}} 宏显式插入，代码不会自动注入任何额外系统提示词。
# 角色设定由预设中的 {{game_setting}} 宏注入，预设内部已用 <dream_setting> 等标签包裹。
# 实际上用户可以看到思考内容，但不需要告诉模型
AIRP_PROMPT = """\
你的唯一输出通道是 respond 工具：
- 你必须通过调用 respond 工具输出剧情正文（content）与后续选项（options）
- 只有工具调用内部的内容对用户可见，工具之外的任何文本用户都看不到
- 用户的新一轮输入会作为你上一次工具调用的结果（tool 消息）返回给你
"""

# Responses API 风格的 function 工具定义
RESPOND_TOOL = {
    "type": "function",
    "name": "respond",
    "description": "输出剧情正文与后续选项。只有此工具内的内容对用户可见。",
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
    # 对话历史：assistant 块 -> respond 的 function_call 项；user 块 -> function_call_output 项
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            call_n += 1
            # 思考模式要求把 reasoning 原样传回
            if entry.get("reasoning"):
                items.append({
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": entry["reasoning"]}],
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
