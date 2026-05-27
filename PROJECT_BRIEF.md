# 项目简报

## 定位

Mini-Nanobot 是基于 nanobot 改造的 AI 编程助手方向版本。它保留原项目轻量 Agent 框架、多渠道接入、工具调用、MCP、Session 记忆和 Dream 长期记忆能力，同时把默认体验收束到工程任务：读代码、规划小 diff、调用工具、运行检查、总结变更。

## 当前架构要点

- 后端核心是 `AgentLoop` 状态机和 `AgentRunner` ReAct 工具循环。
- 工具通过 `Tool` 基类、JSON Schema 参数和 `ToolLoader` 自动注册。
- 上下文由 `ContextBuilder` 汇总系统提示、AGENTS/SOUL/USER/TOOLS、Skills、Session 历史和 Memory。
- 记忆分为 Session 短期上下文、`memory/history.jsonl` 压缩历史、`memory/MEMORY.md` / `SOUL.md` / `USER.md` 长期文件。
- WebUI 是 React + TypeScript + Vite 的聊天界面，通过 WebSocket gateway 与后端通信。

## 已完成方向

- 新增默认启用的 `coding-assistant` 编程助手 Skill。
- 新增工程任务 Skill 自动选择骨架，`code-review` 和 `test-fix` 可根据当前请求或结构化 summary 中的审查、回归、测试失败等关键词自动注入。
- 新增 Skills 可观测链路，本轮上下文启用的 skill 会以安全元数据形式进入 WebUI 活动面板，展示名称、来源和匹配原因，不展示 skill 正文。
- 在系统提示和工具说明中强化工程任务工作流。
- 将 WebUI 首页快捷入口调整为更贴近代码项目的任务。
- 将 Agent 活动区改为工程活动面板，突出读取项目、工具步骤、文件变更和检查结果。
- 将检查命令结果可视化，支持运行中、通过、失败状态和结果摘要展示。
- 增强文件变更可视化，按前端、后端、测试、文档、配置分组，并展示新增、修改、删除类型与文件扩展名。
- 增加工程活动阶段化时间线，将读取、工具、编辑、检查、完成阶段的状态集中展示。
- 增加任务快照可视化，基于现有 trace、工具事件、文件变更和检查结果重建当前任务状态。
- 打通后端运行时 checkpoint 到 WebUI 的透传链路，任务快照可优先消费后端保存的阶段、工具数、文件编辑数和检查状态。
- 补充 checkpoint 恢复来源标识，恢复后的任务快照会显示 `Recovered checkpoint`，与实时执行和历史重建区分开。
- 打通上下文压缩可观测链路，Consolidator / AutoCompact 会生成压缩前后消息数、估算 token、节省 token 和摘要预览，并在 WebUI 中显示 `Context compressed`。
- 打通长期记忆来源可观测链路，WebUI 可显示 `Memory snapshot`，说明本轮上下文使用了项目记忆、用户画像、助手风格、近期历史或 session summary 中的哪些来源，且只暴露来源、大小和更新时间等元数据。
- 打通长期记忆候选闭环，用户明确要求记住偏好或项目事实时生成 `memory_candidate`，WebUI 展示候选并在用户确认后写入 `USER.md`、`SOUL.md` 或 `memory/MEMORY.md`。
- 长期记忆候选写入前会进行目标文件内近似去重，重复内容会返回 `duplicate_reason` 和 `existing_preview`，避免同一偏好或项目事实反复追加。
- 打通工具安全分级可观测链路，工具生命周期事件携带 `risk_category` / `risk_level` / `safety` 元数据，WebUI 可显示 `Read`、`Write`、`Shell`、`Network`、`MCP`、`High risk` 和 `Blocked` 标签。
- 增强 Shell 危险命令拦截说明，默认 deny 规则会返回具体危险类型，例如递归删除、磁盘操作、系统关机或 nanobot 内部记忆状态文件覆盖。
- 标准化 runtime checkpoint 元数据，runner checkpoint 现在包含稳定 `checkpoint_id`、待执行工具 ID、已完成工具 ID、已执行工具 ID、工具计数和可恢复标记。
- 工具生命周期事件透传 `checkpoint_id`，WebUI 可把工具 start/end/error 与同一轮任务快照关联起来，为后续断点续跑和避免重复执行打基础。
- 增强 checkpoint 恢复幂等性，恢复时按 `tool_call_id` 复用已完成工具结果、跳过重复工具结果，并记录被补偿为中断错误的 pending 工具数量。
- 增强异步工具执行可观测性，长时间运行的工具会定期发送 `running` heartbeat，工具事件携带开始时间、已运行时长和最终耗时，便于 Shell/测试命令的非阻塞状态展示。
- 标准化工具注册契约，`Tool` 基类可校验名称、描述、参数 schema 和 scope，`ToolRegistry` 暴露统一注册元数据，供插件、调度器和 WebUI 复用工具能力信息。
- 打通工具注册元数据到运行时和 WebUI，工具 queued/start/running/end/error 事件会携带只读、并发安全、独占、配置键和 scope 信息，界面可展示 `Parallel safe`、`Read only`、`Exclusive` 和 `Config: ...` 能力标签。
- 增强 checkpoint 恢复候选识别，恢复 pending 工具时会基于工具注册元数据标记安全 `resumable` 候选，WebUI 任务快照可显示 `Resumable` 数量。
- 打通 checkpoint 安全恢复执行入口，`/resume-safe-tools` 可执行已确认安全的 pending 工具，并记录 `recovered_executed_tool_call_ids`、`recovered_skipped_tool_call_ids` 和 `recovered_requires_user_tool_call_ids`。
- WebUI 会在存在安全恢复候选时显示 `Resume safe tools` 入口，触发后刷新 recovered checkpoint，让用户看到已恢复、跳过和仍需确认的工具状态。
- checkpoint 恢复审查已形成显式分组：只读安全工具进入 `safe_resume`，Shell / 写入 / mutating MCP 工具进入 `review_required`，需要用户补充信息的工具进入 `needs_input`，安全拦截工具进入 `blocked`；`/resume-safe-tools` 会分别返回审查、输入和安全拦截数量。
- WebUI 任务快照可展示 `Recovery review` 详情列表，按工具展示安全恢复、需审查、需输入和已阻断分组，并显示后端提供的恢复原因与建议动作，但不展示工具参数。
- 升级上下文压缩摘要为工程任务结构，压缩事件和 session summary 可携带 `goal`、`constraints`、`files_touched`、`commands_run`、`failures`、`decisions`、`next_steps` 等字段，WebUI 可展示结构化预览和完整摘要。
- 上下文压缩摘要会在写入 session metadata、WebUI 事件和轻量检索索引前进行脱敏与限长，避免 API key、token、密码、Authorization header、私钥和大段日志污染长期上下文。
- 新增轻量长期记忆检索雏形，`MemoryRetriever` 会索引压缩摘要并在上下文构建时注入相关历史片段，帮助跨轮任务复用先前决策。
- 长期记忆检索结果会携带轻量类别与命中原因，例如 `decision`、`failure`、`command`、`project_fact` 以及命中的 term / section / path，ContextBuilder 注入时仍只展示短片段和元数据。
- `Memory snapshot` 现在会额外展示检索命中的类别、条目数、命中原因、来源和安全等级等元数据，仍不暴露检索记忆正文或 snippet。
- MCP resource / prompt wrapper 已声明为只读工具；普通 MCP tool 会基于 annotation、名称、描述和 schema 保守推断只读、并发安全与独占执行能力。
- MCP 调用对超时、取消和部分瞬时连接错误提供重试或明确错误结果，运行时会把 MCP timeout、连接中断、协议错误、权限问题映射到统一失败恢复字段。
- 新建并持续维护项目简报、页面地图、数据地图和变更记录文档。

