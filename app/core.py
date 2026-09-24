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

# AIRP 任务提示词：使 AI 明确自身任务（正文直接文本输出，工具只用于判定与记账）。
# 通过预设中的 {{respond_tool}} 宏显式插入，代码不会自动注入任何额外系统提示词。
# 宏名沿用历史叫法，内容已不含任何选项契约：选项不再由模型在正文回合里提交，而是在正文
# 落盘后由 OPTIONS_INSTRUCTION 驱动的独立请求生成（见 llm.generate_options）。
# 文本由 respond_tool_text 按预设 frontmatter 的 tools 白名单组装：没启用的工具不出现在
# 提示词里。
# 角色设定由预设中的 {{game_setting}} 宏注入，预设内部已用 <dream_setting> 等标签包裹。
# 实际上用户可以看到思考内容，但不需要告诉模型

# world_run 的说明段（它被启用时才出现）
_WORLD_RUN_PARA = """\
world_run是你的计算器兼笔记本。所有数值与随机性判定（战斗、检定、经济、时间流逝……）用它写代码完成；随机性操作（如掷骰）必须用代码生成，口头编点数的随机性很糟糕。所有需要追踪的游戏数据（生命、资源、物品、位置、旗标……）放进全局对象 state；重复的流程（骰子判定、伤害公式等）定义为顶层 def 函数，跨调用自动保留。
state只记当前状态，避免无限增长的日志，防止无效信息堆积。上下文本身就是日志。"""

_WORLD_RUN_NOTES = """\
书写 world_run 代码的注意事项：
- state 是 dict：读写一律用下标（state['hp']），不支持点访问（state.hp 会报错）。
- 双引号嵌套需要多层转义、极易出错。含引号的文本（对话、选项）一律用单引号或三引号的 Python 字符串包裹，让双引号原样出现在字符串里。"""

# 附加工具的说明段（read_file 只在流程里点名，没有专段）
_TOOL_PARAS = {
    "write_file": """\
write_file 用来写文件：默认整篇覆盖（文件不存在就新建，父目录自动创建），mode='append' 时追加到末尾；换行统一为 LF。相对路径以本会话角色卡所在目录为基准，也接受绝对路径。改文件里的局部内容请用 edit_file，不要整篇重写。""",
    "edit_file": """\
edit_file 用来把文件里的一段文字精确替换成另一段：old_string 必须与文件内容逐字一致（含缩进与空行），默认要求它在文件里唯一，否则报错要你给出更长的上下文；replace_all=True 时全部替换。不确定文件当前内容时先用 read_file 看一眼。""",
    "bash": """\
bash 在角色卡所在目录执行一段 shell 命令（Git Bash），返回退出码与合并后的输出（stdout 在前、stderr 在后）：适合批量处理文件、跑脚本、查环境这类事情。命令有超时上限（默认 60 秒，最多 600 秒），超时会被终止；不要用它跑需要交互输入的命令。""",
}


def _airp_prompt(direct: list) -> str:
    """非 PTC 模式的任务提示词：按启用的直接工具裁剪。"""
    flow = ["每轮回复的固定流程："]
    step = 1
    if direct:
        names = " / ".join(direct)
        flow.append(
            f"{step}.（可选）任意时刻调用 {names} 收集信息、执行计算、完成判定，执行结果作为工具结果返回给你；"
        )
        step += 1
        flow.append(f"{step}. 输出正文。需要的话中间可以插入{names}。")
    else:
        flow.append(f"{step}. 输出正文。")
    parts = ["\n".join(flow)]
    if "world_run" in direct:
        parts += [_WORLD_RUN_PARA, _WORLD_RUN_NOTES]
    parts += [_TOOL_PARAS[n] for n in direct if n in _TOOL_PARAS]
    parts.append("再次提醒：正文直接写出来即可，工具只用来做判定与记账。")
    return "\n\n".join(parts)

# PTC 模式的任务提示词：world_run 是唯一直接工具，read_file 等是代码内绑定。
# 形态对齐 DeepSeek Harness 的 PTC 训练分布（单一代码执行工具 + 程序内绑定调用），
# 叙事仍是普通文本输出。由 ptc: true 的预设通过 {{respond_tool}} 宏注入；
# 绑定清单按预设的 tools 白名单裁剪（启用什么列什么）。
_PTC_BINDING_DOCS = {
    "read_file": "- read_file(file_path: str, offset: int = 1, limit: int = 2000)：读取 UTF-8 文本文件（玩家提供的设定文档、笔记等），内容返回到当次日志；大文件用 offset/limit 分页。",
    "write_file": "- write_file(file_path: str, content: str = '', mode: str = 'overwrite')：写 UTF-8 文本文件（overwrite 整篇覆盖/新建，append 追加到末尾，父目录自动创建），相对路径以角色卡所在目录为基准。",
    "edit_file": "- edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False)：把文件里的一段文字精确替换成另一段，old_string 必须与文件内容逐字一致且默认唯一。",
    "bash": "- bash(command: str, timeout: int = 60)：执行一段 shell 命令（Git Bash），返回退出码与合并后的输出，timeout 最多 600 秒。",
}


