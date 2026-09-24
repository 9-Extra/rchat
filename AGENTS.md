# 用于 AIRP 的后端

一个究极简化版的 SillyTavern：启动后端并打开网页，和 AI 对话。

## 用法

```
uv run -m main
```

默认 25530 端口、双栈、自动开浏览器。`--keep-awake`（默认开）在运行期间调 Windows 的 `SetThreadExecutionState` 阻止系统空闲睡眠/休眠，`--no-keep-awake` 关闭；非 Windows 平台自动忽略。

**测试一律用 `uv run -m main --port 25531 --no-browser --no-keep-awake`**，避免和用户的实例冲突。

## 配置（config.yaml）

- `endpoints`：模型端点列表，前端按会话切换；启动时校验，为空直接报错退出。每项必填 `api_base` / `model` / `api_key`，可选 `display_name`（缺省用 model）、`api_type`（`"responses"` 默认 / `"chat_completions"`）、`x_opencode_session`。**端点身份 = 列表下标**，重排列表会改变旧会话的指向。
- 全局键：`temperature` / `max_tokens` / `reasoning_effort` / `options_enabled` / `user_agent`（覆盖 SDK 默认 UA）。
- 可按会话覆盖（存 state.json）：`temperature` / `max_tokens` / `reasoning_effort` / `endpoint` / `options_enabled`，前端可改（`POST /api/sessions/{name}/params`、`/endpoint`、`/reasoning_effort`、`/options`）。新会话继承 created_at 最大的那个会话的这几项，无会话时用端点 0 + config 默认；会话值非法/缺失回退 config 默认，config 值非法直接报错；fork 自动继承。
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

- **world_run**：持久 Python 环境（`app/world.py`）。每会话一个 exec 命名空间，三者持久化到 `sessions/<name>/world/`：state（state.json）、顶层 def 函数、全大写全局变量（常量，规则 `^[A-Z][A-Z0-9_]*$`）；函数与常量存源码进 lib.py，重放时按 `tree.body` 顺序 exec。无超时保护。
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
2. **选项**（`generate_options`）：正文完成后，若 `options_enabled` 为真，再用同一份 config 发一次非流式请求，只要选项 JSON。

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

- **输入**：`core.build_options_input(state, history, content, tool_calls, reasoning)` = 正文请求的输入 + 本轮助手回复 + 一条 user 指令（`core.OPTIONS_INSTRUCTION`：只要 2-4 条、只输出 `{"options": [...]}`、不要续写正文也不要调用工具）。
- **缓存约束（实测 deepseek 官方端点）**：必须与正文请求共用同一份 config——同 model、同 tools、同 thinking / reasoning_effort，且**不加 `response_format`**。任一项不一致，前缀缓存命中率从 98% 掉到 6%；作为「正文请求的严格续写」它自身只在末尾多几十个 token（命中约 96%）。所以别给选项请求单独压低思考，也别用 `response_format`（该端点不支持 json_schema，`json_object` 同样往 prompt 头部插说明、打断前缀）。
- **解析与兜底**（`core.parse_options`）：剥围栏 → `json.loads` → 退一步取第一个 `{` 到最后一个 `}`；失败追加一条提醒重发一次（`_OPTIONS_RETRY_HINT`，追加在末尾不影响前缀），仍失败则选项为空、错误写进 entry 的 `options_error`（前端给提示 +「重新生成选项」；与表示整轮失败的 `error` 区分）。正文不受影响。
- **失败现场**：两次都失败时，模型每次输出的原文（第 1/2 次分别标注，空输出记成「（空输出）」）经 `generate_options` 的第三个返回值落盘为 entry 的 `options_raw`，前端在错误行下折叠显示；整段原文同时进 WARNING 日志。模型调工具或被输出上限压空时 `_request_options_once` 不返回文本而直接给出说明（含 `finish_reason` / `status`+`reason`、工具调用的名字与参数），这条说明就是错误文本本身。
- 选项请求的 usage 以 INFO 一行打进日志（含缓存命中数与其中的思考 token 数，`llm._usage_note`）。
- `POST /api/sessions/{name}/regenerate_options`（`_prepare_options` / `_run_options`）只重掷最后一轮的选项：不重新生成正文、不新增块、不碰 world。

## PTC 模式

预设 frontmatter 加 `ptc: true`（如 `preset/GM-ptc.md`）即开启，形态对齐 DeepSeek Harness 的 PTC 训练分布：schema 只含 world_run 一个工具（调用其它工具名返回错误），其余工具降级为 world_run 命名空间内的 Python 绑定函数（`world._bindings_for` 按白名单挂载 read_file/write_file/edit_file/bash 各一个 `_*_binding`；签名写在 `{{respond_tool}}` 提示词和 world_run 的 schema 描述两处，都由 core 按白名单拼）。叙事仍是普通文本，工具只用于判定与记账，回合结束同样是「某一轮不再调用任何工具」，选项与普通模式一样由正文后的独立请求生成。保留名是 state/print 加上当前启用的绑定名，不允许模型的顶层 def/常量覆盖；会话中途切换预设由 `world._reconcile_bindings` 原地换掉绑定（保留当前 state 与函数定义）。

## 流式、落盘与失败

生成与 HTTP 连接解耦：一轮生成跑在会话级后台任务里（`_run`；`_active[name]` 存 task / events / wake / finished / mode / user_input），POST `start|chat|regenerate|regenerate_options` 与 `GET /api/sessions/{name}/stream` 都只是把 `events` 里的 SSE 文本转发出去（`_tail`，15s 一次 `: ping` 心跳）。

- 客户端断开（刷新、掉线、锁屏）只中断这次转发，生成照旧跑完并落盘。页面加载时看 `GET /api/sessions/{name}` 的 `generating`（`{mode, user_input}`，没在跑则 null，mode 可能是 `options`；本轮还没落盘，用户输入与被替换的旧 AI 块靠它还原），前端据此接上 `/stream` 重放整轮（幂等，可多客户端同时接），断了退避重连。
- `_gen_of` 判定「正在生成」，生成中再次 POST 只返回一个 popup 错误；`/interrupt` 取消任务。
- 事件类型：`reasoning` / `content` / `tool` / `done`（正文完成，不带 options）/ `options_start` / `options` / `options_error` / `error`。
- 失败不丢半截结果：后端/API 错误照打断的先例落盘（正文或「（生成失败，无正文输出）」占位、已执行的 tool_calls、tail 思维链），entry 上加 `error`（前端以「出错」标签 + 错误条显示），已执行的工具调用改动一并 commit 保持叙事与状态一致。仅当尚未开始流式输出时才只 abort 世界状态。

## 会话存储与操作

`sessions/<name>/` 下有 `state.json`、`history.jsonl`、`world/`（state.json / lib.py / snapshots.json）。回滚/重生成/打断时 server 调 `world.sync` / `abort_turn` / `commit_turn` 让世界状态与历史对齐。

- **回滚**（用户块上的按钮）：history 截断到该块之前。
- **重生成**：丢掉最后一个 AI 块，用它回复的那条用户输入重发。
- **fork**（`POST /api/sessions/{name}/fork {index}`）：在用户块断点处复制出新会话（`core.fork_session`：state 复制 + `history[:index]`，名字 `原名-fork-时间戳`，原会话不动），`world.fork` 按同一 index 复制世界状态。新会话末尾是 assistant 块（或空历史），可直接继续输入。