## 后续方向

- 详细后续优化记录见 `OPTIMIZATION_ROADMAP.md`。
- 近期优先完善恢复策略表、MCP / 插件工具能力推断和上下文压缩测试覆盖。
- 中期继续增强长期记忆分类检索、Skills 自动选择和 WebUI 工程任务工作台。

## 简历描述对齐缺口

- ReAct 闭环已经能覆盖推理、工具调用、反馈续推理和 checkpoint 恢复，但恢复后的逐项确认、补充输入和继续推理体验仍不完整。
- 插件式工具体系已经有标准注册契约、参数 schema、并发调度和安全标签，但 MCP server 类型、网络工具和第三方插件的能力声明仍需更细。
- Session / 长期记忆已经支持结构化压缩、轻量检索、候选确认写入和近似去重，但记忆合并、冲突处理和保留优先级仍需优化。
- WebUI 已能展示工程活动、工具风险、检查状态、恢复审查和 Active skills，但还缺任务计划、测试失败详情、diff 预览和本轮总结工作台。

## 2026-05-27 文档校准

- 根据当前代码校准优化完成度：P0 安全恢复入口、P1 结构化上下文摘要和长期记忆轻量检索已具备雏形。
- 明确剩余缺口：恢复策略仍缺写入 / Shell / MCP 的细分确认流程，长期记忆检索仍缺分类、命中原因和去重合并，Skills 仍缺任务类型自动选择。
- 同步更新 `OPTIMIZATION_ROADMAP.md`、`PAGE_MAP.md`、`DATA_MAP.md` 和 `CHANGELOG.md`，保持后续优化来源一致。

