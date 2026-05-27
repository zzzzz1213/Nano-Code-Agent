# 变更记录

## 2026-05-27

- 根据简历描述补充项目优化缺口梳理：按 ReAct 闭环、多工具协作、上下文工程、跨会话记忆和编程助手体验标注仍需优化的能力。
- `OPTIMIZATION_ROADMAP.md` 新增简历描述对齐后的剩余优化小节，明确逐项恢复确认、MCP / 插件能力诊断、上下文保留优先级、记忆合并和 WebUI 工作台仍是后续重点。
- `PROJECT_BRIEF.md` 新增简历描述对齐缺口，说明当前已具备可演示闭环，但恢复交互、插件能力、记忆治理和工程工作台仍需继续优化。
- `PAGE_MAP.md` 补充后续页面缺口，包括恢复工作台、测试详情、变更预览和本轮总结卡片。
- `DATA_MAP.md` 补充后续数据缺口，包括恢复确认数据、检查结果数据、diff 预览数据、记忆合并数据和 Skill 边界数据。
- P2 Skills 工程化第二阶段：SkillsLoader 新增 `select_task_skill_matches()`，在保留原 `select_task_skills()` 兼容接口的同时返回安全的匹配解释元数据。
- ContextBuilder 新增 active skills snapshot，记录本轮注入的 skill 名称、来源、命中关键词、优先级和原因；默认 `coding-assistant` 标记为 `always`，自动匹配 skill 标记为 `auto`，显式传入 skill 标记为 `explicit`。
- WebSocket、WebUI transcript 和实时流新增 `active_skills` / `activeSkills` 事件链路，历史回放与实时执行保持一致，且不传输 Skill Markdown 正文。
- WebUI 工程活动面板新增紧凑 `Active skills` 卡片，展示本轮启用的编程助手 skill 和自动匹配原因，保持项目定位为 AI 编程助手而非通用技能市场。
- 补充 SkillsLoader、ContextBuilder 和 AgentActivityCluster 测试，覆盖可解释匹配、active skills snapshot、显式 skill 来源和前端安全展示。
- P1 长期记忆检索新增轻量分类：压缩摘要索引会推断 `project_fact`、`user_preference`、`assistant_style`、`decision`、`failure`、`command`。
- 检索结果新增 `match_reason`，用于说明命中 term、结构化 section、path 或类别；ContextBuilder 注入 `[Retrieved Memories]` 时展示 category / reason，但仍只展示短片段。
- Memory snapshot 新增 `retrieved` 元数据汇总，后端只输出命中条目数、类别计数、原因、来源、安全等级和更新时间，不输出检索正文或 snippet。
- WebUI `Memory snapshot` 卡片新增检索元数据展示，显示 `Retrieved` 数量、类别 badge、命中原因和 item 级来源/安全等级。
- ContextBuilder 检索查询新增当前用户请求和近期历史信号，文件路径、失败关键词和命令关键词会与 session summary 一起参与长期记忆检索。
- MemoryRetriever tokenization 会保留完整文件路径 token，例如 `nanobot/api/server.py`，并可返回更准确的 `path:<path>` 命中原因；显式 `Decision:` 文本优先归类为 `decision`。
- 记忆候选确认写入新增近似去重：规范化标点、大小写、列表前缀和常见同义表达，相同 target 内相似内容会返回 `duplicate_reason` 与 `existing_preview` 并跳过写入。
- P2 Skills 工程化第一阶段：SkillsLoader 新增基于 `task_keywords` / `priority` 的工程任务 skill 选择，ContextBuilder 会在默认 `coding-assistant` 之外自动注入匹配 skill。
- 新增内置 `code-review` 与 `test-fix` skill，分别服务代码审查 / 回归风险和测试失败 / 构建错误场景，保持项目聚焦 AI 编程助手。
- `build_messages()` 现在会把 `session_metadata` 传给 `build_system_prompt()`，确保结构化 `_last_summary.sections` 路径也能触发相关记忆检索。
- 补充 retriever、ContextBuilder、AgentActivityCluster、SkillsLoader 和 memory candidate 测试，覆盖分类、命中原因、结构化 session summary 检索、memory snapshot 元数据展示、skill 自动选择和近似重复拦截。
- P1 上下文压缩新增摘要清洗流程：`summary_preview`、`summary_sections` 和 session summary 写入前会统一脱敏并限长。
- 压缩摘要会移除私钥块、Authorization bearer、常见 API key / secret / token / password 赋值和命令行敏感 flag；同时限制每节最多 8 条、单条长度按字段收敛。
- 从归档消息推断失败摘要时优先保留关键错误行，避免大段原始日志进入 WebUI、session summary 和 retriever。
- 补充压缩事件测试，覆盖敏感值脱敏、长日志限长和关键命令 / 错误信息保留。
- P0 智能恢复执行新增确认式恢复策略表：只读安全工具进入 `safe_resume`，Shell / 写入 / mutating MCP 工具进入 `review_required`，需要用户补充信息的工具进入 `needs_input`，安全拦截工具进入 `blocked`。
- `/resume-safe-tools` 仍只执行安全恢复候选，并在返回文案中区分需审查、需用户输入和被安全策略阻断的 pending 工具数量。
- 补充 checkpoint 恢复分组与命令反馈测试，覆盖只读 MCP 安全恢复候选、写入 / mutating MCP 审查、needs input 和 safety block 场景。
- WebUI 任务快照新增 `Recovery review` 详情列表，展示 checkpoint 中 `recovery_review_items` 的工具名、审查分组、恢复原因和建议动作，不暴露工具参数。
- 补充 AgentActivityCluster recovered checkpoint 测试，覆盖恢复审查详情展示。
- 文档校准：同步 `PROJECT_BRIEF.md`、`OPTIMIZATION_ROADMAP.md`、`PAGE_MAP.md` 和 `DATA_MAP.md`，把当前代码中已落地的安全恢复入口、结构化上下文摘要、轻量记忆检索和 MCP 只读 wrapper 从待做项调整为当前状态。
- 明确剩余优化缺口：恢复策略仍需覆盖写入 / Shell / MCP 的确认式审查流程，长期记忆仍需分类检索、命中原因和重复合并，Skills 仍需按任务自动选择，WebUI 仍需任务计划、测试详情、diff 预览和本轮总结工作台能力。
- 更新路线图建议顺序：近期优先推进确认式恢复、MCP / 插件能力推断、上下文压缩保留策略和测试覆盖。
- MCP 普通 tool 新增能力推断：根据 MCP annotations、工具名、描述和 schema 保守推断 `read_only`、`concurrency_safe`、`exclusive`，明显只读的查询/读取工具可进入安全恢复候选，明显写入或命令类工具会被标记为非只读或独占。
- MCP wrapper 注册元数据统一补充 `config_key: "mcp"` 和 `scopes: ("mcp",)`；resource / prompt 保持只读并发安全。
- AgentRunner 新增 MCP 错误结果分类：将 MCP timeout、连接中断、协议错误、权限问题和普通 MCP 工具错误映射到统一 `failure_category`、`recovery_action`、`retryable`、`needs_user_input` 字段。
- 补充 MCP 能力推断、MCP wrapper 注册元数据和 MCP 失败恢复分类测试。

