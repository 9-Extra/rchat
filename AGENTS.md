# 用于 AIRP 的后端

一个究极简化版的 SillyTavern：启动后端并打开网页，和 AI 对话。

## 用法

```
uv run -m main
```

默认 25530 端口、双栈、自动开浏览器。`--host`（默认 `::`）指定监听地址，服务器上建议 `--host 127.0.0.1` 只让反代访问（socket 家族按 `getaddrinfo` 解析结果选，给 IPv4 字面量时用 AF_INET）。`--keep-awake`（默认开）在运行期间调 Windows 的 `SetThreadExecutionState` 阻止系统空闲睡眠/休眠，`--no-keep-awake` 关闭；非 Windows 平台自动忽略。uvicorn 开了 `proxy_headers`，反代日志/限速拿到的是真实客户端地址。

启动时除端点外还校验 `auth` 段：开了鉴权却一个账号都没有直接报错退出。

**改完代码先跑仓库里的测试**：`uv run python -m unittest discover -s tests -t .`（`tests/` 里 26 个用例，纯 stdlib unittest、进程内 ASGI、假模型，离线可跑，也不会碰真 `sessions/`）。里面既有行为用例，也有几个「守住认知」的用例（旧临时名并发写必失败、裸 `to_thread` 取消后线程还在跑）——它们复现不了会 skip，不算假红。人工联调仍用下面这条起真服务：

**测试一律用 `uv run -m main --port 25531 --no-browser --no-keep-awake`**，避免和用户的实例冲突。

## 多用户与鉴权

默认（`auth.enabled: false`）**不做任何鉴权**，行为与改造前完全一致：会话键就是裸会话名，落 `sessions/<会话名>/`。放到服务器上给几个人用时打开：

```yaml
auth:
  enabled: true
  users: {alice: "密码", bob: "密码"}   # 明文手写；改完保存即生效，不用重启
  session_ttl_days: 30
  cookie_secure: false                  # https 反代时置 true
```

- `app/auth.py`：账号来自 config.yaml，密码用 `hmac.compare_digest` 比；登录成功发 `secrets.token_urlsafe(32)`，存「内存会话表 + `auth_sessions.json`」（不入库，重启免登录）。登录失败按「用户名+来源 IP」限速（5 次 / 5 分钟）。账号从 config.yaml 删掉，已发的 cookie 立刻失效（`session_user` 每次都比对账号表）。
- `server.auth_gate` 中间件**默认拒绝**：启用鉴权后除 `/api/login`、`/api/me` 外的所有 `/api/*` 都要有效 cookie，否则 401；`current_user` 依赖把它取出来给各端点。前端 `api()` 收到 401 就切登录页（`showLogin`），启动时先问 `/api/me`（返回 `{user, auth_enabled}`）决定要不要显示登录页。
- **作用域**：内部会话键是 `"<用户名>/<会话名>"`（`core.session_key`），落盘 `sessions/<用户名>/<会话名>/`。API 路径始终只暴露裸会话名（用户从 cookie 取、服务端拼前缀），前端 URL 不用变——也避开 `%2F` 被 Starlette 解码成 `/` 导致 `{name}` 匹配不上。进程内的 `server._active`（生成状态）与 `world._runtimes`（执行环境）用的就是这个键，所以两个用户各建一个同名会话互不影响。
- `core.list_sessions(user)` / `_latest_session_state(user)` 都按用户过滤：侧栏只列自己的，新会话继承**自己**上一个会话的参数（不是全局最新那个）。
- 本地模式下 `user` 恒为 None，`session_key(None, name)` 就是裸名——旧目录照旧能读。启用鉴权后，想把旧会话给某个人，手动挪进 `sessions/<用户名>/` 下。
- ⚠️ 工具能做的事 = 服务进程能做的事（`world_run` 执行任意 Python、`bash` 执行任意 shell、文件工具收绝对路径），所以账号只发给可信的人；`config.yaml` 里有 API key 和明文密码，`chmod 600`。部署件与运维说明在 `deploy/`。

## 配置（config.yaml）

