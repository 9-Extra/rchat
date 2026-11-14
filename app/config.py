"""配置模型：`config.yaml` 的静态配置（Settings）与单次请求实际生效的配置（TurnConfig）。

分成两层不是过度设计，而是因为这些东西的生命周期不同：

- `Settings`：config.yaml 里的东西，按文件 mtime 缓存、全进程共用、构造后不可变；
- `TurnConfig`：一次请求实际要用的值 —— 选中端点 + 会话覆盖（温度/上限/思考强度）
  + 预设派生的工具白名单，每次请求由 `core.build_turn()` 现场构造。

所有默认值都只在本模块定义一次（见各字段的 default 与顶部常量）。下游拿到的对象字段天然
存在、类型明确，不需要 `config.get(key, 默认值)` 那种「每个读者各自兜一次底」——
兜底一多，默认值就没有主人，写错一个枚举值也会静默走另一条分支。真要缺，就在构造期报错。

`web_tools` 是唯一允许延迟校验的：没写这一段不算错（不启用联网工具的预设用不上它），
写了就必须完整 —— 所以它在这里保持「原始映射 + 访问时才解析」的形态，见 `Settings.web_tools()`。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"

# ---------- 默认值（全项目唯一来源）----------

# 端点未写 api_type 时的默认值
DEFAULT_API_TYPE = "chat_completions"

# 支持的 API 形态：chat_completions（Chat Completions）/ responses（Responses API）。
# 取值写错时在解析配置时直接报错 —— 否则会按「不等于 chat_completions 就走 responses」
# 悄悄换一套请求形状，排查起来极难。
API_TYPES = ("chat_completions", "responses")

# 思考强度五档；none = 禁用思考（请求带 "thinking": {"type": "disabled"}，见 llm.build_request）
EFFORT_LEVELS = ("none", "low", "medium", "high", "max")
DEFAULT_REASONING_EFFORT = "low"

# 选项请求默认开启
DEFAULT_OPTIONS_ENABLED = True

# 联网工具（web_search / web_fetch）的独立配置，打的是 DeepSeek 的 Search / Fetch 接口，
# 与模型端点无关：api_key 就是 DeepSeek API Key，从端点那边复制过来即可。
WEB_TOOL_API_BASE = "https://api.deepseek.com"
WEB_TOOL_TIMEOUT = 60
WEB_TOOL_COUNT = 10
WEB_TOOL_COUNT_MAX = 50

# 全服务器同时在跑的生成上限
DEFAULT_MAX_CONCURRENT_GENERATIONS = 4

# 登录态有效期（天）
DEFAULT_SESSION_TTL_DAYS = 30


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------- 端点 ----------

# provider 路由（OpenRouter 的字段，写错它自己会 400）里本项目校验的部分
_PROVIDER_SORTS = ("price", "throughput", "latency")
_PROVIDER_PERCENTILES = ("p50", "p75", "p90", "p99")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """一个模型端点。身份 = 它在 config.yaml 列表里的下标（重排会改变旧会话的指向）。"""

    display_name: str
    api_base: str
    model: str
    api_key: str
    api_type: str = DEFAULT_API_TYPE
    # 每会话固定一个 UUID 当 X-Opencode-Session 请求头发（opencode-go 的风控要求）
    x_opencode_session: bool = False
    # 把本会话 chat_id 当请求体 session_id 发出去（OpenRouter 的粘性路由 + 前缀缓存亲和）
    session_id: bool = False
    # 发给该端点前给思考强度改名（OpenRouter 只到 xhigh，没有 max）
    reasoning_effort_map: Mapping[str, str] | None = None
    # 原样塞进请求体的 provider 字段（OpenRouter 的路由偏好）
    provider: Mapping | None = None


def _check_effort_map(value, i: int) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"config.yaml 的 endpoints[{i}].reasoning_effort_map 不是映射: {value!r}")
    for k, v in value.items():
        if k not in EFFORT_LEVELS:
            raise ValueError(
                f"config.yaml 的 endpoints[{i}].reasoning_effort_map 键非法: {k!r}"
                f"（可选值: {', '.join(EFFORT_LEVELS)}）"
            )
        if not isinstance(v, str) or not v:
            raise ValueError(
                f"config.yaml 的 endpoints[{i}].reasoning_effort_map[{k!r}] 不是非空字符串: {v!r}"
            )


def _is_threshold(v) -> bool:
    """吞吐/延迟阈值：一个数字（p50），或 p50/p75/p90/p99 的子集。"""
    if _is_num(v):
        return True
    return (
        isinstance(v, Mapping) and bool(v)
        and all(k in _PROVIDER_PERCENTILES and _is_num(n) for k, n in v.items())
    )


def _check_provider(value, i: int) -> None:
    """只校验本项目文档化的三项，其余键透传 —— OpenRouter 对未知键会 400，报错由它给更准的信息。"""
    if value is None:
        return
    where = f"config.yaml 的 endpoints[{i}].provider"
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} 不是映射: {value!r}")
    sort = value.get("sort")
    if sort is not None:
        if isinstance(sort, str):
            ok = sort in _PROVIDER_SORTS
        else:
            ok = isinstance(sort, Mapping) and sort.get("by") in _PROVIDER_SORTS
        if not ok:
            raise ValueError(
                f"{where}.sort 非法: {sort!r}"
                f"（{'/'.join(_PROVIDER_SORTS)}，或 {{by: ..., partition: ...}}）"
            )
    for key in ("preferred_min_throughput", "preferred_max_latency"):
        if key in value and not _is_threshold(value[key]):
            raise ValueError(
                f"{where}.{key} 非法: {value[key]!r}（数字，或 {{p50/p75/p90/p99: 数字}}）"
            )


def parse_endpoint(raw, i: int) -> Endpoint:
    """解析并校验 endpoints[i]。缺必填项、api_type 非法、provider/effort_map 写错都直接报错。"""
    if not isinstance(raw, Mapping):
        raise ValueError(f"config.yaml 的 endpoints[{i}] 不是映射: {raw!r}")
    for k in ("api_base", "model", "api_key"):
        if not raw.get(k):
            raise ValueError(f"config.yaml 的 endpoints[{i}] 缺少必填项 {k}")
    api_type = raw.get("api_type", DEFAULT_API_TYPE)
    if api_type not in API_TYPES:
        raise ValueError(
            f"config.yaml 的 endpoints[{i}].api_type 非法: {api_type!r}"
            f"（可选值: {', '.join(API_TYPES)}）"
        )
    _check_effort_map(raw.get("reasoning_effort_map"), i)
    _check_provider(raw.get("provider"), i)
    return Endpoint(
        display_name=str(raw.get("display_name") or raw["model"]),
        api_base=str(raw["api_base"]).rstrip("/"),
        model=str(raw["model"]),
        api_key=str(raw["api_key"]),
        api_type=api_type,
        x_opencode_session=bool(raw.get("x_opencode_session", False)),
        session_id=bool(raw.get("session_id", False)),
        reasoning_effort_map=raw.get("reasoning_effort_map") or None,
        provider=raw.get("provider") or None,
    )


# ---------- 联网工具 ----------


@dataclass(frozen=True, slots=True)
class WebTools:
    api_base: str
    api_key: str
    timeout: float
    count: int


def parse_web_tools(raw) -> WebTools:
    """解析 web_tools 段；缺 api_key 直接报错（只有真的调用联网工具才会走到这里）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"config.yaml 的 web_tools 不是映射: {raw!r}")
    api_key = raw.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(
            "config.yaml 的 web_tools.api_key 未配置：web_search / web_fetch 需要 DeepSeek API Key"
            "（与 DeepSeek 端点用的同一个 key，复制过来即可）"
        )
    api_base = raw.get("api_base") or WEB_TOOL_API_BASE
    if not isinstance(api_base, str) or not api_base.startswith(("http://", "https://")):
        raise ValueError(f"config.yaml 的 web_tools.api_base 非法: {api_base!r}")
    timeout = raw.get("timeout", WEB_TOOL_TIMEOUT)
    if not _is_num(timeout) or not 1 <= timeout <= 600:
        raise ValueError(f"config.yaml 的 web_tools.timeout 非法: {timeout!r}（1~600 秒）")
    count = raw.get("count", WEB_TOOL_COUNT)
    if not _is_int(count) or not 1 <= count <= WEB_TOOL_COUNT_MAX:
        raise ValueError(
            f"config.yaml 的 web_tools.count 非法: {count!r}（1~{WEB_TOOL_COUNT_MAX} 条）"
        )
    return WebTools(
        api_base=api_base.rstrip("/"),
        api_key=api_key.strip(),
        timeout=float(timeout),
        count=count,
    )