def _airp_prompt_ptc(bindings: list) -> str:
    """PTC 模式的任务提示词：绑定函数清单按启用的工具生成。"""
    docs = [_PTC_BINDING_DOCS[b] for b in bindings if b in _PTC_BINDING_DOCS]
    parts = [
        "每轮回复的固定流程：\n"
        "1.（可选）调用 world_run 收集信息、执行计算、完成判定。world_run 是唯一能直接调用的工具，调用任何其它工具名都会失败。\n"
        "2. 输出正文。正文直接作为普通文本输出。",
        _WORLD_RUN_PARA,
        _WORLD_RUN_NOTES,
    ]
    if docs:
        parts.insert(1, "world_run 代码内的绑定函数（直接调用，不是工具）：\n" + "\n".join(docs))
    parts.append("再次提醒：world_run 只用来做判定与记账，正文直接写出来即可。")
    return "\n\n".join(parts)

# 选项请求的指令：正文落盘后单独发一次请求，只让它产出选项 JSON。
# 必须与正文请求共用同一份 config（同 tools / 同 thinking 配置、不加 response_format），
# 否则端点侧的前缀缓存整体失效（见 llm.generate_options）。
OPTIONS_INSTRUCTION = """\
<system>
现在只做一件事：为玩家提供接下来的剧情推进选项，不要续写正文、不要调用任何工具。
要求：2-4 条；每条一句话、导向不同的发展方向、不透露玩家角色未知的信息。
只输出一个 JSON 对象，格式如下：{"options": ["选项一", "选项二"]}
没有合适的选项时输出：{"options": []}
</system>
"""


# api_type 无关的基础工具定义（只含 name/description/parameters）

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

_WRITE_FILE_TOOL_DEF = {
    "name": "write_file",
    "description": (
        "写一个 UTF-8 文本文件并返回写入结果。"
        "默认整篇覆盖（文件不存在就新建，父目录自动创建），mode=\"append\" 时追加到末尾（换行统一为 LF）。"
        "相对路径以当前会话角色卡所在目录为基准，也支持绝对路径。"
        "只改文件里的局部内容请用 edit_file，不要整篇重写。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要写的文件路径（相对角色卡所在目录，或绝对路径）。"},
            "content": {"type": "string", "description": "文件内容（整篇，或 mode=append 时要追加的部分）。"},
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "overwrite=整篇覆盖（默认），append=追加到末尾。",
            },
        },
        "required": ["file_path", "content"],
    },
}

_EDIT_FILE_TOOL_DEF = {
    "name": "edit_file",
    "description": (
        "把文件里的一段文本精确替换成另一段（比整篇重写安全，不会动到其它部分）。"
        "old_string 必须与文件内容逐字一致（含缩进与空行），且默认必须唯一，"
        "不唯一时报错并要求给出更长的上下文；replace_all=true 时替换全部匹配。"
        "不确定文件当前内容时先用 read_file 看一眼。"
        "相对路径以当前会话角色卡所在目录为基准，也支持绝对路径。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要修改的文件路径（相对角色卡所在目录，或绝对路径）。"},
            "old_string": {"type": "string", "description": "要被替换掉的原文，必须与文件内容逐字一致。"},
            "new_string": {"type": "string", "description": "替换成的新文本。"},
            "replace_all": {
                "type": "boolean",
                "description": "old_string 出现多次时是否全部替换（默认 false，此时要求唯一）。",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}

_BASH_TOOL_DEF = {
    "name": "bash",
    "description": (
        "执行一段 shell 命令（Git Bash），返回退出码与合并后的输出（stdout 在前、stderr 在后）。"
        "工作目录是当前会话角色卡所在目录。适合批量处理文件、跑脚本、查看环境这类事情；"
        "命令有超时上限（默认 60 秒，最多 600 秒），超时会被终止。不要用它跑需要交互输入的命令。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令。"},
            "timeout": {"type": "number", "description": "超时秒数，默认 60，最大 600。"},
        },
        "required": ["command"],
    },
}