- `endpoints`：模型端点列表，前端按会话切换；启动时校验，为空直接报错退出。每项必填 `api_base` / `model` / `api_key`，可选 `display_name`（缺省用 model）、`api_type`（`"responses"` 默认 / `"chat_completions"`）、`x_opencode_session`、`reasoning_effort_map`（发给该端点前给思考强度改名，如 OpenRouter 写 `{max: xhigh}`，它的枚举只到 xhigh）、`provider`（原样塞进请求体的 `provider` 字段，OpenRouter 的路由偏好，如 `{preferred_min_throughput: 30}`＝默认负载均衡不变、只把低于 30 tok/s 的上游降到末尾；`sort` 与两个 `preferred_*` 阈值在启动时校验，其余键透传）、`session_id`（true 时把本会话的 `chat_id` 当**请求体** `session_id` 发出去——OpenRouter 靠它做粘性路由与上游缓存亲和，正文与选项请求才更容易落到同一个上游、命中同一份前缀缓存）。**端点身份 = 列表下标**，重排列表会改变旧会话的指向。
- 全局键：`temperature` / `max_tokens` / `reasoning_effort` / `options_enabled` / `user_agent`（覆盖 SDK 默认 UA）。
- `server.max_concurrent_generations`（缺省 4）：全服务器同时在跑的生成上限，多出来的排队等槽位（`server._generation_sem`，`_run` / `_run_options` 各占一个）。第一次用到时建成，改了要重启才生效。
- `auth.*`：见「多用户与鉴权」。
- 可按会话覆盖（存 state.json）：`temperature` / `max_tokens` / `reasoning_effort` / `endpoint` / `options_enabled`，前端可改（`POST /api/sessions/{name}/params`、`/endpoint`、`/reasoning_effort`、`/options`）。新会话继承 created_at 最大的那个会话的这几项（**同一用户内**），无会话时用端点 0 + config 默认；会话值非法/缺失回退 config 默认，config 值非法直接报错；fork 自动继承。
- `reasoning_effort` 五档 `none`/`low`/`medium`/`high`/`max`，不设置按 `low`；`none` = 禁用思考（请求带 `"thinking": {"type": "disabled"}`，这个参数名 OpenAI SDK 不认，只能经 `extra_body` 透传：直接当 kwarg 传会 TypeError，一行都发不出去）。
- `options_enabled`（缺省 true）：正文之后要不要再发一次选项请求，见「选项生成」。
- `x_opencode_session`：`true` 时每个会话的请求都带固定的 `X-Opencode-Session: <UUID v4>` 头（opencode-go 风控 / GPU KV 缓存亲和）。UUID 存在 state.json 的 `chat_id`，创建时生成，fork 换新值。

生成/预览前 server 用 `core.effective_config(state, config)` 合并出扁平 config 交给 llm（llm 不感知多端点）。`api_type` 只影响上下文拼装、工具 schema 与流式实现，落盘的 `history.jsonl` 格式不变。

## 预设与角色卡

预设 = 通用的 AI 主持指南（文风、禁词、防止全知），决定如何基于角色卡与历史拼出最开头的上下文，含系统提示词；角色卡 = 几个大字符串（世界设定、用户人设、开局），由预设通过宏决定插到哪。

预设格式（示例 `./preset/GM.md`）：`<preset_section role="system|user|assistant">` 块组织内容。只有 `<preset_section>` 是代码处理的格式标记，其它 xml 标签是预设的**内部文本**，原样发给模型。宏：`{{game_setting}}`（世界设定）、`{{game_beginning}}`（选中的开局）、`{{user_setting}}`（用户人设）、`{{respond_tool}}`（工具相关提示词）。

宏实现：所有 `{{...}}` 按 Python 表达式 eval（`core.render_template`），四个固定宏就是求值环境里的同名变量；环境里还预置 random / time / math / datetime。严格模式：未知变量或求值出错直接抛 ValueError，不静默兜底。

`<preset_user_input>` 块（前后空白 trim）是用户输入的后处理模板，渲染时额外提供 `user_input`。它只作用于当前这一轮发给模型的内容，落盘的 history 仍是原文（回滚拿到原文，重生成会重新渲染）；没有这个块时用户输入原样透传。

