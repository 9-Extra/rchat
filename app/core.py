"""预设/角色卡解析、session 存储、上下文拼装。"""
import json
import re
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PRESET_DIR = ROOT / "preset"
GAMES_DIR = ROOT / "games"
SESSIONS_DIR = ROOT / "sessions"

# AIRP 任务提示词：使 AI 明确自身任务（只通过 respond 工具输出）。
# 通过预设中的 {{airp_task}} 宏显式插入，代码不会自动注入任何额外系统提示词。
# 角色设定由预设中的 {{game_setting}} 宏注入，预设内部已用 <dream_setting> 等标签包裹。
# 实际上用户可以看到思考内容，但不需要告诉模型
AIRP_PROMPT = """<airp_task>
你的唯一输出通道是 respond 工具：
- 你必须通过调用 respond 工具输出剧情正文（content）与后续选项（options）。
- 只有工具调用内部的内容对用户可见，工具之外的任何文本用户都看不到。
- 用户的新一轮输入会作为你上一次工具调用的结果（tool 消息）返回给你，你需要将其融入后续剧情。
</airp_task>"""

RESPOND_TOOL = {
    "type": "function",
    "function": {
        "name": "respond",
        "description": "输出剧情正文与后续选项。只有此工具内的内容对用户可见。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "剧情正文"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "0 到多个剧情推进选项，可为空数组",
                },
            },
            "required": ["content"],
        },
    },
}

SECTION_RE = re.compile(
    r'<preset_section\s+role="(system|user|assistant)"\s*>(.*?)</preset_section>', re.S
)
SETTING_RE = re.compile(r"<game_setting>(.*?)</game_setting>", re.S)
BEGINNING_RE = re.compile(r"<game_beginning>(.*?)</game_beginning>", re.S)
NAME_RE = re.compile(r"[\w\-一-鿿]+")


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
        presets[p.stem] = {
            "id": p.stem,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description") or "",
            "sections": sections,
        }
    return presets


def load_cards() -> dict:
    cards = {}
    for p in sorted(GAMES_DIR.glob("*.md")):
        meta, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        m = SETTING_RE.search(body)
        cards[p.stem] = {
            "id": p.stem,
            "name": meta.get("name") or p.stem,
            "description": meta.get("description") or "",
            "setting": m.group(1).strip() if m else "",
            "beginnings": [b.group(1).strip() for b in BEGINNING_RE.finditer(body)],
        }
    return cards


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


# ---------- session 存储 ----------

def _session_dir(name: str) -> Path:
    if not NAME_RE.fullmatch(name):
        raise ValueError("非法 session 名称")
    return SESSIONS_DIR / name


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


# ---------- 上下文拼装 ----------

def build_messages(state: dict, history: list, draft=None) -> list:
    """拼装将发送给 AI 的完整上下文。每次调用都用当前保存的开局重新渲染预设。"""
    preset = load_presets()[state["preset"]]
    card = load_cards()[state["card"]]
    messages = []
    for sec in preset["sections"]:
        content = (
            sec["content"]
            .replace("{{game_setting}}", card["setting"])
            .replace("{{game_beginning}}", state["beginning_text"])
            .replace("{{airp_task}}", AIRP_PROMPT)
        )
        messages.append({"role": sec["role"], "content": content})
    # 对话历史：assistant 块 -> respond 工具调用；user 块 -> 工具调用结果
    call_n = 0
    for entry in history:
        if entry["role"] == "assistant":
            call_n += 1
            args = {"content": entry["content"]}
            if entry.get("options"):
                args["options"] = entry["options"]
            messages.append({
                "role": "assistant",
                # 思考模式要求把 reasoning_content 原样传回
                "reasoning_content": entry.get("reasoning", ""),
                "tool_calls": [{
                    "id": f"call_{call_n}",
                    "type": "function",
                    "function": {"name": "respond", "arguments": json.dumps(args, ensure_ascii=False)},
                }],
            })
        else:
            messages.append({"role": "tool", "tool_call_id": f"call_{call_n}", "content": entry["content"]})
    # 输入框中的本次输入：作为最后一个工具调用的结果拼入
    if draft and history and history[-1]["role"] == "assistant":
        messages.append({"role": "tool", "tool_call_id": f"call_{call_n}", "content": draft})
    return messages