## 2026-05-22

- 新增工具排队阶段：工具生命周期事件现在覆盖 `queued` / `start` / `running` / `end` / `error`，排队事件携带 `queued_at`。
- 新增工具调度元数据：工具事件会携带 `batch_id`、`batch_index`、`batch_count`、`batch_size`、`concurrency_limit`、`queue_position`，便于 WebUI 展示并发批次和队列位置。
- 新增 `agents.defaults.maxConcurrentTools` 配置，默认限制并发安全工具单批最多 4 个，避免一次性无限制并发执行。
- WebUI 工程活动面板新增工具调度摘要和批次徽标，支持展示运行中、排队中、并发限制、批次数与工具耗时。
- 新增工具失败恢复元数据：错误事件会携带 `failure_category`、`recovery_action`、`retryable`、`needs_user_input`，用于区分安全拦截、workspace 边界、可重试异常和需要用户输入的失败。
- WebUI 工具步骤新增恢复建议徽标，支持展示 `Retryable`、`Blocked`、`Needs input` 等状态。
- checkpoint 恢复新增策略闭环：pending 工具恢复时会记录 `retryable_tool_call_ids` 和 `requires_user_tool_call_ids`，WebUI 任务快照可展示 `Retryable N` 与 `Needs input N`，同时仍通过补偿 tool result 避免重复执行。
- checkpoint 恢复新增安全恢复候选字段：pending 工具若满足只读、并发安全、非独占且不是 Shell / 写入类，会记录到 `resumable_tool_call_ids`，WebUI 任务快照可展示 `Resumable N`。
- 新增工具注册契约校验：`Tool.validate_registration()` 会在注册时检查工具名、描述、对象参数 schema、`properties`、`required` 和 `_scopes`。
- 新增 `ToolRegistrationMetadata` 与 registry metadata API：`ToolRegistry.get_metadata()` / `get_metadata_map()` 可查询工具的 `config_key`、scope、只读状态、并发安全和独占执行标记。
- `ToolLoader` 自动发现的内置工具和插件工具现在复用同一注册元数据路径，并在 debug 日志中保留注册后的能力摘要。
- 工具生命周期事件新增注册能力字段：`queued` / `start` / `running` / `end` / `error` 均可携带 `read_only`、`concurrency_safe`、`exclusive`、`config_key`、`scopes`。
- WebUI 工程活动面板新增工具能力徽标，支持展示 `Parallel safe`、`Read only`、`Exclusive` 和 `Config: ...`。
- 新增 `OPTIMIZATION_ROADMAP.md`，集中记录后续优化路线、优先级、当前状态、待做内容、验收标准和预计轮次。
- 新增工具运行 heartbeat：长时间工具执行期间会定期发送 `phase: "running"` 的结构化事件，WebUI 可在不等待工具结束的情况下保持运行状态。
- 工具事件新增 `started_at`、`elapsed_ms`、`completed_at`、`duration_ms`，start/running/end/error 均可携带执行时长相关元数据。
- 增强 runtime checkpoint 恢复幂等性：恢复时按 `tool_call_id` 复用已完成工具结果、跳过重复工具结果，pending 工具仍 materialize 为中断补偿结果，避免后续恢复链路重复执行同一工具调用。
- recovered checkpoint 新增 `reused_tool_call_ids`、`compensation_tool_call_ids`、`skipped_duplicate_tool_call_ids` 及对应计数，WebUI 任务快照会显示复用与补偿工具数量。
- 标准化 runtime checkpoint payload：新增稳定 `checkpoint_id`、`pending_tool_call_ids`、`completed_tool_call_ids`、`executed_tool_call_ids`、工具计数和 `recoverable` 标记。
- 工具生命周期 start/end/error 事件新增 `checkpoint_id`，便于 WebUI 和后续恢复逻辑把工具执行与具体快照关联。
- WebUI checkpoint 类型与工具事件标准化逻辑同步支持 checkpoint 工具 ID 字段，任务快照可展示更完整的恢复状态数据。
- 新增工具风险元数据：工具 start/end/error 事件统一携带 `risk_category`、`risk_level` 和 `safety`，覆盖 read、write、shell、network、mcp、tool 等分类。
- WebUI 工程活动面板新增工具风险标签，支持显示 `Read`、`Write`、`Shell`、`Network`、`MCP`、`High risk` 和 `Blocked`。
- Shell deny 规则返回更具体的危险命令原因，例如递归删除、磁盘操作、系统电源控制和 nanobot 内部记忆状态文件覆盖，并保留原有 allow_patterns 优先级。
- 修复上下文压缩事件发布的兼容性：当旧测试或调用方返回非列表值时视为无压缩事件。