预设 frontmatter 的 `tools` 是工具白名单：列什么就只有什么（顺序即 schema 顺序），未定义时用 `world_run` + `read_file`。可选 `world_run` / `read_file` / `write_file` / `edit_file` / `bash`；未知名字（含历史上的 `respond`）在生成/预览时抛 ValueError。`core.respond_tool_text` 渲染的任务提示词按白名单裁剪：没启用的工具不出现，启用 write_file/edit_file/bash 会附上用法段。PTC 模式下 schema 只有 world_run，这个列表决定它程序内的绑定函数。

角色卡格式：`<game_setting>` / `<user_setting>` / `<game_beginning>` 等 XML 块，宏名同名；示例见 `./games/龙娘x猫娘.md`。

## 工具

schema 在 `app/core.py`，启用与否由预设 `tools` 白名单决定。正文始终是模型的普通文本输出、直接流式给用户，不经过任何工具。

- **world_run**：持久 Python 环境（`app/world.py`）。每会话一个 exec 命名空间，三者持久化到 `<会话目录>/world/`：state（state.json）、顶层 def 函数、全大写全局变量（常量，规则 `^[A-Z][A-Z0-9_]*$`）；函数与常量存源码进 lib.py，重放时按 `tree.body` 顺序 exec。**无超时保护**，所以 `execute_tool` 把它丢进 `asyncio.to_thread` 执行：死循环只占住一个工作线程，不会把事件循环和所有人的 SSE 一起冻住（代价是它会一直占着那个线程，`MemoryMax`/`TasksMax` 兜底）。线程杀不掉，所以**打断时会先等它跑完再抛取消**（`WORLD_RUN_CANCEL_GRACE`＝5 秒；代码用 `asyncio.shield` + `wait_for`）：否则取消处理器里的 `world.abort_turn` / `commit_turn` 会和还在改 `state` 的线程同时动手（`json.dumps` 撞出 `dictionary changed size during iteration`，或让被取消那轮的函数定义留进 lib）。超过宽限（多半是死循环）才放行并记 WARNING。
- **read_file**：只读分页，默认/上限 2000 行、单次约 50KB，超出用 offset/limit 翻页。
- **write_file**：写 UTF-8（`overwrite` 整篇覆盖/新建、`append` 追加；父目录自动创建，换行统一 LF）。
- **edit_file**：精确替换一段文字（`old_string` 逐字匹配、默认要求唯一，`replace_all=true` 全部替换；原文件的 LF/CRLF 风格不变）。
- **bash**：Git Bash 里跑命令，返回 `<cwd>/<exit_code>/<output>`。bash.exe 由 `tools._shell()` 探测并缓存（PATH → `C:\Program Files\Git\bin` 等），找不到以错误文本返回；默认超时 60s / 上限 600s，超时终止（Windows 上可能留下孙进程），输出超 20000 字符只留开头。

文件类工具的相对路径基准与 bash 的工作目录都是**当前会话角色卡所在目录**（`games/<世界包>/`），也接受绝对路径；取不到卡目录时报错。`tools.execute_tool` 先按会话预设解析白名单，未启用的工具名返回带「本会话启用的直接工具」清单的错误文本。直接调 bash 走 `asyncio.to_thread`（不卡事件循环），world_run 内的 bash 绑定是同步执行（长命令卡住服务端）。工具的文件/shell 副作用不参与 world_run 的出错回滚（回滚只覆盖 state 与函数/常量）。

**world_run 快照**：按历史长度存进 `snapshots.json`，只在改动过 world 的轮次结束时记一条。取某长度的状态要用 `world._snapshot_at` 回退到不晚于它的最近快照（两轮之间状态没变），早于所有快照（世界状态还没建立）视为空状态——直接 `snapshots.get(str(len))` 会在开局前/未碰 world 的长度上静默保持最新状态，回滚失效。

两种 `api_type` 都支持多轮工具调用：`responses` 用 `function_call`/`function_call_output`，`chat_completions` 用 `tool_calls`/`tool` 消息。落盘历史格式不变。

## 一轮回复

分两段，都是普通请求（`app/llm.py`）：