# ---------- 鉴权 / 服务端 ----------


@dataclass(frozen=True, slots=True)
class AuthSettings:
    enabled: bool
    users: Mapping[str, str]
    ttl: float  # 登录态有效期（秒）
    cookie_secure: bool


def parse_auth(raw) -> AuthSettings:
    """解析 auth 段；账号/密码/有效期非法直接报错（不静默兜底）。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"config.yaml 的 auth 不是映射: {raw!r}")
    users = raw.get("users") or {}
    if not isinstance(users, Mapping):
        raise ValueError(f"config.yaml 的 auth.users 不是映射: {users!r}")
    for name, password in users.items():
        if not isinstance(name, str) or not name.strip() or "/" in name:
            raise ValueError(f"config.yaml 的 auth.users 用户名非法: {name!r}（不能为空、不能含 /）")
        if not isinstance(password, str) or not password:
            raise ValueError(f"config.yaml 的 auth.users.{name} 的密码必须是非空字符串")
    ttl = raw.get("session_ttl_days", DEFAULT_SESSION_TTL_DAYS)
    if not _is_num(ttl) or ttl <= 0:
        raise ValueError(f"config.yaml 的 auth.session_ttl_days 非法: {ttl!r}")
    return AuthSettings(
        enabled=bool(raw.get("enabled")),
        users=dict(users),
        ttl=float(ttl) * 86400,
        cookie_secure=bool(raw.get("cookie_secure", False)),
    )


@dataclass(frozen=True, slots=True)
class ServerSettings:
    max_concurrent_generations: int = DEFAULT_MAX_CONCURRENT_GENERATIONS


def parse_server(raw) -> ServerSettings:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"config.yaml 的 server 不是映射: {raw!r}")
    size = raw.get("max_concurrent_generations", DEFAULT_MAX_CONCURRENT_GENERATIONS)
    if not _is_int(size) or size < 1:
        raise ValueError(
            f"config.yaml 的 server.max_concurrent_generations 非法: {size!r}（应为 ≥1 的整数）"
        )
    return ServerSettings(max_concurrent_generations=size)


# ---------- 静态配置 ----------


@dataclass(frozen=True, slots=True)
class Settings:
    """config.yaml 的完整快照。构造后不可变，全进程共用（按文件 mtime 缓存）。"""

    endpoints: tuple[Endpoint, ...]
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    options_enabled: bool = DEFAULT_OPTIONS_ENABLED
    user_agent: str | None = None
    auth: AuthSettings = field(default_factory=lambda: AuthSettings(False, {}, DEFAULT_SESSION_TTL_DAYS * 86400, False))
    server: ServerSettings = field(default_factory=ServerSettings)
    # web_tools 段的原始映射：None = 没写这一段（不算错）。校验推迟到真正要用的地方
    web_tools_raw: Mapping | None = None

    # ---- 构造 ----

    @classmethod
    def from_mapping(cls, raw: Mapping) -> "Settings":
        """从已解析的 yaml 映射构造（`load()` 与测试都走这里）。"""
        if not isinstance(raw, Mapping):
            raise ValueError(f"config.yaml 顶层不是映射: {raw!r}")
        endpoints = tuple(
            parse_endpoint(item, i) for i, item in enumerate(raw.get("endpoints") or [])
        )
        if not endpoints:
            raise ValueError("config.yaml 没有配置任何端点（endpoints 为空）")

        temperature = raw.get("temperature")
        if temperature is not None and not _is_num(temperature):
            raise ValueError(f"config.yaml 的 temperature 非法: {temperature!r}")
        max_tokens = raw.get("max_tokens")
        if max_tokens is not None and (not _is_int(max_tokens) or max_tokens < 1):
            raise ValueError(f"config.yaml 的 max_tokens 非法: {max_tokens!r}")

        effort = raw.get("reasoning_effort")
        if effort is None:
            effort = DEFAULT_REASONING_EFFORT
        elif effort not in EFFORT_LEVELS:
            raise ValueError(
                f"config.yaml 的 reasoning_effort 非法: {effort!r}"
                f"（可选值: {', '.join(EFFORT_LEVELS)}）"
            )

        options_enabled = raw.get("options_enabled")
        if options_enabled is None:
            options_enabled = DEFAULT_OPTIONS_ENABLED
        elif not isinstance(options_enabled, bool):
            raise ValueError(
                f"config.yaml 的 options_enabled 非法: {options_enabled!r}（应为 true/false）"
            )

        user_agent = raw.get("user_agent")
        if user_agent is not None and not isinstance(user_agent, str):
            raise ValueError(f"config.yaml 的 user_agent 非法: {user_agent!r}")

        web_tools_raw = raw.get("web_tools")
        return cls(
            endpoints=endpoints,
            temperature=float(temperature) if temperature is not None else None,
            max_tokens=max_tokens,
            reasoning_effort=effort,
            options_enabled=options_enabled,
            user_agent=user_agent,
            auth=parse_auth(raw.get("auth")),
            server=parse_server(raw.get("server")),
            web_tools_raw=web_tools_raw,
        )

    @classmethod
    def load(cls) -> "Settings":
        """读 config.yaml（按 mtime 缓存）。改文件保存即生效，不需要重启。

        解析失败直接抛 ValueError —— 调用方（服务端/启动脚本）据此报错，不会带病运行。
        """
        try:
            st = CONFIG_FILE.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        cached = _cache.get("settings")
        if cached is not None and cached[0] == sig:
            return cached[1]
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        settings = cls.from_mapping(raw)
        _cache["settings"] = (sig, settings)
        return settings

    # ---- 派生 ----

    @property
    def web_tools_declared(self) -> bool:
        """config.yaml 里写了 web_tools 段（不管写没写全），启动时据此决定要不要校验。"""
        return self.web_tools_raw is not None

    def web_tools(self) -> WebTools:
        """联网工具配置。没写这一段或缺 api_key 时抛 ValueError —— 只在真调用工具时发生。"""
        return parse_web_tools(self.web_tools_raw)


_cache: dict = {}


@dataclass(frozen=True, slots=True)
class TurnConfig:
    """一次请求实际生效的配置：Settings + 会话状态 + 预设派生，构造完只读。

    由 `core.build_turn()` 构造，`llm` 的每个函数都只认它 —— 所以 llm 里不存在
    「这个键可能没有」的分支，`None` 一律有明确语义（如温度/输出上限 None = 不发送）。
    """

    endpoint: Endpoint
    temperature: float | None
    max_tokens: int | None
    reasoning_effort: str
    chat_id: str
    user_agent: str | None
    ptc: bool
    tools: tuple[str, ...]
    bindings: tuple[str, ...]
    options_enabled: bool = DEFAULT_OPTIONS_ENABLED
