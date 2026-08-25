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

# AIRP 任务提示词：使 AI 明确自身任务（正文直接文本输出，选项走 respond 工具，world_run/read_file 辅助）。
# 通过预设中的 {{respond_tool}} 宏显式插入，代码不会自动注入任何额外系统提示词。
# 角色设定由预设中的 {{game_setting}} 宏注入，预设内部已用 <dream_setting> 等标签包裹。
# 实际上用户可以看到思考内容，但不需要告诉模型
AIRP_PROMPT = """\
每轮回复的固定流程：
1.（可选）任意时刻调用 world_run / read_file 收集信息、执行计算、完成判定，执行结果作为工具结果返回给你；
2. 输出正文。需要的话中间可以插入world_run / read_file。
3. 在正文写完后，调用 respond 提交的剧情推进选项（options）同时结束本轮。没有合适的选项时传空数组。

world_run是你的计算器兼笔记本。所有数值与随机性判定（战斗、检定、经济、时间流逝……）用它写代码完成；随机性操作（如掷骰）必须用代码生成，口头编点数的随机性很糟糕。所有需要追踪的游戏数据（生命、资源、物品、位置、旗标……）放进全局对象 state；重复的流程（骰子判定、伤害公式等）定义为顶层 def 函数，跨调用自动保留。

再次提醒：正文写完后不要忘respond提交选项。
"""

# Responses API 风格的 function 工具定义
RESPOND_TOOL = {
    "type": "function",
    "name": "respond",
    "description": "提交剧情推进选项并结束本轮回复。调用本工具之前，必须已经以普通文本输出了完整正文（正文不写在本工具里）。参数只含选项；无选项时传空数组。",
    "parameters": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "正文之后的剧情推进选项，无选项时传空数组",
            },
        },
        "required": ["options"],
    },
}

WORLD_RUN_TOOL = {
    "type": "function",
    "name": "world_run",
    "description": (
        "在持久的 Python 环境中执行一段代码，用于一切涉及数值与规则的判定与状态更新。"
        "跨调用保留：全局对象 state、顶层 def 函数、全大写全局变量（常量）三者自动持久化，跨 turn 不丢失。"
        "约定：会变的游戏数据（生命、资源、旗标……）放进 state；固定不变的常量表/魔法数字用全大写变量定义（如 TIERS = [...]、MAX_HP = 100）；可复用的流程用顶层 def 函数定义（函数内部的 def 是局部的，调用结束即消失）。"
        "输出：print(...值) 写入当次日志返回。注意 normalize 之外的 print 是 normalize 执行前的值，normalize 执行后的结果在 state diff 中自动返回。"
        "原子执行：代码出错时自动回滚到执行前（state、函数、常量全部还原），不会留下半更新的状态。"
        "自动钩子：如果你定义了 normalize() 函数，每次代码成功执行后、生成 state diff 之前框架会自动调用它一次；"
        "把变量钳制（如 if state['hp'] < 0: state['hp'] = 0）、阈值提醒（函数里 print 即提醒）、派生量自动更新写在里面避免遗忘；"
        "normalize 出错只回滚它自己的改动并记录，不影响本次代码的成果。"
        "每次执行返回：日志、state 的变化 diff、normalize 错误（如有）。"
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
                "description": "试运行：照常执行并返回完整结果（含 normalize 效果与 state diff），但不提交任何变化（state、函数定义全部还原）。",
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


def fork_session(name: str, index: int) -> dict:
    """在用户块 index 处分叉：复制会话状态与 history[:index] 为新会话，原会话不动。

    新会话最后一项是 assistant 块（或空历史），可直接继续输入。世界状态由
    world.fork 按同一 index 复制。
    """
    state = load_state(name)
    history = load_history(name)
    if not (0 <= index < len(history)) or history[index]["role"] != "user":
        raise ValueError("fork 目标不是用户块")
    base = f"{state['name']}-fork-{time.strftime('%Y%m%d-%H%M%S')}"
    new_name, n = base, 2
    while _session_dir(new_name).exists():
        new_name, n = f"{base}-{n}", n + 1
    new_state = {
        **state,
        "id": safe_dir_name(new_name),
        "name": new_name,
        "created_at": time.time(),
    }
    _session_dir(new_name).mkdir(parents=True)
    save_state(new_state)
    save_history(new_name, history[:index])
    return new_state


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
    # （回合内的工具循环）+ 正文 message + respond 的 function_call/output 对（只含选项）；
    # user 块 -> 普通 user message。call_n 对每个 function_call 项递增。
    # 请求以 user message 结尾，历史里的 reasoning 回放
    # 因此不受 DeepSeek「reasoning_text must be passed back」强制约束（实验 G 验证），
    # 但仍全部回传，与官方文档口径一致。旧格式历史（正文也在 entry 上）同样适用。
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
            # 正文:普通 assistant message(新旧格式都存在 entry["content"] 上)
            if entry.get("content"):
                items.append(_message_item("assistant", entry["content"]))
            # respond 只提交选项。无论有无选项都回放这次调用,让模型每轮看到一致的收尾模式
            # (DeepSeek 会模仿旧轮次的行为,固定模式反而强化选项的稳定生成)
            respond_reasoning = entry.get("reasoning") or next(
                (tc["reasoning"] for tc in reversed(entry.get("tool_calls", [])) if tc.get("reasoning")), " ")
            items.append({
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": respond_reasoning}],
            })
            call_n += 1
            items.append({
                "type": "function_call",
                "call_id": f"call_{call_n}",
                "name": "respond",
                "arguments": json.dumps({"options": entry.get("options") or []}, ensure_ascii=False),
            })
            items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": "ok"})
        else:
            # 用户输入:普通 user message(不再伪装成 respond 的工具结果)
            items.append(_message_item("user", entry["content"]))
    # 输入框中的本次输入：作为新的 user message 拼在末尾,请求以 user message 结尾。
    # 预设含 preset_user_input 块时,先按模板渲染(提供 user_input 变量);
    # 渲染只影响本次发送,落盘的 history 仍是渲染前的原文。
    if draft and history and history[-1]["role"] == "assistant":
        template = preset.get("user_input_template")
        if template is not None:
            draft = render_template(template, {**env, "user_input": draft})
        items.append(_message_item("user", draft))
    return items
