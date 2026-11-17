# rchat（AIRP 后端）

极简版 SillyTavern：一条命令起单进程后端 + 单页前端，和 AI 跑长线角色扮演。
两个差异化点：`world_run` 持久 Python 沙盒（判定、记账、状态），以及**对前缀缓存友好的上下文组装**。
不兼容 SillyTavern 的预设/角色卡格式，用的是本仓库自己的简化格式。

本文件写给改代码的人（AI）：只记读代码读不出来的取向、约束与坑。面向使用者的用法与配置说明在 `README.md`，别把那些内容搬回来。

## 动手前先接受的取向

1. **前缀缓存 > 省 token。** 上下文开头到上一次输出的前缀必须逐字稳定。任何在头部插内容、改动 model/tools/thinking、给请求加 `response_format` 的改动，先问一句「会不会破坏前缀」。为了缩短输入而裁剪/改写上下文，通常是亏的。
2. **单进程、无数据库** 状态只有在磁盘（`sessions/`、`auth_sessions.json`）和进程内表（`_active` / `_runtimes` / `_locks` / `_session_errors`）两处。
3. **前端零构建。** `app/static/index.html` 单文件（内联 CSS/JS）。

## 开工与收工

- 运行时要求 Python ≥ 3.14（`pyproject.toml`）；`uv run` 会自动取合适的解释器。
- 人工联调固定用 `uv run -m main --port 25531 --no-browser --no-keep-awake`，不要占用户正在用的 25530。
- 配置格式有变动时同步 `config.yaml.example`（`config.yaml` 是本机真实配置，不进版本库）。
- `preset/` 与 `games/` 只有白名单文件进版本库（见 `.gitignore`），个人预设与角色卡留在本地。

## 代码地图

| 位置 | 职责 |
|---|---|
| `main.py` | 参数、启动期 config 校验、uvicorn、keep-awake |
| `app/config.py` | `Settings`（config.yaml 快照，按 mtime 缓存）与 `TurnConfig`（一次请求实际生效的值）；**默认值只在这里定义** |
| `app/core.py` | 预设解析与宏渲染、工具 schema 与白名单、`build_input` 上下文拼装、会话与落盘（`create_session` / `session_key` / `_atomic_write_text`）、压缩切点 `compact_cut` |
| `app/llm.py` | 正文工具循环与选项请求，两种 `api_type` 的实现 |
| `app/server.py` | 端点、SSE 转发、会话锁与并发槽位、鉴权中间件 |
| `app/tools.py` | 工具分发；文件工具、bash、联网工具 |
| `app/world.py` | world_run 的持久 exec 命名空间、快照（含压缩后的平移）、PTC 绑定 |
| `app/auth.py` | 账号校验、登录态、登录限速 |
| `app/static/index.html` | 全部前端 |
| `preset/` `games/` | 预设与角色卡；`GM-safe.md` / `GM-ptc.md` 是可提交示例 |
| `deploy/` | systemd / Caddy / nginx / TLS，运维看 `deploy/README.md` |

## 硬约束

### 配置
- 默认值只在 `app/config.py`；下游只做属性访问，**不写 `config.get(key, 默认)`**，避免多重fallback掩盖错误。加新键的流程：常量与校验进 `config.py` → 由 `Settings` 补全 → 需要按会话覆盖的再加进 `state.json` 与 `build_turn`。
- `core.build_turn(state)` 是 `TurnConfig` 的唯一构造点。`server` 的预览 / 正文 / 重掷选项三个入口共用同一个 `TurnConfig`，所以上下文形状与请求形状不可能各说各话。新增请求期参数走这里，别在半路重新读 config。

### 会话与落盘
- 会话键 = `core.session_key(user, name)`，即 `"<user>/<name>"`（本地模式 `user=None` 时是裸名），落盘 `sessions/<user>/<name>/`。**API 路径只暴露裸名**，用户名只从 cookie 取——这也顺带避开 `%2F` 被 Starlette 解码成 `/`、`{name}` 匹配不上的问题。
- 会话名归一化（strip、`/`→`-`、空名自动命名）只在 `core.create_session` 一处。其它端点只定位已存在的会话，不自己造名字。
- 一切落盘走 `core._atomic_write_text`（同目录临时名 + `os.replace` + 每路径进程内锁）。不用它的后果是实测过的：同步端点跑在线程池里，两人同时写同一文件，每 1000 次约 39 次失败（Linux `FileNotFoundError`、Windows `WinError 32`）。
- 用户输入在 `_prepare` 时就落盘，助手块由 `_persist` 补。不要合并成「一轮结束才写」——进程被 kill 会丢用户刚打的那句话。
- 末尾是 user 块 = 可恢复状态（上一轮没写出来），前端据此把发送按钮变成「继续」并走 `/regenerate`。