## 2026-05-21

- 新增长期记忆候选闭环：`AgentLoop` 在保存轮次后为显式“记住/偏好/以后”意图生成 `memory_candidate`，WebSocket 推送并写入 transcript。
- 新增 `nanobot/agent/memory_candidates.py`，负责记忆候选分类、敏感信息过滤、重复检测和确认后追加写入 `USER.md`、`SOUL.md`、`memory/MEMORY.md`。
- 新增 `/api/webui/memory-candidate/commit` 辅助路由和前端 `commitMemoryCandidate` helper，WebUI 的工程活动面板可展示候选并由用户点击 Save 后写入长期记忆。
- WebUI 实时流、历史回放和活动面板新增 `memoryCandidate` 支持，并补充 Python/前端测试覆盖候选生成、确认写入、事件吸收和卡片渲染。
- 新增长期记忆来源快照事件：构建 LLM 上下文前发布 `memory_snapshot`，记录项目记忆、用户画像、助手风格、近期历史和 session summary 的启用状态、大小、估算 token 与更新时间。
- WebSocket、transcript、`useNanobotStream` 与 `AgentActivityCluster` 新增 `memorySnapshot` 支持，WebUI 展开工程活动时会显示 `Memory snapshot`，且不暴露原始记忆正文。
- 新增上下文压缩可观测事件：Consolidator / AutoCompact 会记录压缩原因、来源、压缩前后消息数、归档/保留消息数、估算 token、节省 token 和摘要预览。
- WebSocket、transcript、`useNanobotStream` 与 `AgentActivityCluster` 新增 `context_compaction` / `contextCompaction` 支持，WebUI 展开工程活动时会显示 `Context compressed` 卡片。
- runtime checkpoint 恢复后新增 WebUI 可见的 recovered 标识，任务快照现在区分 `Live turn`、`Rebuilt from history`、`Recovered checkpoint`。
- checkpoint 恢复事件保留待完成工具数量，配合既有历史 overlap 去重逻辑，便于识别中断恢复且不重复执行的状态。
- 打通后端 runtime checkpoint 到 WebUI 的透传链路：`AgentLoop` 将 runner checkpoint 压缩为轻量任务状态，WebSocket 推送并写入 transcript。
- WebUI 实时流和历史回放新增 `checkpoint` 事件支持，`AgentActivityCluster` 的任务快照优先使用后端 checkpoint，缺失时继续使用前端重建数据。
- 增加后端 transcript 回放、前端流式 hook、工程活动面板相关测试覆盖 checkpoint 场景。
- 在工程活动面板中新增阶段化时间线，按 Reading、Tools、Editing、Checking、Done 展示 Pending、Running、Done、Failed 状态。
- 折叠态摘要改为优先展示当前阶段，例如 `Checking · 1 failed`、`Editing · 3 files`。
- 更新 Agent 活动面板测试覆盖，验证阶段时间线和当前阶段摘要。
- 在工程活动面板中新增任务快照卡片，基于现有活动数据展示当前阶段、工具数、文件数、检查状态、失败状态和实时/历史重建状态。
- 更新 Agent 活动面板测试覆盖，验证任务快照、失败快照和历史重建标记。

