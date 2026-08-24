"""world_run 的持久 Python 执行环境（移植自 AIRP 预设的 node:vm 版）。

每个会话一个 exec 命名空间：预置全局对象 state（dict）与 print。
持久化到 sessions/<name>/world/：
- state.json   当前 state（JSON）
- lib.py       模型定义的顶层函数源码（重放恢复）
- snapshots.json  按历史长度存档的 {state, lib} 快照，会话回滚/重生成时
  状态跟着回到对应位置（server 在截断 history 后调用 sync）。

语义（与原 JS 版对齐）：
- 原子执行：代码出错自动回滚（state、本次新增/覆盖的函数），不留半更新；
- dry=True 试运行：返回完整结果但一切变化不生效；
- 约定式整理：定义了全局函数 normalize() 时，每次成功执行后、生成 diff 前
  自动调用一次；normalize 出错只回滚它自己的改动；
- 每次返回 state 的变化 diff；查看具体值用 print(...)。
- 无超时保护：预设可信，假设模型不会写出死循环。
"""
import ast
import copy
import json
import logging

from app.core import SESSIONS_DIR, load_history

logger = logging.getLogger("airp.world")

DIFF_ENTRY_CAP = 60
LOG_LINE_CAP = 2000
LOG_COUNT_CAP = 100

# session 目录名 -> runtime: {"ns": dict, "lib": {name: source},
#   "committed": {"state": ..., "lib": ...}, "turn_len": int, "logs": list|None}
_runtimes: dict = {}


def _world_dir(name: str):
    return SESSIONS_DIR / name / "world"


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _json_safe(value) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
        return True
    except (TypeError, ValueError):
        return False


def _diff(before, after, base="", out=None):
    """递归比较两个 JSON 值,产出有界 diff 条目。"""
    if out is None:
        out = []
    if len(out) >= DIFF_ENTRY_CAP or before == after:
        return out
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            if len(out) >= DIFF_ENTRY_CAP:
                break
            p = f"{base}.{key}" if base else key
            if key not in before:
                out.append({"path": p, "kind": "added", "to": after[key]})
            elif key not in after:
                out.append({"path": p, "kind": "removed", "from": before[key]})
            else:
                _diff(before[key], after[key], p, out)
    elif isinstance(before, list) and isinstance(after, list):
        out.append({"path": base or "(root)", "kind": "changed", "from": before, "to": after})
    else:
        out.append({"path": base or "(root)", "kind": "changed", "from": before, "to": after})
    return out


def _extract_defs(program: str) -> dict:
    """提取程序中顶层 def 的 {name: 源码}。语法错误返回 None(由 exec 报错)。"""
    try:
        tree = ast.parse(program)
    except SyntaxError:
        return None
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(program, node)
            if src:
                out[node.name] = src
    return out


def _push_log(rt, text):
    logs = rt["logs"]
    if logs is None:
        return
    if len(logs) >= LOG_COUNT_CAP:
        if len(logs) == LOG_COUNT_CAP:
            logs.append("（日志过多，后续输出已省略）")
        return
    logs.append(text if len(text) <= LOG_LINE_CAP else text[:LOG_LINE_CAP] + "…（截断）")


def _print_part(p):
    return p if isinstance(p, str) else repr(p)


def _fresh_ns(rt):
    """重建命名空间：重放 lib 函数源码，恢复 committed state。"""
    ns = {
        "state": copy.deepcopy(rt["committed"]["state"]),
        "print": lambda *parts: _push_log(rt, " ".join(_print_part(p) for p in parts)),
    }
    for fname, src in rt["committed"]["lib"].items():
        try:
            exec(src, ns)
        except Exception:
            logger.warning("world lib 重放失败 %s: %s", fname, src[:80])
    rt["lib"] = dict(rt["committed"]["lib"])
    return ns


def _load_committed(name: str):
    """从磁盘读 committed 状态（lib.py 源码 + state.json）。"""
    d = _world_dir(name)
    lib = {}
    lib_file = d / "lib.py"
    if lib_file.exists():
        src = lib_file.read_text(encoding="utf-8")
        defs = _extract_defs(src)
        if defs:
            lib = defs
    state = {}
    state_file = d / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("world state.json 损坏,按空状态恢复: %s", state_file)
    return {"state": state, "lib": lib}


def runtime_for(name: str):
    rt = _runtimes.get(name)
    if rt is None:
        rt = {"committed": _load_committed(name), "turn_len": None, "logs": None}
        rt["turn_len"] = _current_len(name)
        rt["ns"] = _fresh_ns(rt)
        _runtimes[name] = rt
    return rt


def _current_len(name: str) -> int:
    return len(load_history(name))