1. **正文**（`stream_body`）：工具循环。模型可多次调用白名单内的工具（PTC 下是 world_run 内的绑定），结果追加进输入继续请求；**某一轮不再调用任何工具即视为说完**，回合结束（yield `done`，只含 content 与 reasoning）。全程没有内容、或超过 `MAX_ROUNDS`(25) 轮，都报错。
2. **选项**（`generate_options`）：正文完成后，若 `options_enabled` 为真，再用同一份 config 发一次非流式请求，只要选项列表。

history 条目：`{role, content, options, reasoning, tool_calls?}`，另有 `error` / `options_error` / `options_raw` 三个失败标记（回放时都不进模型上下文）。

### 上下文回放（core.build_input）

- `responses`：每个 tool_call -> reasoning + function_call/output 对，正文 -> 一条普通 assistant message；user 块与本次输入都是普通 user message，**请求以 user message 结尾**。
- `chat_completions`：每个 tool_call 一条 assistant(tool_calls) + 一条 tool 消息，正文一条 assistant(content)，user 块与 draft 对应 user 消息。
- 都原样回放 `entry["tool_calls"]`；选项不参与回放。
- `chat_completions` 每轮的 assistant 消息只回传**本轮新增**的正文（`content` 是跨轮累积量，整段回传会让模型把自己的正文反复读一遍）。
- `responses` 上同一段思考只在它带来的第一个调用前放一次（`core._build_input_responses` 的 `prev_reasoning` 去重），否则一轮里多次调用会把整轮思考重复回放好几遍。
- `chat_completions` 的思考链字段名各家不一，`llm._delta_reasoning` 依次兜底 `reasoning_content`（deepseek 等）→ `reasoning`（OpenRouter 归一化字段）→ `reasoning_details`（OpenRouter 的结构化块，取 `reasoning.text` / `reasoning.summary`、跳过 encrypted）；OpenRouter 同时发前两个且逐字相同，所以按顺序只认第一个。判断只看字段有没有文本，不按端点配置。回传照旧用 assistant 的 `reasoning_content`（OpenRouter 认它是 `reasoning` 的别名；实测不传、只传 `reasoning`、原样回传 `reasoning_details` 都不报错）。

### responses 的思考链约束

deepseek-flash 的 `/responses` 要求**每个 `function_call` 前都有各自的一份 `reasoning_text`，否则 400**——一轮里模型发了两个调用而你只回放一份真文本会 400，连它自己产出的 trace 原样回放也会 400。所以 `llm._stream_body_responses` 中场追加时按调用逐个护航：轮首那份真实思考算第一份，其后每个调用补一个单空格占位（实测合法，空字符串不合法），不重复长文本。同一处还兜了 `output_item.added` 缺失的实现（只发 `reasoning_text.delta` / `output_text.delta`）。

### 选项生成

- **输入**：`core.build_options_input(state, history, content, tool_calls, reasoning)` = 正文请求的输入 + 本轮助手回复 + 一条 user 指令（`core.OPTIONS_INSTRUCTION`：只要 2-4 条、只输出以「- 」开头的选项行、不要续写正文也不要调用工具）。
- **输出格式**：一行一条、每行以「- 」开头（模型因此不必转义选项里的引号；此前用 JSON 时「引号未转义」和「括号配错」两类语法错都实际发生过）。没有合适选项时输出单独一行「无」。
- **缓存约束（实测 deepseek 官方端点）**：必须与正文请求共用同一份 config——同 model、同 tools、同 thinking / reasoning_effort，且**不加 `response_format`**。任一项不一致，前缀缓存命中率从 98% 掉到 6%；作为「正文请求的严格续写」它自身只在末尾多几十个 token（命中约 96%）。所以别给选项请求单独压低思考，也别用 `response_format`（该端点不支持 json_schema，`json_object` 同样往 prompt 头部插说明、打断前缀）。
- **解析与兜底**（`core.parse_options`）：按行扫，只认 `_OPTION_LINE_RE` 匹配的选项行（行首 `-` / `*` / `•` / `·`，或 `1.` `2、` `3)` 这类编号）——前言、后记、代码围栏、空行一律丢弃，**不做宽松兜底**（把杂行当选项会在页面上多出假按钮）；一行都没有时抛 ValueError；单独一行「无」表示这轮没有合适选项、返回空列表。失败追加一条提醒重发一次（`_OPTIONS_RETRY_HINT`，追加在末尾不影响前缀），仍失败则选项为空、错误写进 entry 的 `options_error`（前端给提示 +「重新生成选项」；与表示整轮失败的 `error` 区分）。正文不受影响。
- **失败现场**：两次都失败时，模型每次输出的原文（第 1/2 次分别标注，空输出记成「（空输出）」）经 `generate_options` 的第三个返回值落盘为 entry 的 `options_raw`，前端在错误行下折叠显示；整段原文同时进 WARNING 日志。模型调工具或被输出上限压空时 `_request_options_once` 不返回文本而直接给出说明（含 `finish_reason` / `status`+`reason`、工具调用的名字与参数），这条说明就是错误文本本身。
- 选项请求的 usage 以 INFO 一行打进日志（含缓存命中数与其中的思考 token 数，`llm._usage_note`）。
- `POST /api/sessions/{name}/regenerate_options`（`_prepare_options` / `_run_options`）只重掷最后一轮的选项：不重新生成正文、不新增块、不碰 world。

