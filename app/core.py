"""预设/角色卡解析、session 存储、上下文拼装。"""
import datetime
import json
import math
import random
import re
import time
import uuid
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
state只记当前状态，避免无限增长的日志，防止无效信息堆积。上下文本身就是日志。

书写 world_run 代码的注意事项：
- state 是 dict：读写一律用下标（state['hp']），不支持点访问（state.hp 会报错）。
- 双引号嵌套需要多层转义、极易出错。含引号的文本（对话、选项）一律用单引号或三引号的 Python 字符串包裹，让双引号原样出现在字符串里。

再次提醒：正文写完后不要忘respond提交选项。
"""

# PTC 模式的任务提示词：world_run 是唯一直接工具，respond/read_file 是代码内绑定。
# 形态对齐 DeepSeek Harness 的 PTC 训练分布（单一代码执行工具 + 程序内绑定调用），
# 叙事仍是普通文本输出。由 ptc: true 的预设通过 {{respond_tool}} 宏注入。
AIRP_PROMPT_PTC = """\
每轮回复的固定流程：
1.（可选）调用 world_run 收集信息、执行计算、完成判定。world_run 是唯一能直接调用的工具，调用任何其它工具名都会失败。
2. 输出正文。正文直接作为普通文本输出。
3. 正文写完后，再调用一次 world_run，在程序末尾用 respond(options=[...]) 提交剧情推进选项并结束本轮。respond 会立即终止程序，它之后的代码不会执行。没有合适的选项时传空数组。

world_run 代码内的绑定函数（直接调用，不是工具）：
- respond(options: list)：提交剧情推进选项并结束本轮回复，只在正文写完之后调用。
- read_file(file_path: str, offset: int = 1, limit: int = 2000)：读取 UTF-8 文本文件（玩家提供的设定文档、笔记等），内容返回到当次日志；大文件用 offset/limit 分页。

world_run是你的计算器兼笔记本。所有数值与随机性判定（战斗、检定、经济、时间流逝……）用它写代码完成；随机性操作（如掷骰）必须用代码生成，口头编点数的随机性很糟糕。所有需要追踪的游戏数据（生命、资源、物品、位置、旗标……）放进全局对象 state；重复的流程（骰子判定、伤害公式等）定义为顶层 def 函数，跨调用自动保留。
state只记当前状态，避免无限增长的日志，防止无效信息堆积。上下文本身就是日志。

书写 world_run 代码的注意事项：
- state 是 dict：读写一律用下标（state['hp']），不支持点访问（state.hp 会报错）。
- 双引号嵌套需要多层转义、极易出错。含引号的文本（对话、选项）一律用单引号或三引号的 Python 字符串包裹，让双引号原样出现在字符串里。