def _snapshots(name: str) -> dict:
    f = _world_dir(name) / "snapshots.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_disk(name: str, committed: dict, turn_len: int):
    d = _world_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps(committed["state"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "lib.py").write_text("\n\n".join(committed["lib"].values()), encoding="utf-8")
    snaps = {k: v for k, v in _snapshots(name).items() if int(k) <= turn_len}
    snaps[str(turn_len)] = committed
    (d / "snapshots.json").write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")


def sync(name: str, turn_len: int):
    """把运行时对齐到指定历史长度（回滚/重生成/打断后调用）。

    历史被截断时从快照恢复对应状态并剪掉更晚的快照;长度未变但内存中有
    未提交改动(上一轮被打断/失败)时回到 committed。
    """
    rt = _runtimes.get(name)
    if rt is not None and turn_len == rt["turn_len"]:
        rt["ns"] = _fresh_ns(rt)
        return
    snap = _snapshots(name).get(str(turn_len))
    if snap is not None:
        committed = snap
        _write_disk(name, committed, turn_len)
    elif rt is not None:
        committed = rt["committed"]  # 无快照(功能上线前的旧会话):保持当前状态
    else:
        return  # 运行时与快照都不存在,首次 world_run 时按磁盘现状加载
    if rt is None:
        rt = {"committed": committed, "turn_len": turn_len, "logs": None}
        _runtimes[name] = rt
    else:
        rt["committed"] = committed
        rt["turn_len"] = turn_len
    rt["ns"] = _fresh_ns(rt)


def abort_turn(name: str):
    """本轮生成被打断/失败：丢弃内存中未提交的改动,回到 committed。"""
    rt = _runtimes.get(name)
    if rt is not None:
        rt["ns"] = _fresh_ns(rt)


def commit_turn(name: str, turn_len: int):
    """本轮生成成功落盘：把当前状态作为 committed 写入磁盘与快照。"""
    rt = _runtimes.get(name)
    if rt is None:
        return
    if not _json_safe(rt["ns"]["state"]):
        logger.warning("会话 %s 的 state 不可 JSON 序列化,本次不提交", name)
        return
    rt["committed"] = {"state": _json_copy(rt["ns"]["state"]), "lib": dict(rt["lib"])}
    rt["turn_len"] = turn_len
    _write_disk(name, rt["committed"], turn_len)


def fork(src: str, dst: str, turn_len: int):
    """fork：把 src 在 turn_len 处的世界状态（及更早的快照）复制到新会话 dst。

    优先取该历史长度的快照；没有快照（功能上线前的旧会话）时与 sync 一样
    退化为复制当前 committed 状态。只读 src，不影响其运行时。
    """
    src_dir = _world_dir(src)
    if not src_dir.exists():
        return  # 源会话从未使用过 world_run，没有世界状态可复制
    snaps = _snapshots(src)
    committed = snaps.get(str(turn_len)) or _load_committed(src)
    d = _world_dir(dst)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(
        json.dumps(committed["state"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (d / "lib.py").write_text("\n\n".join(committed["lib"].values()), encoding="utf-8")
    # 保留 fork 点及更早的快照，新会话内继续回滚/重生成时状态仍能对齐
    dst_snaps = {k: v for k, v in snaps.items() if int(k) <= turn_len}
    dst_snaps[str(turn_len)] = committed
    (d / "snapshots.json").write_text(json.dumps(dst_snaps, ensure_ascii=False), encoding="utf-8")


def drop(name: str):
    _runtimes.pop(name, None)


def run(name: str, program: str, dry: bool = False) -> str:
    """在指定会话的持久环境中执行 program,返回模型可见的结果文本。"""
    rt = runtime_for(name)
    ns = rt["ns"]
    # 快照:浅拷贝命名空间(函数等)+ state 深拷贝 + lib 副本,供回滚
    ns_backup = dict(ns)
    state_backup = _json_copy(ns["state"]) if _json_safe(ns["state"]) else None
    lib_backup = dict(rt["lib"])
    logs = []
    rt["logs"] = logs
    error = None
    hook_errors = []
    try:
        exec(program, ns)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    if error is None:
        # 用户代码成功:收集顶层 def 进 lib
        defs = _extract_defs(program)
        if defs:
            rt["lib"].update(defs)
    if error is None and callable(ns.get("normalize")):
        # 约定式整理:normalize 出错只回滚它自己的改动
        hook_ns = dict(ns)
        hook_state = _json_copy(ns["state"]) if _json_safe(ns["state"]) else None
        try:
            ns["normalize"]()
        except Exception as e:
            hook_errors.append(f"{type(e).__name__}: {e}（normalize 的改动已回滚）")
            kept_state = ns["state"]
            ns.clear()
            ns.update(hook_ns)
            ns["state"] = hook_state if hook_state is not None else kept_state
    rt["logs"] = None
    if error is not None or dry:
        # 出错回滚 / 试运行不提交:还原命名空间、state、lib
        ns.clear()
        ns.update(ns_backup)
        if state_backup is not None:
            ns["state"] = state_backup
        rt["lib"] = lib_backup
    # diff 在恢复之后仍按「假如提交」计算
    after = _json_copy(ns_backup["state"]) if (error is not None or dry) else None
    if error is None and not dry:
        after = _json_copy(ns["state"]) if _json_safe(ns["state"]) else None
    diff = _diff(state_backup, after) if state_backup is not None and after is not None else []
    parts = []
    if error:
        parts.append(f"执行出错：{error}")
    if logs:
        parts.append("日志：\n" + "\n".join(logs))
    if diff:
        lines = []
        for d in diff:
            if d["kind"] == "added":
                lines.append(f"  + {d['path']} = {json.dumps(d['to'], ensure_ascii=False)}")
            elif d["kind"] == "removed":
                lines.append(f"  - {d['path']}（原 {json.dumps(d['from'], ensure_ascii=False)}）")
            else:
                lines.append(
                    f"  ~ {d['path']}: {json.dumps(d['from'], ensure_ascii=False)}"
                    f" → {json.dumps(d['to'], ensure_ascii=False)}"
                )
        parts.append("state 变化：\n" + "\n".join(lines))
    elif not error:
        parts.append("state 无变化。")
    if hook_errors:
        parts.append("normalize 错误：\n" + "\n".join(f"  - {e}" for e in hook_errors))
    if error and state_backup is not None:
        parts.append("已回滚：执行出错，state 与函数定义均已恢复到执行前，未留下半更新。")
    if state_backup is None:
        parts.append("（注意：执行前 state 含不可 JSON 序列化的内容，变化无法追踪，出错也无法回滚。）")
    if dry:
        parts.append("（试运行：以上变化与函数定义均未生效。）")
    return "\n\n".join(parts)