## PTC 模式

预设 frontmatter 加 `ptc: true`（如 `preset/GM-ptc.md`）即开启，形态对齐 DeepSeek Harness 的 PTC 训练分布：schema 只含 world_run 一个工具（调用其它工具名返回错误），其余工具降级为 world_run 命名空间内的 Python 绑定函数（`world._bindings_for` 按白名单挂载 read_file/write_file/edit_file/bash 各一个 `_*_binding`；签名写在 `{{respond_tool}}` 提示词和 world_run 的 schema 描述两处，都由 core 按白名单拼）。叙事仍是普通文本，工具只用于判定与记账，回合结束同样是「某一轮不再调用任何工具」，选项与普通模式一样由正文后的独立请求生成。保留名是 state/print 加上当前启用的绑定名，不允许模型的顶层 def/常量覆盖；会话中途切换预设由 `world._reconcile_bindings` 原地换掉绑定（保留当前 state 与函数定义）。

## 流式、落盘与失败

生成与 HTTP 连接解耦：一轮生成跑在会话级后台任务里（`_run`；`_active[会话键]` 存 task / events / wake / finished / mode / user_input），POST `start|chat|regenerate|regenerate_options` 与 `GET /api/sessions/{name}/stream` 都只是把 `events` 里的 SSE 文本转发出去（`_tail`，15s 一次 `: ping` 心跳）。`_run` / `_run_options` 开头先抢一个全局槽位（`server._generation_sem`，上限见 `server.max_concurrent_generations`），多人同时发是排队而不是一起打上游。

- **用户输入在开局时就落盘**（`_prepare` 里追加 user 块并 `save_history`，`_persist` 只补助手块）：进程被 kill / 部署重启 / OOM 都不会丢掉用户刚打的那句。请求拼装用的是「追加前的那段历史 + draft」，所以预设的 `<preset_user_input>` 模板仍只作用于这一轮、落盘保留原文。顺带的好处：选项请求的输入（`build_options_input(state, history, ...)`）现在严格等于「正文请求的输入 + 本轮助手回复」，前缀缓存不会再在用户那一句上断开。
- 末尾是 user 块（上一轮没写出来）= 可恢复状态：前端发送按钮变成「继续」（走 `/regenerate`，把那句用户输入取出来重发），`updateComposer` / `regenerate` 都按这个判定走了分支。
- 客户端断开（刷新、掉线、锁屏）只中断这次转发，生成照旧跑完并落盘。页面加载时看 `GET /api/sessions/{name}` 的 `generating`（`{mode, user_input}`，没在跑则 null，mode 可能是 `options`；被替换掉的旧 AI 块靠它还原），前端据此接上 `/stream` 重放整轮（幂等，可多客户端同时接），断了退避重连。`attachTurn` 只在「末尾不是 user 块」时才现场补画用户块，否则会画两遍。
- `_gen_of` 判定「正在生成」，生成中再次 POST 只返回一个 popup 错误；`/interrupt` 取消任务。
- **生成与改会话互斥**：`_busy(key)` 让回滚 / 编辑 AI 块 / 换预设 / 改参数 / fork / 删除直接返回 409（`_prepare` 会重写 history、`_persist` 会重写 world，并发只会互相覆盖）。这些端点**都是 `async def`**，和 `_generate` 共用 `server._lock_for(会话键)`：`_generate` 在锁里完成「查 `_active` + `_prepare` + 注册」，所以「检查」与「注册」之间没有窗口——原来同步端点跑在工作线程池里，`_busy` 过了、`_active` 还没写上的那几毫秒里，回滚会被随后那一轮的 `save_history` 覆盖回去。锁只在注册阶段持有，**流式转发不持锁**（否则一次生成会把端点锁几分钟）；改会话端点的锁体里也不许有 await（会让出控制权），`_lock_check` 式的结构检查保证这一点。
- 事件类型：`reasoning` / `content` / `tool` / `done`（正文完成，不带 options）/ `options_start` / `options` / `options_error` / `error`。
- 失败不丢半截结果：后端/API 错误照打断的先例落盘（正文或「（生成失败，无正文输出）」占位、已执行的 tool_calls、tail 思维链），entry 上加 `error`（前端以「出错」标签 + 错误条显示），已执行的工具调用改动一并 commit 保持叙事与状态一致。仅当尚未开始流式输出时才只 abort 世界状态。

