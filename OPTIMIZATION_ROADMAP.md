# 优化路线图

本文档记录 Mini-Nanobot 朝“AI 编程助手”目标继续演进时仍需完成的优化项。当前版本已经具备 ReAct 工具循环、checkpoint、工具并发调度、工具失败恢复元数据、上下文压缩可观测性、长期记忆候选、工具注册元数据和 WebUI 工程活动面板；接下来重点从“可观测、可记录”推进到“可决策、可恢复、可主动协作”。

## 总体目标

- 让 Agent 在工程任务中能稳定完成“理解仓库 -> 制定计划 -> 调用工具 -> 修改代码 -> 运行检查 -> 总结结果”的闭环。
- 让工具系统具备明确的插件契约、安全分级、并发调度和恢复策略。
- 让上下文与记忆系统能在长任务、多轮任务和跨会话任务中保留关键工程信息。
- 让 WebUI 从活动日志升级为工程任务工作台，支持查看计划、工具轨迹、文件变更、检查结果和恢复入口。

## 当前完成度

估算完成度：约 81%。

已完成能力：

- ReAct 风格 LLM 推理与工具调用循环。
- 工具自动发现、注册契约校验、注册元数据查询。
- 工具并发调度、排队事件、运行 heartbeat、耗时统计。
- 工具风险标签、危险 Shell 命令拦截、失败恢复元数据。
- runtime checkpoint、幂等恢复占位、retryable / needs input 策略记录。
- `/resume-safe-tools` 安全恢复入口、恢复执行结果字段、确认式审查分组和 WebUI `Resume safe tools` 按钮。
- Session 短期记忆、长期记忆来源快照、长期记忆候选确认写入。
- 上下文压缩事件、token 节省可观测和工程任务结构化摘要字段。
- 上下文压缩摘要会做敏感信息脱敏、长日志限长和关键失败行保留。
- 轻量长期记忆检索器，可索引压缩摘要并在上下文构建时注入相关历史片段。
- 长期记忆检索会结合 session summary、当前请求和近期历史信号；结果带轻量类别和命中原因，memory snapshot 可展示检索命中的元数据，便于解释为什么某段历史相关且不泄露正文。
- MCP tool / resource / prompt 注册元数据统一进入 MCP scope；普通 MCP tool 可保守推断只读、并发安全和独占能力。
- MCP timeout、连接中断、协议错误、权限问题会进入统一工具失败恢复字段。
- WebUI 工程活动面板、任务快照、工具调度摘要、文件变更分组和检查状态展示。
- Skills 自动选择可观测链路，WebUI 可展示本轮启用的编程助手 skill 名称、来源和匹配原因。

主要缺口：

- checkpoint 已支持用户触发的安全续跑，MCP 只读候选识别更准确，写入类、Shell 类、mutating MCP、needs input 和 blocked 工具已进入审查分组，但仍缺真正的逐项确认 / 补充输入 UI。
- 上下文压缩已有结构化字段、敏感信息过滤和长日志限长，但工程任务保留策略与更多失败恢复场景测试仍需加强。
- 长期记忆已有轻量检索、类别、命中原因、当前请求信号检索、snapshot 元数据展示和目标文件内近似去重，但分类精度、相关性排序和真正的合并编辑还不够强。
- Skills 已具备默认 coding-assistant、少量专用工程 skill、任务自动选择和 WebUI 可观测；仍缺更多专用 skill、冲突边界和更细任务类型。
- MCP / 插件工具已有注册标准，但能力分级和失败恢复仍不够细。
- WebUI 还没有完整的“任务计划 / 恢复 / 测试详情 / 变更预览”工作台体验。

## 简历描述对齐后的剩余优化

简历中的核心表述已经覆盖本项目主线：ReAct 闭环、插件式 Tool 体系、Session / 长期记忆与上下文压缩。当前代码已具备可演示版本，但要让描述更稳，需要继续补齐以下工程细节：

