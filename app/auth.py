"""服务端鉴权：账号手写在 config.yaml 的 auth 段，登录态是「服务端会话表 + HttpOnly Cookie」。

配置（config.yaml）：

    auth:
      enabled: true
      users:
        alice: "密码"
        bob: "密码"
      session_ttl_days: 30   # 缺省 30
      cookie_secure: false   # 走 https 反代时置 true（Cookie 只经 TLS 发送）

取舍：
- auth.enabled 为假时整套逻辑不生效（当前用户为 None），会话键不带用户名前缀，
  行为与改造前完全一致——本地开发不受影响。
- 密码在 config.yaml 里是明文（这个文件本来就存着 API key，且不入库、建议 chmod 600）。
  账户增删改改完保存即生效，不需要重启（load_config 按 mtime 缓存）。
- 会话表在内存里，同时落一份 auth_sessions.json（不入库），重启后不必重新登录；
  服务端可单方面吊销（改密码不会自动吊销——要踢人就删掉该文件里的条目或重启）。
- 登录失败按「用户名 + 来源」限速，防在线爆破。
"""
import hmac
import json
import logging
import secrets
import time

from app import core

logger = logging.getLogger("airp.auth")

COOKIE_NAME = "airp_session"
SESSIONS_FILE = core.ROOT / "auth_sessions.json"

DEFAULT_TTL_DAYS = 30
# 登录失败限速：窗口内失败次数达到上限就拒绝（按 用户名 + 来源 IP 计）
FAIL_WINDOW = 300.0
FAIL_MAX = 5

# token -> {"user": str, "exp": float}
_sessions: dict = {}
# "用户名\n来源" -> [失败时间戳]
_fails: dict = {}
_loaded = False


def auth_settings(config: dict | None = None) -> dict:
    """解析并校验 config.yaml 的 auth 段。配置非法直接报错（不静默兜底）。"""
    config = core.load_config() if config is None else config
    raw = config.get("auth") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml 的 auth 不是映射: {raw!r}")
    users = raw.get("users") or {}
    if not isinstance(users, dict):
        raise ValueError(f"config.yaml 的 auth.users 不是映射: {users!r}")
    for name, password in users.items():
        if not isinstance(name, str) or not name.strip() or "/" in name:
            raise ValueError(f"config.yaml 的 auth.users 用户名非法: {name!r}（不能为空、不能含 /）")
        if not isinstance(password, str) or not password:
            raise ValueError(f"config.yaml 的 auth.users.{name} 的密码必须是非空字符串")
    ttl = raw.get("session_ttl_days", DEFAULT_TTL_DAYS)
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        raise ValueError(f"config.yaml 的 auth.session_ttl_days 非法: {ttl!r}")
    return {
        "enabled": bool(raw.get("enabled")),
        "users": users,
        "ttl": float(ttl) * 86400,
        "cookie_secure": bool(raw.get("cookie_secure", False)),
    }


def _save_sessions() -> None:
    try:
        core._atomic_write_text(
            SESSIONS_FILE, json.dumps(_sessions, ensure_ascii=False, indent=2)
        )
    except OSError as e:
        # 落盘失败只影响「重启后免登录」，当前进程内的登录态照常可用
        logger.warning("登录态落盘失败（重启后需要重新登录）: %s", e)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    now = time.time()
    _sessions.update(
        {t: v for t, v in data.items() if isinstance(v, dict) and v.get("exp", 0) > now}
    )


def session_user(token: str | None) -> str | None:
    """校验 cookie 里的 token，返回用户名；无效/过期/账号已删则返回 None。"""
    _ensure_loaded()
    if not token:
        return None
    entry = _sessions.get(token)
    if entry is None:
        return None
    if entry.get("exp", 0) <= time.time():
        _sessions.pop(token, None)
        _save_sessions()
        return None
    user = entry.get("user")
    # 账号从 config.yaml 删掉后，已发的 cookie 立刻失效
    if not isinstance(user, str) or user not in auth_settings()["users"]:
        _sessions.pop(token, None)
        _save_sessions()
        return None
    return user


def login(username: str, password: str, client: str = "") -> tuple:
    """校验用户名密码并开一个会话，返回 (token, 错误信息)；失败时 token 为 None。"""
    cfg = auth_settings()
    if not cfg["enabled"]:
        return None, "服务端未启用鉴权"
    key = f"{username}\n{client}"
    now = time.time()
    fails = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW]
    if len(fails) >= FAIL_MAX:
        _fails[key] = fails
        return None, f"登录失败次数过多，请 {int(FAIL_WINDOW // 60)} 分钟后再试"
    stored = cfg["users"].get(username)
    ok = isinstance(stored, str) and hmac.compare_digest(
        stored.encode("utf-8"), password.encode("utf-8")
    )
    if not ok:
        fails.append(now)
        _fails[key] = fails
        return None, "用户名或密码错误"
    _fails.pop(key, None)
    # 顺手清理两张全局表：登录是低频操作，在这里做够用了。
    # 不清理的话 _fails 会被伪造用户名的爆破请求撑大（每个不存在的用户名留一条），
    # _sessions 则会在反复登录/换设备时一直留着早已过期的 token（还会写进 auth_sessions.json）。
    if len(_fails) > 64:
        for k in [k for k, v in _fails.items() if not any(now - t < FAIL_WINDOW for t in v)]:
            _fails.pop(k, None)
    for t in [t for t, v in _sessions.items() if v.get("exp", 0) <= now]:
        _sessions.pop(t, None)
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"user": username, "exp": now + cfg["ttl"]}
    _save_sessions()
    return token, ""


def logout(token: str | None) -> None:
    if token and _sessions.pop(token, None) is not None:
        _save_sessions()
