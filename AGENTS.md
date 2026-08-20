# 用于AIRP的后端

一个究极简化版的SillyTavern，运行后启动一个后端并打开网站，和AI对话

# 用法
uv run -m main
默认在端口25530上启动一个双栈访问的服务器，并自动打开浏览器

AI在测试时应使用uv run -m main --port 25531 --no-browser避免和用户冲突

# 预设和角色卡
这两个概念来自SillyTavern，但本项目将它们大幅度简化

预设：功能上是在不同的世界观定义间通用的AI主持指南，定义的通用的文风，禁词表，防止全知等。逻辑上预设控制了如何基于对话历史和角色卡构造最开头的输入模型的上下文，包含系统提示词。
角色卡：功能上它的作用已经远超“定义单个角色”，或者把它叫“规则书”更合适。但逻辑上来说就是几个大字符串，预设通过宏决定将它插到上下文的什么地方。

预设格式参考./preset/梦鲸.md定义，定义了第一段系统提示词和用户的第一段回复。注意只有<preset_section>块是代码需要处理的格式标记，其它xml标记是预设的**内部文本**，需要原样发给模型。预设支持三种role: sysyem,user和assistant，不需要支持tool。其中还有宏标记，{{game_setting}}需要替换为角色卡中的世界设定（包含人物等等），{{game_beginning}}被替换为选择的角色卡开局。{{user_setting}}替换为用户人设。{{respond_tool}}替换为工具相关提示词。

宏的实现：所有{{...}}统一按Python表达式eval求值（app/core.py的render_template），上述四个固定宏只是求值环境中的同名变量。环境中还预置了random、time、math、datetime模块，可写{{random.randint(1,20)}}之类的表达式。严格模式：未知变量或执行出错直接抛ValueError，不做静默兜底。

预设还可以包含<preset_user_input>块（前后空白会被trim），作为用户输入的后处理模板，渲染时额外提供user_input变量。该渲染只作用于当前这一轮发送给模型的内容，落盘的history仍是渲染前的原文，因此这类附加提示只在最新一轮可见（回滚拿到原文，重生成会重新渲染）。无此块时用户输入原样透传。

角色卡格式参考./games/k❤s99.md，处理其中xml块并映射到对应的宏。

# 工具
模型有三个工具（schema 在 app/core.py，通过 Responses API function calling）：
- respond：唯一对用户可见的输出（正文+选项），必须是一轮回复的最后一次调用
- world_run：持久 Python 环境（app/world.py）。每会话一个 exec 命名空间，state 与顶层 def 函数持久化到 sessions/<name>/world/（state.json / lib.py / snapshots.json）。快照按历史长度存档，回滚/重生成/打断时由 server 调 world.sync/abort_turn/commit_turn 保持状态与历史一致。无超时保护
- read_file：只读分页读文件（app/tools.py），相对路径以项目根为基准，允许绝对路径

一轮回复是工具循环（app/llm.py 的 stream_respond）：模型可多次调用 world_run/read_file（执行结果追加进 input 继续请求），最后以 respond 结束。回合内 respond 之前的工具调用记录在 history 的 assistant 块 tool_calls 字段中，build_input 重放为 function_call/output 对。

思维链回传（DeepSeek 思考模式的硬性要求，违反会 400 "reasoning_text must be passed back"）：每个 function_call 前都必须紧跟产生它的那一轮**非空**思维链，同一轮多个调用重复传同一段文本（重复合法，缺失或空串不行）。因此 tool_calls 逐项带 reasoning 字段，assistant 块的 reasoning 字段只存 respond 轮的思维链；build_input 与工具循环都在每个 function_call 前插入对应 reasoning 项。模型个别轮（甚至整个回合）可能不输出思维链：先用前轮兜底，全都没有时用单空格占位（实测合法）。旧格式历史（tool_calls 无 reasoning）用整轮合并的 entry.reasoning 兜底。