# 可以直接调用的工具（选项不是工具：正文落盘后由独立请求生成）
TOOL_NAMES = ("world_run", "read_file", "write_file", "edit_file", "bash")
# 预设未声明 tools 字段时的默认工具集
DEFAULT_TOOLS = ("world_run", "read_file")
_TOOL_DEFS = {
    "world_run": _WORLD_RUN_TOOL_DEF,
    "read_file": _READ_FILE_TOOL_DEF,
    "write_file": _WRITE_FILE_TOOL_DEF,
    "edit_file": _EDIT_FILE_TOOL_DEF,
    "bash": _BASH_TOOL_DEF,
}


def preset_tools(preset: dict) -> tuple:
    """解析预设启用的工具，返回 (直接工具名单, world_run 内的绑定名)。

    白名单语义：tools 列什么就只有什么（顺序即 schema 顺序），未声明时用 DEFAULT_TOOLS。
    PTC 模式的 schema 只能有 world_run（它是该模式的唯一直接工具），tools 列表决定它程序
    内可用的绑定函数。
    """
    raw = preset.get("tools")
    if raw is None:
        names = list(DEFAULT_TOOLS)
    else:
        if not isinstance(raw, list):
            raise ValueError(f"预设 {preset.get('id')} 的 tools 必须是工具名列表: {raw!r}")
        names = []
        for n in raw:
            if n not in TOOL_NAMES:
                raise ValueError(
                    f"预设 {preset.get('id')} 的 tools 含未知工具 {n!r}（可选: {', '.join(TOOL_NAMES)}）"
                )
            if n not in names:
                names.append(n)
    bindings = [n for n in names if n != "world_run"]
    if preset.get("ptc"):
        return ["world_run"], bindings
    return names, bindings


def session_tools(state: dict) -> tuple:
    """按会话预设解析 (直接工具名单, 绑定名)。预设不存在时 KeyError（交由调用方处理）。"""
    return preset_tools(load_presets()[state["preset"]])


def apply_preset_tools(config: dict, state: dict) -> None:
    """把预设决定的生成模式与工具白名单写进扁平 config（server 在生成/预览前调用）。"""
    preset = load_presets()[state["preset"]]
    direct, bindings = preset_tools(preset)
    config["ptc"] = bool(preset.get("ptc"))
    config["tools"] = direct
    config["bindings"] = bindings


def respond_tool_text(preset: dict) -> str:
    """{{respond_tool}} 宏的内容：按预设模式与启用的工具组装任务提示词。

    宏名沿用历史叫法，内容已不含任何选项契约（选项由正文落盘后的独立请求生成）。
    末尾保留一个换行：预设里这个宏独占一行，去掉换行会跟下一段黏在一起。
    """
    direct, bindings = preset_tools(preset)
    text = _airp_prompt_ptc(bindings) if preset.get("ptc") else _airp_prompt(direct)
    return text + "\n"