### 生成与并发
- 一轮生成跑在会话级后台任务（`_active[会话键]`）里，HTTP 与生成解耦：断开连接只中断转发，生成照旧跑完落盘；重连靠 `/stream` 重放事件（幂等，可多客户端同时接）。别把生成塞进请求生命周期。
- 生成与改会话（回滚 / 编辑 AI 块 / 换预设 / 改参数 / fork / 删除）互斥：这些端点必须是 `async def`，和 `_generate` 共用 `server._lock_for(键)`。`_generate` 在锁里完成「查 `_active` + `_prepare` + 注册」，所以没有「检查过了但还没注册」的抢跑窗口。**锁内不许 `await`**（会让出控制权），流式转发不持锁。
- 全局并发槽位 `_generation_sem` 控制同时打上游的生成数，多人同时发是排队。
- 失败也要落盘半截结果（正文或占位、已执行的 tool_calls、思考链），并让 world 状态与历史对齐。

### 工具与世界状态
- 工具失败一律**以错误文本返回给模型**，不抛异常中断本轮（参数不合法、工具未启用、配置缺失都如此）。
- `world_run` 无超时保护：必须经 `asyncio.to_thread` 执行；取消时用 `asyncio.shield` + `wait_for(WORLD_RUN_CANCEL_GRACE)` 等它跑完再抛。线程杀不掉，抢先取消会让 `abort_turn` / `commit_turn` 和还在改 `state` 的线程撞车。
- PTC 绑定函数是**同步**的（在模型代码里直接调用）。绑定名是保留名，模型的顶层 def / 全大写常量不能覆盖；切预设时由 `_reconcile_bindings` 原地替换并保留 state 与函数定义。
- 取某长度的世界快照必须用 `world._snapshot_at` 回退到「不晚于它的最近一条」。直接 `snapshots.get(str(n))` 会让开局前 / 未碰过 world 的轮次静默拿到最新状态，回滚失效。
- 新增一个工具要同步改四处：`core` 的 schema 与 `TOOL_NAMES` 白名单、`tools.execute_tool` 分发、`world._bindings_for`（PTC）、前端的工具块展示。工具的用法说明写在 schema 的 `description` 里（预设不再有 `{{respond_tool}}` 这种渲染入口）。

### 上下文压缩（/compact）
- **压缩后 history 就是上下文**，不是「history 原样留着 + 另存一份 summary」。摘要块是一条普通的 `{role: "assistant", content, compacted: true}`：`build_input` / 预览页 / 回滚 / 分支全都不需要知道压缩的存在。想加新状态前先问「能不能只靠 history 表达」。
- **切点必须落在 user 块上**（`core.compact_cut`），且只在 `history[-1]` 是 assistant 时才允许压缩。末尾是 user 块 = 上一轮还没写出来，压下去会把玩家刚打的字一起吞掉。
- **`world` 快照的 key 是历史长度，压缩后必须平移**（`world.remap_after_compact`，`new = old - cut + 1`，并补一条 key=1 的边界快照）。不做的后果不是「回滚不准」而是「下一轮 `_prepare` 的 `world.sync(name, len(history))` 把世界状态静默回滚到开局，再下一轮 `commit_turn` 的 prune 把之后的快照全删掉」。
- 摘要请求与选项请求同构：同一份 `TurnConfig`、同一个输入前缀的严格续写、非流式、只单独改输出上限。压缩期间用 `_active[mode="compact"]` 占位，锁内只做「查 + 注册」，LLM 调用在锁外，写盘前重新取锁并校验长度。
- 落盘顺序是「先摘要、再 fork、后改写」：摘要失败什么都不留；fork 在改写前，所以改写途中崩了还有完整副本。`core.fork_session` 支持任意 index，但 `/fork` 端点只放行用户块——位置约束属于面向用户的入口。
- 摘要块不能重掷：`_prepare` 的 regenerate 分支看到 `compacted` 就报错。`keep_turns=0` 时历史只剩摘要块，pop 掉它会 `world.sync(name, 0)` 清空世界状态。
- **摘要块成为最后一轮时（`keep_turns=0`，即 `cut == len(history)`），选项跟着搬到摘要块上**：压缩前末块已有 `options` 就直接沿用（同一段剧情的延续，重问一遍只换个措辞、白花一次请求），没有且开关打开才补问一次。补问失败记进 `options_error`，压缩本身照常完成。保留原文时末块还是原来那一轮，它的 `options` 原样留着，别拿摘要块去覆盖。`_prepare_options` 不认 `compacted`，所以摘要块天然支持「重新生成选项」，但前端只在它是末块时才给按钮。

### 前端
- 同会话刷新只走 `reconcileSession()` 按块 key 增量对账；整页 `renderAll()` 只在**切换会话**时用（且显式滚到底）。一轮结束优先用 `saved` 事件就地定稿，别重建 DOM——否则滚动位置、展开的 details、选区、编辑中的 textarea 都会丢。
- **唯一的例外是压缩**：摘要块替换整段历史，所有块的 key 都变了，`reconcileSession` 会把整棵树对错位，必须走 `selectSession` 整页重建。
- 每条流有 `sid` / `seq`，重连、对账、滚动前先校验，旧流不许写进新会话。
- 斜杠指令（`/compact`）在前端解析，不进 `/chat`——后端不该在正文路径里认命令。以后加「压缩」按钮复用同一个 `runCompact`。

