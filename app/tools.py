"""read_file 工具（移植自 AIRP 预设的 file-read 插件）与工具分发。"""
import json
import logging
from pathlib import Path

from app import world
from app.core import ROOT

logger = logging.getLogger("airp.tools")

# 默认/最大单次返回行数
READ_LIMIT = 2000
# 单行最长保留字符数,超出截断
MAX_LINE_LENGTH = 2000
# 单次调用输出字节上限(约 50KB)
MAX_BYTES = 50 * 1024
# 超过该大小的文件拒绝读取
MAX_FILE_BYTES = 20 * 1024 * 1024


def read_file(file_path: str, offset: int = 1, limit: int = READ_LIMIT) -> str:
    """读取一个 UTF-8 文本文件,返回带分页信息的内容窗口。

    相对路径以项目根目录为基准,也接受绝对路径。所有错误以文本返回,
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
            absolute = ROOT / absolute
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


def execute_tool(session: str, name: str, arguments: str) -> str:
    """执行一个非 respond 的工具调用,返回作为 function_call_output 的文本。

    arguments 是模型给出的 JSON 字符串;解析失败/未知工具同样以文本返回。
    """
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as e:
        return f"错误：工具参数不是合法 JSON：{e}"
    try:
        if name == "world_run":
            return world.run(session, str(args.get("program", "")), dry=args.get("dry") is True)
        if name == "read_file":
            return read_file(args.get("file_path", ""), args.get("offset", 1), args.get("limit", READ_LIMIT))
        return f"错误：未知工具 {name}"
    except Exception as e:
        logger.exception("工具 %s 执行失败", name)
        return f"错误：{type(e).__name__}: {e}"