再次提醒：正文写完后不要忘了调用 world_run 用 respond 提交选项。
"""

# api_type 无关的基础工具定义（只含 name/description/parameters）
_RESPOND_TOOL_DEF = {
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

_WORLD_RUN_TOOL_DEF = {
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

_READ_FILE_TOOL_DEF = {
    "name": "read_file",
    "description": (
        "读取一个 UTF-8 文本文件并返回其内容。"
        "用于读取玩家提供的设定文档、笔记、角色卡、存档等文本文件；"
        "相对路径以当前会话角色卡所在目录为基准，也支持绝对路径；"
        "大文件可用 offset/limit 分页继续读取（分页信息在返回末尾）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "要读取的文件路径（相对角色卡所在目录，或绝对路径）。",
            },
            "offset": {"type": "number", "description": "起始行号（1 起），默认 1。"},
            "limit": {"type": "number", "description": "最多返回行数，默认 2000，最大 2000。"},
        },
        "required": ["file_path"],
    },
}

# PTC 模式的 world_run：schema 中唯一的工具，描述对齐 DSH PTC 的契约句式
_WORLD_RUN_PTC_TOOL_DEF = {
    "name": "world_run",
    "description": (
        "唯一能直接调用的工具：在持久的 Python 环境中执行一段代码。调用任何其它工具名都会失败；"
        "respond(options) 与 read_file(file_path, ...) 是代码内的绑定函数，直接在程序里调用。"
        "只有你 print 的内容会作为执行结果返回给你（用户看不到 print 输出），正文必须作为普通文本输出。"
        "跨调用保留：全局对象 state、顶层 def 函数、全大写全局变量（常量）三者自动持久化，跨 turn 不丢失"
        "（state、print、respond、read_file 是内置绑定名，同名定义不会被保留）。"
        "原子执行：代码出错时自动回滚到执行前（state、函数、常量全部还原），不会留下半更新的状态。"
        "自动钩子：如果你定义了 normalize() 函数，每次代码成功执行后、生成 state diff 之前框架会自动调用它一次；"
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
            "description": {
                "type": "string",
                "description": "这段程序在做什么的简短摘要（5-10 词，展示在界面上）",
            },
            "dry": {
                "type": "boolean",
                "description": "试运行：照常执行并返回完整结果（含 normalize 效果与 state diff），但不提交任何变化（state、函数定义全部还原）。",
            },
        },
        "required": ["program"],
    },
}

_BASE_TOOLS = [_RESPOND_TOOL_DEF, _WORLD_RUN_TOOL_DEF, _READ_FILE_TOOL_DEF]


def _to_responses_format(tool: dict) -> dict:
    """把基础定义转为 Responses API 工具格式。"""
    return {"type": "function", **tool}


def _to_chat_completions_format(tool: dict) -> dict:
    """把基础定义转为 Chat Completions API 工具格式。"""
    return {"type": "function", "function": tool}


def get_tools(api_type: str, ptc: bool = False) -> list:
    """根据 api_type 返回对应格式的工具定义。ptc 模式下 schema 只含 world_run。"""
    if ptc:
        base = [_WORLD_RUN_PTC_TOOL_DEF]
    else:
        base = _BASE_TOOLS
    if api_type == "chat_completions":
        return [_to_chat_completions_format(t) for t in base]
    return [_to_responses_format(t) for t in base]


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
            # PTC 实验模式：schema 只含 world_run，respond/read_file 是代码内绑定
            "ptc": bool(meta.get("ptc")),
            # 用户输入后处理模板,渲染时提供 user_input 变量;缺省 None 表示原样透传
            "user_input_template": uim.group(1).strip() if uim else None,
        }
    return presets


def load_cards() -> dict:
    cards = {}
    # id 用相对路径（去 .md），天然唯一
    paths = sorted(GAMES_DIR.glob("*.md")) + sorted(GAMES_DIR.glob("*/*.md"))
    for p in paths:
        meta, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        m = SETTING_RE.search(body)
        um = USER_SETTING_RE.search(body)
        cid = p.relative_to(GAMES_DIR).with_suffix("").as_posix()
        cards[cid] = {
            "id": cid,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description") or "",
            "path": str(p),
            "setting": m.group(1).strip() if m else "",
            "user_setting": um.group(1).strip() if um else "",
            "beginnings": [b.group(1).strip() for b in BEGINNING_RE.finditer(body)],
        }
    return cards


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


# ---------- 多端点与生成参数 ----------

# 端点的合法键（来自 config.yaml 的 endpoints 列表）
_ENDPOINT_KEYS = ("display_name", "api_base", "model", "api_key", "api_type", "x_opencode_session")


def endpoints(config: dict) -> list:
    """返回校验后的端点列表。端点为空、缺 api_key/model/api_base 时直接报错（启动校验也走这里）。"""
    out = []
    for i, raw in enumerate(config.get("endpoints") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"config.yaml 的 endpoints[{i}] 不是映射: {raw!r}")
        ep = {k: raw[k] for k in _ENDPOINT_KEYS if k in raw}
        for k in ("api_base", "model", "api_key"):
            if not ep.get(k):
                raise ValueError(f"config.yaml 的 endpoints[{i}] 缺少必填项 {k}")
        ep.setdefault("display_name", ep["model"])
        out.append(ep)
    if not out:
        raise ValueError("config.yaml 没有配置任何端点（endpoints 为空）")
    return out


def resolve_endpoint_index(state_value, config: dict) -> int:
    """解析会话实际使用的端点下标：会话值合法直接用，非法/缺失回退 0。"""
    eps = endpoints(config)
    if isinstance(state_value, int) and not isinstance(state_value, bool) and 0 <= state_value < len(eps):
        return state_value
    return 0


def _resolve_num(state_value, config: dict, key: str, integer: bool):
    """数值型按会话覆盖：会话值合法直接用，否则回退全局；全局未设置返回 None（请求不带），非法直接报错。"""
    if integer:
        ok = lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 1
    else:
        ok = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    v = state_value if ok(state_value) else config.get(key)
    if v is None:
        return None
    if not ok(v):
        raise ValueError(f"config.yaml 的 {key} 非法: {v!r}")
    return v


def resolve_temperature(state_value, config: dict):
    return _resolve_num(state_value, config, "temperature", integer=False)


def resolve_max_tokens(state_value, config: dict):
    return _resolve_num(state_value, config, "max_tokens", integer=True)


def effective_config(state: dict, config: dict) -> dict:
    """合并出 llm 使用的扁平 config：全局键 + 选中端点键 + 会话生成参数覆盖。"""
    merged = {k: v for k, v in config.items() if k != "endpoints" and k not in _ENDPOINT_KEYS}
    merged.update(endpoints(config)[resolve_endpoint_index(state.get("endpoint"), config)])
    merged["temperature"] = resolve_temperature(state.get("temperature"), config)
    merged["max_tokens"] = resolve_max_tokens(state.get("max_tokens"), config)
    return merged


def _latest_session_state() -> dict | None:
    """created_at 最大的会话 state（新会话继承其模型与生成参数配置）；无会话返回 None。"""
    states = list_sessions()
    if not states:
        return None
    return max(states, key=lambda s: s.get("created_at") or 0)


# 思考强度五档；none = 禁用思考（请求带 "thinking": {"type": "disabled"}，见 llm.build_request）
EFFORT_LEVELS = ("none", "low", "medium", "high", "max")


def resolve_reasoning_effort(state_value, config: dict) -> str:
    """解析实际生效的思考强度。

    会话值合法直接用；非法或缺失时回退 config 默认值；config 未设置时用 low；
    config 值非法直接报错（不静默兜底，避免发出与预期不符的请求）。
    """
    if state_value in EFFORT_LEVELS:
        return state_value
    default = config.get("reasoning_effort")
    if default is None:
        return "low"
    if default not in EFFORT_LEVELS:
        raise ValueError(
            f"config.yaml 的 reasoning_effort 非法: {default!r}（可选值: {', '.join(EFFORT_LEVELS)}）"
        )
    return default


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
    card_obj = cards[card]
    d = _session_dir(name)
    if d.exists():
        raise ValueError(f"session 已存在: {name}")
    if beginning_index is None:
        text = ""
    else:
        text = card_obj["beginnings"][beginning_index]
    d.mkdir(parents=True)
    (d / "history.jsonl").touch()
    config = load_config()
    # 模型端点与生成参数继承上一个会话（最近创建者）；没有会话时回退端点 0 + config 默认值
    prev = _latest_session_state() or {}
    state = {
        "id": d.name,
        "name": name,
        "preset": preset,
        "card": card_obj["id"],
        "card_name": card_obj["name"],
        "beginning_index": beginning_index,
        "beginning_text": text,
        "created_at": time.time(),
        # 每会话固定的 UUID v4,作为 X-Opencode-Session 请求头发送(opencode-go 风控)
        "chat_id": str(uuid.uuid4()),
        "endpoint": resolve_endpoint_index(prev.get("endpoint"), config),
        "temperature": resolve_temperature(prev.get("temperature"), config),
        "max_tokens": resolve_max_tokens(prev.get("max_tokens"), config),
        # 创建时快照思考强度,之后前端可按会话覆盖(config 非法时此处直接报错)
        "reasoning_effort": resolve_reasoning_effort(prev.get("reasoning_effort"), config),
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
        # fork 是新对话,重新生成 chat_id,不与原会话共用
        "chat_id": str(uuid.uuid4()),
    }
    _session_dir(new_name).mkdir(parents=True)
    save_state(new_state)
    save_history(new_name, history[:index])
    return new_state


def load_state(name: str) -> dict:
    return json.loads((_session_dir(name) / "state.json").read_text(encoding="utf-8"))


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


def count_turns(name: str) -> int:
    """已运行的轮数 = history 里模型回复（assistant 块）的条数。"""
    return sum(1 for e in load_history(name) if e.get("role") == "assistant")


# ---------- 上下文拼装 ----------

def _message_item(role: str, content: str) -> dict:
    """预设 section 对应的 Responses message 项。assistant 用 output_text,其余用 input_text。"""
    part_type = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": part_type, "text": content}]}


def _split_tool_calls(entry: dict, ptc: bool):
    """把一轮 assistant 的工具调用拆成（辅助调用列表, 收尾调用四元组）。

    收尾调用 (name, arguments, result, reasoning) 回放在正文后：PTC 用带 terminal
    标记的终结 world_run，没有（打断/出错轮）则合成 respond 绑定调用兜底；
    非 PTC 合成 respond 工具调用。无论有无选项都固定回放，保持示范轮一致
    （DeepSeek 会模仿旧轮次的行为，固定模式反而强化选项的稳定生成）。
    """
    tool_calls = entry.get("tool_calls", [])
    terminal_tc = next((tc for tc in tool_calls if tc.get("terminal")), None) if ptc else None
    normal = [tc for tc in tool_calls if tc is not terminal_tc]
    if terminal_tc is not None:
        end = (terminal_tc["name"], terminal_tc["arguments"], terminal_tc["result"],
               terminal_tc.get("reasoning") or " ")
    elif ptc:
        end = ("world_run",
               json.dumps({"program": "respond(options="
                           + json.dumps(entry.get("options") or [], ensure_ascii=False) + ")"},
                          ensure_ascii=False),
               "选项已提交，本轮回复结束。", " ")
    else:
        end = ("respond",
               json.dumps({"options": entry.get("options") or []}, ensure_ascii=False),
               "ok", " ")
    if end[3] == " ":
        # 终结调用自身没存思维链：用整轮或最后一个辅助调用的思维链兜底
        end = end[:3] + (entry.get("reasoning") or next(
            (tc["reasoning"] for tc in reversed(tool_calls) if tc.get("reasoning")), " "),)
    return normal, end


def _build_input_responses(state, history, draft, preset, env):
    """拼装 Responses API 的 input_items。"""
    items = [_message_item(sec["role"], render_template(sec["content"], env)) for sec in preset["sections"]]
    ptc = bool(preset.get("ptc"))
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            normal_tcs, (end_name, end_args, end_result, end_reasoning) = _split_tool_calls(entry, ptc)
            for tc in normal_tcs:
                items.append({
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": tc.get("reasoning") or " "}],
                })
                call_n += 1
                items.append({
                    "type": "function_call",
                    "call_id": f"call_{call_n}",
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                })
                items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": tc["result"]})
            # 正文:普通 assistant message
            if entry.get("content"):
                items.append(_message_item("assistant", entry["content"]))
            items.append({
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": end_reasoning}],
            })
            call_n += 1
            items.append({
                "type": "function_call",
                "call_id": f"call_{call_n}",
                "name": end_name,
                "arguments": end_args,
            })
            items.append({"type": "function_call_output", "call_id": f"call_{call_n}", "output": end_result})
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


def _build_input_chat_completions(state, history, draft, preset, env):
    """拼装 Chat Completions API 的 messages 列表。"""
    messages = [{"role": sec["role"], "content": render_template(sec["content"], env)} for sec in preset["sections"]]
    ptc = bool(preset.get("ptc"))
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            entry_reasoning = entry.get("reasoning") or " "
            normal_tcs, (end_name, end_args, end_result, end_reasoning) = _split_tool_calls(entry, ptc)
            # 辅助工具调用：每个 tool_call 独立成 assistant(tool_calls) + tool 消息
            for tc in normal_tcs:
                call_n += 1
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": tc.get("reasoning") or entry_reasoning,
                    "tool_calls": [{
                        "id": f"call_{call_n}",
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{call_n}",
                    "content": tc["result"],
                })
            # 正文
            if entry.get("content"):
                messages.append({
                    "role": "assistant",
                    "content": entry["content"],
                    "reasoning_content": entry_reasoning,
                })
            # 收尾调用（见 _split_tool_calls）
            call_n += 1
            messages.append({
                "role": "assistant",
                "content": "",
                "reasoning_content": end_reasoning,
                "tool_calls": [{
                    "id": f"call_{call_n}",
                    "type": "function",
                    "function": {"name": end_name, "arguments": end_args},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{call_n}",
                "content": end_result,
            })
        else:
            messages.append({"role": "user", "content": entry["content"]})
    # 输入框中的本次输入：作为新的 user message 拼在末尾。
    if draft and history and history[-1]["role"] == "assistant":
        template = preset.get("user_input_template")
        if template is not None:
            draft = render_template(template, {**env, "user_input": draft})
        messages.append({"role": "user", "content": draft})
    return messages


def build_input(state: dict, history: list, draft=None) -> list:
    """拼装发送给模型的完整输入。根据会话生效的 api_type 返回 Responses input_items 或 Chat Completions messages。"""
    config = effective_config(state, load_config())
    api_type = config.get("api_type", "responses")
    preset = load_presets()[state["preset"]]
    card = load_cards()[state["card"]]
    env = {
        **MACRO_MODULES,
        "game_setting": card["setting"],
        "game_beginning": state["beginning_text"],
        "user_setting": card["user_setting"],
        "respond_tool": AIRP_PROMPT_PTC if preset.get("ptc") else AIRP_PROMPT,
    }
    if api_type == "chat_completions":
        return _build_input_chat_completions(state, history, draft, preset, env)
    return _build_input_responses(state, history, draft, preset, env)