## 2026-05-20

- 新增 `coding-assistant` 内置 Skill，并设置为默认注入上下文。
- 在 Agent 身份提示中加入编程助手模式，强调读仓库、小 diff、验证和总结。
- 在工具说明中补充编程任务下的搜索、编辑和验证习惯。
- 将 WebUI 首页快捷动作调整为项目分析、代码修改、测试检查等编程助手入口。
- 将 WebUI Agent 活动区优化为工程活动面板，分组展示工具步骤、检查结果和文件变更。
- 新增工具 trace 解析与短标签能力，识别 `read_file`、`edit_file`、`exec`、`pytest`、`npm run build` 等常见工程动作。
- 更新 Agent 活动区与消息 trace 的测试覆盖，并通过前端测试与生产构建。
- 新建并更新 `PROJECT_BRIEF.md`、`PAGE_MAP.md`、`DATA_MAP.md`、`CHANGELOG.md`。
- 保留结构化 `toolEvents` 到 WebUI 消息和历史回放，支持工具开始、结束、错误事件更新同一条活动记录。
- 检查命令增加 Running、Passed、Failed 状态徽标和结果摘要展示。
- 增加前端流式、工程活动面板、后端历史回放相关测试覆盖，并通过前端测试、Python 回放测试与生产构建。
- 增强工程活动面板的文件变更展示：按前端、后端、测试、文档、配置分组，显示 Added、Modified、Deleted 类型徽标和文件扩展名。