## 会话存储与操作

`sessions/<用户名>/<name>/`（未启用鉴权时是 `sessions/<name>/`）下有 `state.json`、`history.jsonl`、`world/`（state.json / lib.py / snapshots.json）。state.json 里的 `owner` 决定落盘位置（缺失 = 改造前的旧 state，按裸名落根目录），`id` 是用户作用域内的目录名（前端 URL 用的就是它）。回滚/重生成/打断时 server 调 `world.sync` / `abort_turn` / `commit_turn` 让世界状态与历史对齐。

所有落盘都是「写同目录临时文件 + `os.replace`」（`core._atomic_write_text`，临时名带 pid+线程 id+随机后缀，并按路径加一把进程内锁）：多人同时用、或者进程被 kill 时，不会留下半截的 state.json / history.jsonl / snapshots.json。那把锁不是多余的——FastAPI 的同步端点跑在工作线程池里，同进程两个线程同时写一个文件（最典型是两个人同时登录写 `auth_sessions.json`）在旧实现下实测每 1000 次约 39 次直接失败（Linux `FileNotFoundError`、Windows `WinError 32`），在应用里就是那一轮 history 没保存。

**已知的并发边界**：

- **`world._runtimes` 只增不减**：用过 world_run 的会话，其 exec 命名空间（含模型 `import` 进来的模块）一直留在内存，直到删会话。单进程 + unit 的 `MemoryMax` 兜底；要更稳可做 LRU 淘汰（安全点：`runtime_for` 本来就会从 `lib.py`/`state.json` 重建，但必须在轮次边界淘汰，别丢未提交的改动）。`server._locks` 同理（每会话一个空锁，量级相同）。
- 打断 `world_run`、`_busy` 与注册 `_active` 抢跑这两个窗口**已经加固**（前者见「工具」里的取消宽限，后者见上面那条会话锁），别再把它们当已知问题重复处理。

- **回滚**（用户块上的按钮）：history 截断到该块之前。
- **重生成**：丢掉最后一个 AI 块，用它回复的那条用户输入重发（末尾是 user 块时同样走这条路，就是前端那个「继续」）。
- **fork**（`POST /api/sessions/{name}/fork {index}`）：在用户块断点处复制出新会话（`core.fork_session`：state 复制 + `history[:index]`，名字 `原名-fork-时间戳`，原会话不动，`owner` 继承），`world.fork` 按同一 index 复制世界状态（新会话键同样带用户名作用域）。新会话末尾是 assistant 块（或空历史），可直接继续输入。
- **删除**：`shutil.rmtree(会话目录)` + `world.drop(键)`。
- 会话列表 / 参数继承 / 生成状态 / world 运行时全部按「带用户名的会话键」走，所以不同用户的同名会话互不可见也互不影响。