## 2026-05-27 本轮更新

- 推进 P1 长期记忆主动检索增强：`MemoryRetriever` 会为压缩摘要推断 `project_fact`、`user_preference`、`assistant_style`、`decision`、`failure`、`command` 等轻量类别。
- 检索结果新增 `match_reason`，可说明命中文件路径、结构化 section 或关键词；ContextBuilder 注入 `[Retrieved Memories]` 时展示类别和原因，但仍只注入短片段。
- 记忆来源快照新增 `retrieved` 元数据汇总，WebUI `Memory snapshot` 会显示命中条目数、类别计数、原因和来源/安全等级，不显示检索正文。
- 记忆检索查询会结合结构化 session summary、当前用户请求和近期历史中的文件路径 / 错误 / 命令信号；retriever 会保留 `nanobot/api/server.py` 这类完整路径 token，用于更准确的 `path:` 命中原因。
- 长期记忆候选新增近似去重：确认写入和候选生成都会复用规范化匹配，忽略大小写、标点、列表前缀和常见同义表达；重复时返回原因和已有条目预览。
- 推进 P2 Skills 工程化第一阶段：Skill metadata 支持 `task_keywords` 和 `priority`，ContextBuilder 会在保留默认 `coding-assistant` 的基础上自动选择工程专用 skill。
- 新增 `code-review` 与 `test-fix` 内置 skill，分别服务代码审查 / 回归风险和测试失败 / 构建错误场景，避免项目偏离 AI 编程助手定位。
- 推进 P2 Skills 工程化第二阶段：`select_task_skill_matches()` 会返回匹配解释，active skills snapshot 会记录 `always` / `auto` / `explicit` 来源、命中关键词、优先级和原因。
- WebSocket、transcript 和 WebUI 工程活动面板新增 `Active skills` 可观测卡片，实时流与历史回放都能看到本轮启用的编程助手 skill，但不暴露 Skill Markdown 正文。
- `build_messages()` 现在会把 `session_metadata` 传入系统提示构建，使结构化 `_last_summary.sections` 路径也能触发相关记忆检索。
- 补充 retriever 和 ContextBuilder 集成测试，覆盖分类、命中原因和结构化 session summary 检索。
- 推进 P1 上下文压缩策略升级：压缩摘要和结构化 `summary_sections` 现在会统一脱敏并限制每节条目数与单条长度。
- `commands_run` 和 `failures` 会保留命令 / 关键错误摘要，但会移除 token、API key、密码、Authorization bearer 和私钥内容，避免把敏感日志带入 WebUI、session summary 和 retriever。
- 补充压缩事件测试，覆盖敏感值脱敏、长失败日志限长和关键命令 / 错误信息保留。
- 推进 P0 智能恢复执行确认式恢复：checkpoint pending 工具现在按 `safe_resume`、`review_required`、`needs_input`、`blocked` 生成 UI 安全审查分组。
- Shell、写入类工具和名称呈现写入 / 删除 / 执行语义的 MCP 工具会保守进入审查，不会通过 `/resume-safe-tools` 自动执行。
- `/resume-safe-tools` 命令返回信息区分“需审查”“需用户输入”“被安全策略阻断”，避免把不同恢复动作混成单一 review 提示。
- WebUI 工程活动面板新增恢复审查详情，用户展开任务快照后可直接看到待处理工具名、分组、原因和建议动作。
- 补充恢复分组和命令反馈测试，覆盖只读 MCP 安全候选、写入 / mutating MCP 审查、needs input 和安全拦截场景。
- 推进 MCP / 插件工具能力完善第一阶段：普通 MCP tool 现在会从 MCP annotations、工具名、描述和参数 schema 推断 `read_only`、`concurrency_safe` 与 `exclusive`。
- MCP tool / resource / prompt 注册元数据统一标记 `config_key: "mcp"` 和 `scopes: ("mcp",)`，WebUI 工具能力标签与 checkpoint 恢复候选判断可直接消费。
- AgentRunner 会识别 MCP 错误结果，并将 timeout、连接中断、协议错误、权限问题和普通工具错误映射为 `mcp_timeout`、`mcp_connection_interrupted`、`mcp_protocol_error`、`mcp_permission_denied`、`mcp_tool_error`。
- 补充 MCP wrapper 和 runner 恢复分类测试，验证只读 MCP 工具可进入安全恢复候选基础能力，MCP 错误可进入统一恢复元数据链路。