- ReAct 闭环：已具备推理、工具调用、结果反馈、checkpoint 和中断恢复元数据；仍需补强逐项恢复确认、失败后自动续推理策略、恢复后的用户可解释操作流。
- 多工具协作：已具备工具注册元数据、并发调度、风险标签和 Shell 危险命令拦截；仍需补强网络工具、MCP server 类型、第三方插件工具的能力声明与诊断。
- 上下文工程：已具备结构化压缩、token 节省可观测、敏感信息过滤和检索索引；仍需补强最近用户确认、失败命令、文件变更和未完成计划的保留优先级。
- 跨会话记忆：已具备长期记忆候选、确认写入、轻量检索和近似去重；仍需补强相似记忆合并编辑、相关性排序和长期事实过期/冲突处理。
- 编程助手体验：已具备工程活动面板、Active skills、检查状态和安全恢复入口；仍需补强任务计划、测试失败详情、diff 预览、本轮总结和需要确认的恢复操作入口。

## 优先级路线

### P0：智能恢复执行

目标：

- 让 checkpoint 恢复从“识别可恢复”升级为“安全、可控地恢复执行”。

当前状态：

- 已记录 `pending_tool_call_ids`、`completed_tool_call_ids`、`retryable_tool_call_ids`、`requires_user_tool_call_ids`。
- 恢复时会复用已完成工具结果，并为 pending 工具写入安全占位，避免重复执行。
- 已能基于工具注册元数据识别安全恢复候选：只读、并发安全、非独占、非 Shell / 写入类 pending 工具会记录为 `resumable_tool_call_ids`。
- 已新增 `/resume-safe-tools` 命令，能执行已确认安全的 pending 工具，并更新 checkpoint 中的 `recovered_executed_tool_call_ids`、`recovered_skipped_tool_call_ids`、`recovered_requires_user_tool_call_ids`。
- WebUI 会在存在安全恢复候选时显示 `Resume safe tools` 入口，并展示 `Retryable`、`Needs input`、`Resumable` 及恢复后的执行结果计数。
- checkpoint 恢复会生成确认式审查分组：`safe_resume`、`review_required`、`needs_input`、`blocked`，其中 Shell、写入工具和名称呈现写入 / 删除 / 执行语义的 MCP 工具默认进入审查。
- `/resume-safe-tools` 的命令反馈已区分需审查、需补充输入和安全策略阻断的 pending 工具数量。
- WebUI 任务快照已可展示 `Recovery review` 详情列表，列出待处理工具名、分组、原因和建议动作，不展示工具参数。

待做内容：

- 继续完善工具恢复策略表：网络工具、MCP server 类型和配置来源参与恢复策略判断，减少仅靠名称推断。
- 将审查分组扩展为更清晰的逐项用户确认流程；写入类、Shell 类、needs input 类默认只展示待确认详情，不自动执行。
- WebUI 继续增加真正的确认 / 补充输入入口，例如对 `Review before retry` 条目提供确认按钮、参数摘要、失败原因和补充输入表单。
- 为恢复执行增加更细测试，继续覆盖网络工具、MCP server 类型、重复 tool result 去重和逐项确认。

验收标准：

- 中断后恢复时，已完成工具不会重复执行。
- 只读可重试工具可通过安全入口续跑，并写入新的 tool result。
- 写入类、Shell 类、needs input 类工具不会无确认自动执行。
- WebUI 能清楚展示恢复动作、结果、审查分组和每个待处理工具的下一步动作。

预计轮次：

- 2 到 3 轮。

### P1：上下文压缩策略升级

目标：

- 从普通摘要升级为面向工程任务的上下文压缩，优先保留目标、约束、文件、错误、测试结果和未完成事项。

当前状态：

- Consolidator / AutoCompact 能输出压缩事件、消息数、估算 token、节省 token、摘要预览和 `summary_sections`。
- 已定义工程任务分层摘要结构：`overview`、`goal`、`constraints`、`files_touched`、`commands_run`、`failures`、`decisions`、`next_steps`。
- session metadata 可保存 `_last_summary.sections`，`ContextBuilder` 会优先注入结构化 Archived Context Summary。
- WebUI 能展示 `Context compressed`、结构化摘要预览和完整摘要弹窗。
- 压缩摘要写入 WebUI 事件、session summary 和 retriever 前会统一脱敏常见凭据、私钥和 Authorization bearer，并限制每节条目数与单条长度。
- 失败信息推断会优先保留关键错误行，避免大段原始日志进入长期上下文。

待做内容：