## 踩过的坑（改到相关代码前先看）

| 现象 | 原因与正确做法 |
|---|---|
| 一轮里多个 `function_call` 后 400（responses / deepseek-flash） | 每个 call 前要各自有一份 `reasoning_text`；用单空格占位，空字符串非法。别把一份长思考重复回放。 |
| 模型把自己的正文读了好几遍 | `chat_completions` 每轮回放只带**本轮新增**正文；`content` 是跨轮累积量。 |
| 同一段思考回放多遍 | `responses` 侧同一段思考只放在它带来的第一个调用前（`prev_reasoning` 去重）。 |
| 抓不到思考链 | 字段依次 `reasoning_content`（deepseek 等）→ `reasoning`（OpenRouter 归一化）→ `reasoning_details`（结构化块，跳过 encrypted），按序取第一个有文本的；OpenRouter 会同时发前两个，所以不能合并。回传统一用 `reasoning_content`。 |
| `reasoning_effort: none` 直接当 kwarg 传 | TypeError，一行都发不出去。只能经 `extra_body={"thinking": {"type": "disabled"}}` 透传。 |
| 选项请求缓存命中率从 98% 掉到个位数 | 必须与正文共用同一份 `TurnConfig`（同 model / tools / thinking），且不加 `response_format`；别单独压低思考。指令放请求末尾，替换它不影响前缀。 |
| 页面上冒出假选项按钮 | 解析器只认行首 `-` / `*` / `•` / `·` 或编号，前言后记代码围栏一律丢弃，**不做宽松兜底**。失败会重发一次，仍失败则记 `options_error` + `options_raw`，正文照常落盘。 |
| 新建会话后前端找不到它 | 裸会话名里带 `/` 会被当成作用域前缀，会话落进 `sessions/<前半截>/`。归一化只在 `create_session`。 |
| 旧会话指向了另一个模型 | 端点身份 = `endpoints` **列表下标**，重排列表就会改掉所有旧会话的指向。 |
| `_runtimes` / `_locks` 只增不减 | 已知边界（删会话才释放），靠 systemd `MemoryMax` 兜底。别当 bug 反复「修」；真要淘汰只能在轮次边界做。 |
| 压缩后世界状态跳回开局 | 快照 key 是历史长度，压缩把长度改短却没平移。`world.remap_after_compact` 是唯一入口，别手写 `snapshots.json`。 |
| 压缩后前端整片块错位 | 索引全变了还走 `reconcileSession`。压缩完成必须 `selectSession` 整页重建。 |
| 点「重新输出」把前情删光了 | `keep_turns=0` 时历史只剩摘要块，regenerate 会 pop 掉它并从空历史重开（还会 `sync(0)` 清空世界状态）。已由 `_prepare` 的 `compacted` 检查挡住，前端也不给按钮。 |
| 压缩时模型把世界状态复述进摘要 | 摘要块里自动附加的 `【本轮state快照】` / `【world_run 内已定义的函数与常量】` 段落会被下一次压缩当成输入再摘一遍（不能从输入里剔除——那会破坏前缀缓存）。靠 `COMPACT_INSTRUCTION` 里那条「state 与函数由系统自动附上、不用写进摘要」约束，新自动段落要同步加进去。 |

## 预设 / 角色卡 / 选项（作者契约）

- 代码只处理三类标记：`<preset_section role="system|user|assistant">`、`<preset_user_input>`、`<preset_options>`。其它 XML 标签都是预设的**内部文本**，原样发给模型。不要给新标记加语义，除非确实要改协议。
- `{{...}}` 按 Python 表达式 eval（`core.render_template`）。固定宏 `game_setting` / `game_beginning` / `user_setting`，环境里另有 `random` / `time` / `math` / `datetime`（`preset_user_input` 块额外有 `user_input`）。严格模式：未知变量或求值出错直接 ValueError，不静默兜底。预设是本机可信文件，不是安全边界。
- `<preset_options>` 是选项请求的**完整指令**，格式要求必须由预设作者自己写清：一行一条、行首 `- `（半角短横线）、无合适选项只输出单独一行「无」、不要前言 / 后记 / 代码围栏 / JSON。代码不会在块外再补任何格式要求。块要放在 `preset_section` 外，多个块取第一个；未定义时回退 `core.OPTIONS_INSTRUCTION`。
- 预设 frontmatter 的 `tools` 是白名单：顺序即 schema 顺序，未写默认 `world_run` + `read_file`，未知名字（含历史的 `respond`）直接 ValueError。`ptc: true` 时 schema 只剩 `world_run`，其余工具降级为程序内同步绑定。

## 安全与部署

- 工具能做的事 = 服务进程能做的事（`world_run` 执行任意 Python、`bash` 执行任意 shell、文件工具收绝对路径）。**账号只发给可信的人**。
- `config.yaml` 含 API key 与明文密码，`chmod 600`。服务器上 `--host 127.0.0.1` 只让反代访问（uvicorn 已开 `proxy_headers`）。
- 部署件与运维步骤在 `deploy/`。