## 2026-05-22 本轮更新

- 增强多工具并发调度：工具执行从 `queued` 进入 `start/running/end/error`，并携带批次、队列位置和并发上限元数据。
- 默认工具并发上限收敛为可配置的 `agents.defaults.maxConcurrentTools`，避免并发安全工具一次性无限制 `gather`。
- WebUI 工程活动面板新增工具调度摘要，可展示运行中、排队中、并发限制、批次数以及单个工具的批次/队列位置和耗时。
- 增强工具失败恢复语义：错误事件会标记 `failure_category`、`recovery_action`、`retryable` 和 `needs_user_input`，区分安全拦截、workspace 边界、可重试异常和需要用户补充信息的失败。
- 增强 checkpoint 恢复闭环：恢复 pending 工具时会记录 retryable / needs input 策略计数和 ID，WebUI 任务快照可展示 `Retryable` 与 `Needs input` 状态，但仍不会自动重复执行工具。
- 标准化工具注册抽象层：注册时校验工具名、描述、对象参数 schema 和 scope，注册器缓存并暴露包含 `read_only`、`concurrency_safe`、`exclusive`、`config_key`、`scopes` 的工具元数据。
- 工具运行时事件消费注册元数据：排队、开始、心跳、结束和错误事件均可携带 `read_only`、`concurrency_safe`、`exclusive`、`config_key`、`scopes`，WebUI 工具行新增能力徽标。
- 新增 `OPTIMIZATION_ROADMAP.md`，集中记录剩余优化方向、优先级、待做内容、验收标准和预计轮次。
- 推进 P0 智能恢复执行第一阶段：只读、并发安全、非独占且非 Shell / 写入类 pending 工具会记录为 `resumable_tool_call_ids`，WebUI 可展示 `Resumable N`。