- 强化压缩保留策略，优先保留最近的用户确认、失败命令、文件变更和未完成计划。
- 继续扩展敏感信息过滤词表和边界测试，覆盖更多 provider / shell 输出格式。
- 为压缩结果增加测试，覆盖长对话、工具密集型对话、失败恢复场景和结构化字段注入。

验收标准：

- 长任务压缩后仍能回答“做到了哪里、改了什么、还差什么”。
- 压缩摘要不包含敏感信息和大段原始日志。
- WebUI 可显示压缩摘要的结构化预览。

预计轮次：

- 1 到 2 轮。

### P1：长期记忆主动检索增强

目标：

- 让 Agent 不只是写入长期记忆候选，还能按当前工程任务主动检索相关记忆。

当前状态：

- 明确记忆意图会生成 `memory_candidate`。
- 用户确认后可写入 `USER.md`、`SOUL.md` 或 `memory/MEMORY.md`。
- 上下文构建前会发布 memory snapshot，但相关性选择较粗。
- 已新增轻量 `MemoryRetriever`，可索引压缩摘要、持久化 `memory/retriever_index.json`，并在构建上下文时按 session summary 查询相关历史片段。
- 检索结果带安全评估元数据，ContextBuilder 注入 `[Retrieved Memories]` 时只放短片段和来源标识。
- 检索结果会推断 `project_fact`、`user_preference`、`assistant_style`、`decision`、`failure`、`command` 等轻量类别。
- 检索结果会携带 `match_reason`，说明命中关键词、结构化 section、路径 token 或类别；ContextBuilder 注入时展示 category / reason。
- `build_messages()` 会把 session metadata 传入系统提示构建，结构化 `_last_summary.sections` 也能触发相关记忆检索。
- memory snapshot 会附加 `retrieved` 元数据汇总，WebUI 可展示命中条目数、类别计数、命中原因、来源和安全等级，不展示检索正文或 snippet。
- 检索查询会合并当前用户请求和近期历史中的文件路径、错误、命令信号；完整文件路径 token 会被保留并产生更准确的 `path:` 命中原因。
- 长期记忆候选生成和确认写入会按 target 做近似去重，重复内容返回 `duplicate_reason` 和 `existing_preview`，避免相同偏好或项目事实反复追加。

待做内容：

- 继续提升分类精度，减少关键词启发式误判。
- 继续增强相关性排序，减少当前请求信号带来的弱相关命中。
- 增加真正的重复记忆合并编辑策略，把相似条目合并为更清晰的一条，而不只是阻止重复追加。

验收标准：

- 用户偏好和项目决策能在后续相关任务中自动生效。
- 无关记忆不会大量进入上下文。
- 记忆检索过程可观测但不泄露原文。

预计轮次：

- 2 轮。

### P2：Skills 系统工程化

目标：

- 从单一默认 skill 扩展为按任务类型选择的工程技能系统。

当前状态：

- 已有默认 `coding-assistant` Skill。
- 上下文构建会注入 Skills。
- Skill metadata 已支持 `task_keywords` 和 `priority`，用于工程任务自动匹配。
- ContextBuilder 会保留默认 `coding-assistant`，并按当前请求或结构化 session summary 自动额外选择少量工程 skill。
- 已新增 `code-review` 和 `test-fix` 内置 skill，分别覆盖代码审查 / 回归风险和测试失败 / 构建错误场景。
- SkillsLoader 已新增可解释匹配接口 `select_task_skill_matches()`，可返回安全的匹配关键词、优先级、来源和原因；原 `select_task_skills()` 仍保留名称列表兼容接口。
- ContextBuilder 已新增 active skills snapshot，记录本轮注入的 `always` / `auto` / `explicit` skill 元数据。
- WebSocket、transcript、WebUI 实时流和工程活动面板已支持 `active_skills` / `activeSkills`，展开活动面板可看到 `Active skills` 紧凑卡片，且不暴露 Skill Markdown 正文。

待做内容：

- 继续新增专用 Skills：前端实现、迁移规划、依赖升级、文档同步。
- 为 Skill metadata 继续补充适用任务、冲突关系和更细的触发边界。
- 继续控制自动注入数量，避免上下文显著膨胀。
- 继续校准 WebUI 展示，把 Skill 可观测信息保持在编程助手任务解释层，不扩展为通用技能市场或配置中心。

验收标准：