def _world_run_ptc_def(bindings: list) -> dict:
    """PTC 模式 world_run 的 schema（唯一直接工具）：描述里的绑定清单按启用情况生成。"""
    sigs = {
        "read_file": "read_file(file_path, ...)",
        "write_file": "write_file(file_path, content, ...)",
        "edit_file": "edit_file(file_path, old_string, new_string, ...)",
        "bash": "bash(command, timeout, ...)",
    }
    named = [sigs[n] for n in bindings if n in sigs]
    joined = ("、".join(named[:-1]) + " 与 " + named[-1]) if len(named) > 1 else (named[0] if named else "")
    reserved = "、".join(["state", "print", *bindings])
    bind_line = f"{joined} 是代码内的绑定函数，直接在程序里调用。" if joined else ""
    return {
        "name": "world_run",
        "description": (
            "唯一能直接调用的工具：在持久的 Python 环境中执行一段代码。调用任何其它工具名都会失败。"
            + bind_line +
            "只有你 print 的内容会作为执行结果返回给你（用户看不到 print 输出），正文必须作为普通文本输出。"
            "跨调用保留：全局对象 state、顶层 def 函数、全大写全局变量（常量）三者自动持久化，跨 turn 不丢失"
            f"（{reserved} 是内置绑定名，同名定义不会被保留）。"
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


def _to_responses_format(tool: dict) -> dict:
    """把基础定义转为 Responses API 工具格式。"""
    return {"type": "function", **tool}


def _to_chat_completions_format(tool: dict) -> dict:
    """把基础定义转为 Chat Completions API 工具格式。"""
    return {"type": "function", "function": tool}


def get_tools(api_type: str, ptc: bool, direct: list, bindings: list) -> list:
    """按 api_type 返回工具 schema（顺序同预设白名单；白名单为空时返回空列表）。

    ptc 模式的 schema 只含 world_run（该模式的唯一直接工具，bindings 决定它程序内的绑定）；
    普通模式是白名单里的直接工具。
    """
    if ptc:
        base = [_world_run_ptc_def(bindings)]
    else:
        base = [_TOOL_DEFS[n] for n in direct]
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
        # 允许空表达式和"注释"
        if len(expr) == 0 or expr[0] == "#":
            return ""
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
            # PTC 实验模式：schema 只含 world_run，其它工具是代码内绑定
            "ptc": bool(meta.get("ptc")),
            # 启用的工具白名单（frontmatter 的 tools 列表）；缺省/None = world_run + read_file。
            # 合法性在 preset_tools 里校验，非法直接报错（不静默兜底）
            "tools": meta.get("tools"),
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


def resolve_options_enabled(state_value, config: dict) -> bool:
    """解析是否生成选项：会话值合法直接用，否则回退 config 默认，最后默认开启。"""
    if isinstance(state_value, bool):
        return state_value
    default = config.get("options_enabled")
    if default is None:
        return True
    if not isinstance(default, bool):
        raise ValueError(f"config.yaml 的 options_enabled 非法: {default!r}（应为 true/false）")
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
        # 创建时快照思考强度与选项开关,之后前端可按会话覆盖(config 非法时此处直接报错)
        "reasoning_effort": resolve_reasoning_effort(prev.get("reasoning_effort"), config),
        "options_enabled": resolve_options_enabled(prev.get("options_enabled"), config),
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


def _build_input_responses(state, history, draft, preset, env):
    """拼装 Responses API 的 input_items。"""
    items = [_message_item(sec["role"], render_template(sec["content"], env)) for sec in preset["sections"]]
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            # 模型的同一轮只产生一个 reasoning 项：同一段思考只在它带来的第一个调用前放一次。
            # 一轮里调了多次工具时，历史里每个调用都存着那一段思考，逐个回放会把整轮思考重复
            # 好几遍。
            prev_reasoning = None
            for tc in entry.get("tool_calls", []):
                reasoning = tc.get("reasoning") or " "
                if reasoning != prev_reasoning:
                    items.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": reasoning}],
                    })
                    prev_reasoning = reasoning
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
        else:
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
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            entry_reasoning = entry.get("reasoning") or " "
            # 工具调用：每个 tool_call 独立成 assistant(tool_calls) + tool 消息。这里每条消息都要
            # 自己带 reasoning_content（assistant 消息的字段，不像 responses 的 reasoning 项是
            # 独立公用的一份），所以一轮里多个调用会各带一份整轮思考——这是拆分的必然结果。
            for tc in entry.get("tool_calls", []):
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
        "respond_tool": respond_tool_text(preset),
    }
    if api_type == "chat_completions":
        return _build_input_chat_completions(state, history, draft, preset, env)
    return _build_input_responses(state, history, draft, preset, env)


def build_options_input(state: dict, history: list, content: str, tool_calls: list, reasoning: str) -> list:
    """拼装「选项请求」的输入：正文请求的输入 + 本轮的助手回复 + 选项指令。

    history 是不含本轮的那段历史，本轮回复按 content/tool_calls/reasoning 现场拼出来，
    于是这次请求的输入就是正文请求输入的严格续写：端点侧的前缀缓存能整段命中（实测约 96%），
    每轮只多付末尾那几十个 token。

    必须与正文请求共用同一份 config（同 model / 同 tools / 同 thinking 配置、不加
    response_format），否则前缀整体失配——这是硬约束，不是优化。
    """
    entry = {
        "role": "assistant",
        "content": content,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
    }
    items = list(build_input(state, history + [entry]))
    if effective_config(state, load_config()).get("api_type", "responses") == "chat_completions":
        items.append({"role": "user", "content": OPTIONS_INSTRUCTION})
    else:
        items.append(_message_item("user", OPTIONS_INSTRUCTION))
    return items


def parse_options(text: str) -> list:
    """从选项请求的回复里解析出选项列表；解析不出合法结构时抛 ValueError。

    模型偶尔会带代码围栏或前后废话，所以先剥围栏，再退一步取第一个 { 到最后一个 }。
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict) or not isinstance(data.get("options"), list):
        raise ValueError('模型输出里没有 {"options": [...]} 结构')
    return [str(o) for o in data["options"]]
