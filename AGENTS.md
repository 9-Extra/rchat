# 用于AIRP的后端

一个究极简化版的SillyTavern，运行后启动一个后端并打开网站，和AI对话

# 用法
uv run -m main
默认在端口25530上启动一个双栈访问的服务器，并自动打开浏览器

AI在测试时应使用uv run -m main --port 25531 --no-browser避免和用户冲突

# 配置
`config.yaml` 控制模型连接：
- `api_base` / `api_key` / `model`：常规模型连接参数。
- `api_type`：`"responses"`（默认，OpenAI Responses API）或 `"chat_completions"`（OpenAI 风格 Chat Completions API）。
- `temperature` / `max_tokens` / `reasoning_effort`：生成参数（`reasoning_effort` 仅在 `responses` 模式下生效）。

切换 `api_type` 时，上下文拼装、工具 schema、流式调用实现会自动切换；落盘的 `history.jsonl` 格式不变。

# 预设和角色卡
这两个概念来自SillyTavern，但本项目将它们大幅度简化

预设：功能上是在不同的世界观定义间通用的AI主持指南，定义的通用的文风，禁词表，防止全知等。逻辑上预设控制了如何基于对话历史和角色卡构造最开头的输入模型的上下文，包含系统提示词。
角色卡：功能上它的作用已经远超“定义单个角色”，或者把它叫“规则书”更合适。但逻辑上来说就是几个大字符串，预设通过宏决定将它插到上下文的什么地方。

预设格式由 `./preset/GM.md` 示例定义：用 `<preset_section role="system|user|assistant">` 块组织内容，只有 `<preset_section>` 是代码处理的格式标记，其它 xml 标签是预设的**内部文本**，原样发给模型。预设中可用宏：`{{game_setting}}` 替换为角色卡世界设定，`{{game_beginning}}` 替换为选中的开局，`{{user_setting}}` 替换为用户人设，`{{respond_tool}}` 替换为工具相关提示词。

宏的实现：所有{{...}}统一按Python表达式eval求值（app/core.py的render_template），上述四个固定宏只是求值环境中的同名变量。环境中还预置了random、time、math、datetime模块，可写{{random.randint(1,20)}}之类的表达式。严格模式：未知变量或执行出错直接抛ValueError，不做静默兜底。

预设还可以包含<preset_user_input>块（前后空白会被trim），作为用户输入的后处理模板，渲染时额外提供user_input变量。该渲染只作用于当前这一轮发送给模型的内容，落盘的history仍是渲染前的原文，因此这类附加提示只在最新一轮可见（回滚拿到原文，重生成会重新渲染）。无此块时用户输入原样透传。

角色卡格式由 `<game_setting>`、`<user_setting>`、`<game_beginning>` 等 XML 块组成，宏名与上述固定宏同名。仓库内示例可参看 `./games/龙娘x猫娘.md`。

# 工具
模型有三个工具（schema 在 app/core.py）：
- respond：提交剧情推进选项并结束本轮回复（只含 options，无选项传空数组）；必须是一轮回复的最后一次调用。正文不经过工具，是模型的普通文本输出，直接流式给用户。
- world_run：持久 Python 环境（app/world.py）。每会话一个 exec 命名空间，state、顶层 def 函数、全大写全局变量（常量，识别规则 `^[A-Z][A-Z0-9_]*$`）三者持久化到 sessions/<name>/world/（state.json / lib.py / snapshots.json，函数与常量都存源码进 lib.py，重放时按 tree.body 顺序 exec）。快照按历史长度存档，回滚/重生成/打断时由 server 调 world.sync/abort_turn/commit_turn 保持状态与历史一致；fork 时由 world.fork 把断点处的快照（及更早快照）复制到新会话，无快照则兜底复制当前 committed。无超时保护
- read_file：只读分页读文件（app/tools.py），相对路径以项目根为基准，允许绝对路径

两种 `api_type` 都支持这三个工具的多轮调用。`responses` 模式下使用 Responses API 的 `function_call`/`function_call_output` 格式；`chat_completions` 模式下使用标准 OpenAI function-calling 的 `tool_calls`/`tool` 消息格式。落盘历史格式不变。

生成失败的处理（server.py _generate）：后端/API 错误不再丢弃半截结果——照打断的先例落盘（正文或「（生成失败，无正文输出）」占位、已执行的 tool_calls、tail 思维链），entry 上加 error 字段（前端以「出错」标签+错误条显示；build_input 回放时忽略该字段，不进模型上下文），已执行的工具调用改动一并 commit 保持叙事与世界状态一致；用户可修改/回滚/重新输出。仅当尚未开始流式输出（无内容可落盘）时才只 abort 世界状态。

一轮回复是工具循环（app/llm.py 的 stream_respond）：模型可多次调用 world_run/read_file（执行结果追加进输入继续请求），最后一轮输出正文（普通文本）并调用 respond 提交选项收尾。history 格式不变（content/options/reasoning/tool_calls 都在 entry 上）。

- `responses` 模式 build_input 回放：assistant 块 -> 工具循环的 function_call/output 对 + 正文 message + respond 调用（options JSON）+ "ok" 输出；user 块 -> 普通 user message，本次输入也拼成 user message，**请求以 user message 结尾**（不再是未闭合的工具循环）。
- `chat_completions` 模式 build_input 回放：assistant 块拆分为标准 messages：每个 tool_call 对应一条 assistant(tool_calls) + 一条 tool 消息；正文对应一条 assistant(content)；respond 对应一条 assistant(tool_calls) + 一条 tool("ok") 消息；user 块和本次 draft 对应 user 消息。

respond 契约自动修复（llm.py）：v4-flash 的主要失败模式是写完正文后不调 respond 直接收笔（纯文本收尾），偶尔反向把正文写进思考里只调 respond；上下文中的完整示范轮（正文 + respond 调用）是最强的行为稳定器，示范轮越多失败越少——因此回放时无论有无选项都固定带 respond 调用。修复手段：模型把正文写进思考里只调 respond 时，把该调用作为工具错误回传让模型补齐，最多修复 2 次；respond 带空选项时同样回传错误（提示一次后模型再传空数组视为有意，接受）；纯文本收尾（没调 respond）时在正文后追加一条 developer 元指令让模型补 respond——该消息只存在于本次工具循环，不落盘，无污染；修复用尽才宽容接受为无选项结束。预设侧配合：梦鲸的思考仪式里已把「输出正文 + 调用 respond」写成仪式的收尾步骤（模型对仪式遵循度很高）。

思维链回传（DeepSeek 思考模式）：`responses` 模式下**触发条件是请求以未闭合的工具循环结尾**，该循环内每个 function_call 前以及每个 assistant 正文 message 前都必须紧跟产生它的那一轮非空思维链；`chat_completions` 模式下则在每条 assistant message 中回传 `reasoning_content`。模型个别轮（甚至整个回合）可能不输出思维链：先用前轮兜底，全都没有时用单空格占位（实测合法）。旧格式历史（tool_calls 无 reasoning）用整轮合并的 entry.reasoning 兜底。

# 会话 fork
用户块上的「分支」按钮：POST /api/sessions/{name}/fork {index} 在用户块断点处复制出一个新会话（core.fork_session：state 复制 + history[:index]，新名为 原名-fork-时间戳，原会话不动），world.fork 按同一 index 复制世界状态。新会话末尾是 assistant 块（或空历史），可直接继续输入。