- 代码审查类任务能自动使用 review skill。
- 测试失败类任务能自动使用 test-fix skill。
- Skill 注入不会显著膨胀上下文。
- WebUI 能展示本轮启用的 Skill 名称和匹配原因，且不泄露 Skill 正文。

预计轮次：

- 1 到 2 轮。

### P2：MCP / 插件工具能力完善

目标：

- 让 MCP 工具和外部插件工具具备更细的能力描述、安全边界和恢复策略。

当前状态：

- MCP wrapper 会注册为标准 Tool。
- ToolRegistry 已有统一注册元数据。
- 工具事件已能展示注册能力字段。
- MCP resource / prompt wrapper 已声明为只读工具，默认满足 `read_only` 和由基类推导的 `concurrency_safe`。
- MCP tool / resource / prompt 对 timeout、SDK cancel 和部分 transient connection error 有明确返回或一次重试。
- 普通 MCP tool 已根据 MCP annotations、名称、描述和 schema 保守推断 `read_only`、`concurrency_safe`、`exclusive`；明显只读的查询/读取工具可进入安全恢复候选，明显写入或命令类工具会被标记为非只读或独占。
- AgentRunner 已把 MCP 错误结果归类为 `mcp_timeout`、`mcp_connection_interrupted`、`mcp_protocol_error`、`mcp_permission_denied`、`mcp_tool_error`，并映射到 `recovery_action`、`retryable`、`needs_user_input`。

待做内容：

- 继续补充 MCP server 类型和配置来源参与能力推断，减少只靠名称 / 描述 / schema 的误判。
- WebUI 对 MCP 失败分类增加更明确的诊断文案和恢复提示。
- 插件工具注册失败时提供更明确的错误报告和 WebUI 可见诊断。
- 增加插件工具元数据文档，说明第三方工具如何声明能力。

验收标准：

- MCP resource / prompt 默认识别为只读并发安全。
- MCP 普通 tool 可保守推断只读 / 独占能力，失败能区分 timeout、连接中断、协议错误、权限问题。
- 插件冲突、非法 schema、非法名称能给出可操作诊断。

预计轮次：

- 1 到 2 轮。

### P3：WebUI 工程任务工作台

目标：

- 把活动面板从“日志展示”升级为“任务工作台”。

当前状态：

- 已有阶段时间线、任务快照、工具调度摘要、风险标签、恢复标签、文件变更分组、检查状态和 `Resume safe tools` 恢复入口。

待做内容：

- 增加任务计划视图：显示当前计划、已完成步骤、待确认步骤。
- 增加检查结果详情：对 pytest / npm / ruff 输出提取失败摘要。
- 增加文件变更预览：展示文件列表、diff 摘要、二进制或大文件提示。
- 扩展恢复操作入口：对 retryable / needs input 工具提供审查按钮、确认说明或补充输入提示。
- 增加“本轮总结”卡片：汇总修改、验证、风险和下一步。

验收标准：

- 用户无需展开原始 trace 就能知道任务状态。
- 检查失败时能快速定位失败命令和关键错误。
- 恢复场景中能看到下一步该点什么或该补充什么。

预计轮次：

- 3 到 4 轮。

## 建议执行顺序

近期 1 到 3 轮：

1. 智能恢复执行第三阶段：写入 / Shell / mutating MCP / needs input 工具逐项确认与补充输入入口。
2. MCP / 插件诊断完善：server 类型推断、WebUI 文案和插件注册错误报告。
3. 上下文压缩保留策略与测试覆盖。

中期 4 到 6 轮：

1. 长期记忆分类检索、命中原因和重复合并。
2. 智能恢复执行审查工作台。
3. Skills 专用工程技能扩展、冲突边界和触发精度校准。

后续增强：

1. WebUI 任务计划和恢复工作台。
2. 检查结果详情和文件变更预览。
3. 插件开发文档和能力诊断。

## 跟踪方式

- 每完成一轮优化，都更新本文档对应条目的“当前状态”和“已完成内容”。
- 同步更新 `PROJECT_BRIEF.md`、`PAGE_MAP.md`、`DATA_MAP.md`、`CHANGELOG.md`。
- 每轮优先保持小 diff，先完成后端数据契约，再接入 WebUI 展示，最后补测试和文档。
