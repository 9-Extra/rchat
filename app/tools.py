"""工具实现（read_file/write_file/edit_file/bash）与工具分发。

文件类工具的相对路径以当前会话角色卡所在目录为基准（也接受绝对路径），bash 在该目录下执行。
启用哪些工具由会话预设 frontmatter 的 tools 白名单决定（见 core.preset_tools）。
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app import core, world

logger = logging.getLogger("airp.tools")

# 默认/最大单次返回行数
READ_LIMIT = 2000
# 单行最长保留字符数,超出截断
MAX_LINE_LENGTH = 2000
# 单次调用输出字节上限(约 50KB)
MAX_BYTES = 50 * 1024
# 超过该大小的文件拒绝读取
MAX_FILE_BYTES = 20 * 1024 * 1024

# bash：默认与最大超时秒数（超时的命令会被终止）
BASH_TIMEOUT = 60
BASH_MAX_TIMEOUT = 600
# bash 单次返回的字符上限
BASH_MAX_CHARS = 20000


def read_file(
    file_path: str, offset: int = 1, limit: int = READ_LIMIT, base_dir: str | None = None
) -> str:
    """读取一个 UTF-8 文本文件,返回带分页信息的内容窗口。

    相对路径以 base_dir 为基准（工具调用时传当前会话角色卡所在目录）,
    未提供 base_dir 时以项目根目录为基准。也接受绝对路径。所有错误以文本返回,
    由模型自行纠正(与原插件把异常作为工具结果的行为一致)。
    """
    try:
        file_path = str(file_path).strip()
        if not file_path:
            return "错误：file_path 不能为空"
        for v, label in ((offset, "offset"), (limit, "limit")):
            if not isinstance(v, int) or v < 1:
                return f"错误：{label} 必须是正整数"
        if limit > READ_LIMIT:
            return f"错误：limit 不能超过 {READ_LIMIT}"
        absolute = Path(file_path)
        if not absolute.is_absolute():
            root = Path(base_dir) if base_dir else core.ROOT
            absolute = root / absolute
        try:
            size = absolute.stat().st_size
        except OSError:
            return f'错误：无法读取 "{absolute}"：文件不存在'
        if not absolute.is_file():
            return f'错误：无法读取 "{absolute}"：不是普通文件'
        if size > MAX_FILE_BYTES:
            mb = MAX_FILE_BYTES // 1024 // 1024
            return f'错误：无法读取 "{absolute}"：文件超过 {mb}MB，请先拆分成较小的文件'
        text = absolute.read_text(encoding="utf-8")
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if offset > total and not (total == 0 and offset == 1):
        return f"错误：offset {offset} 超出范围：该文件共 {total} 行"
    out = []
    out_bytes = 0
    truncated = False
    for i in range(offset - 1, min(total, offset - 1 + limit)):
        line = lines[i]
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + "… (行已截断)"
        b = len(line.encode("utf-8")) + (1 if out else 0)
        if out_bytes + b > MAX_BYTES:
            truncated = True
            break
        out_bytes += b
        out.append(line)
    end_line = offset - 1 + len(out)
    if truncated:
        footer = f"(输出已达字节上限，仅显示至第 {end_line} 行；用 offset={end_line + 1} 继续读取。)"
    elif end_line < total:
        footer = f"(共 {total} 行，当前显示至第 {end_line} 行；用 offset={end_line + 1} 继续读取。)"
    else:
        footer = f"(文件结束 - 共 {total} 行)"
    body = "\n".join(out)
    return f"<path>{absolute}</path>\n<content>\n{body + chr(10) + chr(10) if body else ''}</content>\n{footer}"


def _resolve(file_path, base_dir) -> tuple:
    """把工具给的路径解析成绝对路径：相对路径以 base_dir 为基准。返回 (路径, 错误文本)。"""
    text = str(file_path).strip() if file_path is not None else ""
    if not text:
        return None, "错误：file_path 不能为空"
    absolute = Path(text)
    if not absolute.is_absolute():
        absolute = (Path(base_dir) if base_dir else core.ROOT) / absolute
    return absolute, ""


def _count_lines(text: str) -> int:
    """文本行数（末尾换行不算多出一行）。"""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def write_file(
    file_path: str, content: str = "", mode: str = "overwrite", base_dir: str | Path | None = None
) -> str:
    """写一个 UTF-8 文本文件：mode=overwrite 新建/整篇覆盖，mode=append 追加到末尾。

    父目录不存在会自动创建。写入时换行统一为 LF（避免 Windows 上悄悄变成 CRLF）。
    所有错误以文本返回，由模型自行纠正。
    """
    if mode not in ("overwrite", "append"):
        return f"错误：mode 只能是 overwrite 或 append，收到 {mode!r}"
    if not isinstance(content, str):
        return f"错误：content 必须是字符串，收到 {type(content).__name__}"
    absolute, error = _resolve(file_path, base_dir)
    if error:
        return error
    try:
        if absolute.exists() and not absolute.is_file():
            return f'错误：无法写入 "{absolute}"：不是普通文件'
        absolute.parent.mkdir(parents=True, exist_ok=True)
        text = content.replace("\r\n", "\n").replace("\r", "\n")
        with absolute.open("a" if mode == "append" else "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"
    action = "已追加到" if mode == "append" else "已写入"
    return f'{action} "{absolute}"（本次 {_count_lines(text)} 行，{len(text)} 字符）'


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    base_dir: str | Path | None = None,
) -> str:
    """把文件里的一段文本精确替换成另一段（old_string 必须逐字匹配）。

    默认要求 old_string 在文件中唯一，否则报错让模型给出更长的上下文；replace_all=True 时
    全部替换。原文件的换行风格（LF / CRLF）保持不变。所有错误以文本返回。
    """
    if not isinstance(old_string, str) or not old_string:
        return "错误：old_string 不能为空"
    if not isinstance(new_string, str):
        return f"错误：new_string 必须是字符串，收到 {type(new_string).__name__}"
    if old_string == new_string:
        return "错误：old_string 与 new_string 相同，等于没有任何改动"
    absolute, error = _resolve(file_path, base_dir)
    if error:
        return error
    try:
        if not absolute.exists():
            return f'错误：无法修改 "{absolute}"：文件不存在（新建文件请用 write_file）'
        if not absolute.is_file():
            return f'错误：无法修改 "{absolute}"：不是普通文件'
        raw = absolute.read_text(encoding="utf-8", newline="")
    except UnicodeDecodeError:
        return f'错误：无法修改 "{absolute}"：不是 UTF-8 文本文件'
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"

    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    old = old_string.replace("\r\n", "\n").replace("\r", "\n")
    new = new_string.replace("\r\n", "\n").replace("\r", "\n")

    count = text.count(old)
    if count == 0:
        return (
            f'错误：在 "{absolute}" 里找不到 old_string 的精确匹配'
            f"（文件共 {_count_lines(text)} 行）。请先确认文本逐字一致，必要时用 read_file 看当前内容。"
        )
    if count > 1 and not replace_all:
        return (
            f'错误：old_string 在 "{absolute}" 里出现 {count} 次，不唯一。'
            f"请把上下文写长一些使其唯一，或传 replace_all=true 全部替换。"
        )
    replaced = count if replace_all else 1
    out = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        absolute.write_text(out, encoding="utf-8", newline="\r\n" if crlf else "\n")
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"
    return f'已替换 "{absolute}" 中的 {replaced} 处（现共 {_count_lines(out)} 行）'


_SHELL: str | None = None
# 探测失败时记录试过的位置，供报错提示用
_SHELL_TRIED: tuple = ()
_SHELL_PROBED = False

# Windows 上 Git for Windows 默认只把 ...\Git\cmd 写进 PATH，而 bash.exe 在 ...\Git\bin，
# 所以 PATH 查不到时按这些常见安装位置兜底（%VAR% 会被展开）
_GIT_BASH_DIRS = (
    r"C:\Program Files\Git\bin",
    r"C:\Program Files (x86)\Git\bin",
    r"%LOCALAPPDATA%\Programs\Git\bin",
    r"%USERPROFILE%\scoop\apps\git\current\bin",
)


def _shell() -> str | None:
    """定位可用的 bash（首次调用时探测并缓存）。"""
    global _SHELL, _SHELL_TRIED, _SHELL_PROBED
    if not _SHELL_PROBED:
        found = shutil.which("bash") or shutil.which("bash.exe") or shutil.which("sh")
        tried: list = []
        if found is None and sys.platform == "win32":
            for directory in _GIT_BASH_DIRS:
                candidate = Path(os.path.expandvars(directory)) / "bash.exe"
                tried.append(str(candidate))
                if candidate.is_file():
                    found = candidate
                    break
        _SHELL = str(found) if found else None
        _SHELL_TRIED = tuple(tried)
        _SHELL_PROBED = True
    return _SHELL


def bash(command: str, timeout: int = BASH_TIMEOUT, cwd: str | Path | None = None) -> str:
    """在工作目录里跑一段 shell 命令，返回退出码与合并后的输出。

    输出超长时只保留开头；超时的命令会被终止（Windows 上可能留下孙进程）。所有错误以
    文本返回，由模型自行纠正。
    """
    if not isinstance(command, str) or not command.strip():
        return "错误：command 不能为空"
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= BASH_MAX_TIMEOUT
    ):
        return f"错误：timeout 必须是 1..{BASH_MAX_TIMEOUT} 之间的整数"
    shell = _shell()
    if shell is None:
        tried = f"；已尝试：{', '.join(_SHELL_TRIED)}" if _SHELL_TRIED else ""
        return (
            "错误：找不到 bash（Windows 上需要安装 Git Bash，"
            f"并确保 bash 在 PATH 中，或存在于 Git 的 bin 目录）{tried}"
        )
    workdir = Path(cwd) if cwd else core.ROOT
    workdir.mkdir(parents=True, exist_ok=True)

    def render(output: str, exit_code) -> str:
        output = output[:BASH_MAX_CHARS]
        note = "（输出已截断，只保留开头部分）" if len(output) >= BASH_MAX_CHARS else ""
        return (
            f"<cwd>{workdir}</cwd>\n<exit_code>{exit_code}</exit_code>\n"
            f"<output>\n{output}{note}</output>"
        )

    try:
        proc = subprocess.run(
            [shell, "-c", command],
            cwd=str(workdir),
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") + (e.stderr or "")
        return (
            f"错误：命令超过 {timeout}s 未结束，已被终止。\n<output>\n"
            f"{partial[:BASH_MAX_CHARS]}</output>"
        )
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"
    return render((proc.stdout or "") + (proc.stderr or ""), proc.returncode)


def _session_card_dir(session: str):
    """会话工具调用的相对路径基准 / bash 工作目录：角色卡所在目录。返回 (目录, 错误文本)。"""
    try:
        state = core.load_state(session)
        card = core.load_cards().get(state.get("card", ""))
        base_dir = Path(card["path"]).parent if card and card.get("path") else None
    except Exception as e:
        return None, f"错误：无法确定角色卡目录：{type(e).__name__}: {e}"
    return (str(base_dir) if base_dir else None), ""


def _unknown_tool(session: str, name: str) -> str:
    """未知/未启用工具的错误文本：尽量列出本会话启用的工具，供模型自我纠正。"""
    try:
        state = core.load_state(session)
        direct, bindings = core.preset_tools(core.load_presets()[state["preset"]])
    except Exception:
        return (
            f"错误：未知工具 {name}（只有 schema 中声明的工具可以直接调用；"
            "绑定函数请在 world_run 代码内使用）"
        )
    hint = f"本会话启用的直接工具：{'、'.join(direct) if direct else '无'}；respond 始终可用"
    if "world_run" in direct and bindings:
        hint += f"；{'、'.join(bindings)} 是 world_run 程序内的绑定函数，不能直接调用"
    return f"错误：未知工具 {name}（{hint}）"


async def execute_tool(session: str, name: str, arguments: str):
    """执行一个非 respond 的工具调用。

    返回 (function_call_output 文本, respond_info)；respond_info 仅当 world_run
    程序内调用了 respond 绑定时非 None（{"options": [...]} 或 {"error": ...}），
    是 PTC 模式的回合收尾信号。
    arguments 是模型给出的 JSON 字符串;解析失败/未知工具同样以文本返回。
    文件类工具的相对路径以当前会话角色卡所在目录为基准，bash 在该目录下执行；
    只有会话预设 tools 白名单里启用的工具才会执行（见 core.preset_tools）。
    """
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as e:
        return f"错误：工具参数不是合法 JSON：{e}", None
    if not isinstance(args, dict):
        return f"错误：工具参数必须是 JSON 对象，收到 {type(args).__name__}", None
    try:
        state = core.load_state(session)
        direct, _bindings = core.preset_tools(core.load_presets()[state["preset"]])
    except Exception as e:
        return f"错误：无法确定本会话启用的工具：{type(e).__name__}: {e}", None
    if name not in direct:
        return _unknown_tool(session, name), None
    if name == "world_run":
        try:
            return world.run(session, str(args.get("program", "")), dry=args.get("dry") is True)
        except Exception as e:
            logger.exception("工具 %s 执行失败", name)
            return f"错误：{type(e).__name__}: {e}", None
    base_dir, error = _session_card_dir(session)
    if error:
        return error, None
    try:
        if name == "read_file":
            return read_file(
                args.get("file_path", ""),
                args.get("offset", 1),
                args.get("limit", READ_LIMIT),
                base_dir=base_dir,
            ), None
        if name == "write_file":
            return write_file(
                args.get("file_path", ""),
                args.get("content", ""),
                args.get("mode", "overwrite"),
                base_dir=base_dir,
            ), None
        if name == "edit_file":
            return edit_file(
                args.get("file_path", ""),
                args.get("old_string", ""),
                args.get("new_string", ""),
                args.get("replace_all") is True,
                base_dir=base_dir,
            ), None
        # bash：subprocess 阻塞执行，放进工作线程，避免长命令卡住事件循环
        return await asyncio.to_thread(
            bash, args.get("command", ""), args.get("timeout", BASH_TIMEOUT), base_dir
        ), None
    except Exception as e:
        logger.exception("工具 %s 执行失败", name)
        return f"错误：{type(e).__name__}: {e}", None
