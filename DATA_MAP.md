# 数据地图

## 配置数据

- `~/.nanobot/config.json`
  - 运行时配置来源，由 `nanobot/config/schema.py` 定义 Pydantic schema。
- `agents.defaults`
  - 模型、provider、上下文窗口、工具迭代次数、记忆压缩、时区等默认值。
- `tools`
  - Web、exec、image generation、MCP、workspace 限制、SSRF 白名单、Shell allow/deny 规则等工具配置。
- `providers`
  - 各 LLM 服务商凭证和 API base。敏感值应通过环境变量引用。

## 会话与记忆

- `sessions/*.jsonl`
  - 每个 channel/chat 的短期会话历史，由 `SessionManager` 原子写入。
- `memory/history.jsonl`
  - Consolidator 写入的压缩历史，Dream 从这里消费。
- `memory/retriever_index.json`
  - 轻量记忆检索索引，由 `MemoryRetriever` 持久化；索引压缩摘要全文、结构化摘要字段、更新时间和安全评估元数据，用于后续上下文构建时检索相关历史片段。
- `memory/MEMORY.md`
  - 项目事实、长期决策和稳定上下文；用户确认 `project_memory` 候选后会追加写入。
- `SOUL.md`
  - 助手长期表达风格；用户确认 `assistant_style` 候选后会追加写入。
- `USER.md`
  - 用户稳定偏好和画像；用户确认 `user_profile` 候选后会追加写入。

## 工具与上下文

- `OPTIMIZATION_ROADMAP.md`
  - 后续优化路线图数据源。记录每个优化方向的目标、当前状态、待做内容、验收标准、优先级和预计轮次，后续迭代应从该文档读取下一步计划。
  - 记录简历能力对齐后的剩余优化缺口，用于把 ReAct、工具体系、上下文压缩和跨会话记忆的描述落实为可追踪任务。
- `nanobot/skills/*/SKILL.md`
  - 内置 Skill，`coding-assistant` 默认注入；`code-review` 和 `test-fix` 可按工程任务关键词自动选择。Skill frontmatter 的 nanobot metadata 可声明 `always`、`task_keywords`、`priority` 和 `requires`；运行时只向 WebUI 暴露选择元数据，不暴露 Skill Markdown 正文。
- `skills/*/SKILL.md`
  - 用户工作区自定义 Skill。
- `nanobot/templates/*.md`
  - 系统提示、工具说明、记忆模板和 Dream 模板。
- `nanobot/agent/tools/*`
  - 文件、Shell、搜索、MCP、子 Agent、图片生成等工具实现。
- `ToolRegistrationMetadata`
  - 工具注册后的标准能力描述，由 `nanobot/agent/tools/base.py` 定义并由 `ToolRegistry` 缓存。字段包括 `name`、`description`、`parameters`、`config_key`、`plugin_discoverable`、`scopes`、`read_only`、`concurrency_safe`、`exclusive`。
- MCP 工具能力推断
  - `MCPToolWrapper` 根据 MCP annotations、工具名、描述和 schema 保守推断 `read_only`、`exclusive`，并由 `Tool` 基类推导 `concurrency_safe`；`MCPResourceWrapper` 和 `MCPPromptWrapper` 固定为只读。MCP wrapper 注册元数据统一包含 `config_key: "mcp"` 和 `scopes: ("mcp",)`。

## WebUI 数据流

- `webui/src/lib/nanobot-client.ts`
  - WebSocket 连接和事件分发。
- `webui/src/hooks/useNanobotStream.ts`
  - 单个对话的发送、流式消息、运行状态和停止逻辑；把后端 `tool_events` 归并到 `UIMessage.toolEvents`，把 `checkpoint` 事件归并到 `UIMessage.checkpoint`，把 `context_compaction` 事件归并到 `UIMessage.contextCompaction`，把 `memory_snapshot` 事件归并到 `UIMessage.memorySnapshot`，把 `active_skills` 事件归并到 `UIMessage.activeSkills`，把 `memory_candidate` 事件归并到 `UIMessage.memoryCandidate`。
- `webui/src/hooks/useSessions.ts`
  - 会话列表与历史读取。
- `webui/src/lib/tool-traces.ts`
  - 将后端工具调用 trace 转为 UI 可理解的类别：读取、修改、搜索、命令、检查和其他工具；根据工具事件的 `phase`、`result` 与 `error` 推断运行中、通过、失败状态，并保留 `checkpoint_id`、`risk_category`、`risk_level`、`safety`、注册能力、`elapsed_ms` 和 `duration_ms` 元数据。
- `webui/src/components/thread/AgentActivityCluster.tsx`
  - 消费 `UIMessage.kind === "trace"` 中的 `traces`、`toolEvents`、`fileEdits`、`checkpoint`、`contextCompaction`、`memorySnapshot`、`activeSkills` 与 `memoryCandidate`，生成工程活动阶段状态、当前阶段摘要、任务快照、上下文压缩卡片、长期记忆来源卡片、检索记忆元数据卡片、Active skills 卡片、长期记忆候选确认卡片、工具风险标签、检查状态、结果摘要、来源标识、恢复复用/补偿计数、恢复审查详情、文件区域分组、变更类型、文件扩展名和 diff 统计。
- `nanobot/webui/transcript.py`
  - 后端历史回放层。将保存的进度事件重新归并为前端可消费的 `toolEvents`、`checkpoint`、`contextCompaction`、`memorySnapshot`、`activeSkills` 与 `memoryCandidate`，保持实时流与历史加载一致。
- `nanobot/session/webui_turns.py`
  - 将 runner 内部 checkpoint 压缩成 WebUI 轻量结构：`checkpoint_id`、`turn_id`、`phase`、`tool_call_count`、`pending_tool_count`、`completed_tool_count`、`pending_tool_call_ids`、`completed_tool_call_ids`、`executed_tool_call_ids`、`reused_tool_call_ids`、`reused_tool_count`、`compensation_tool_call_ids`、`compensation_tool_count`、`retryable_tool_call_ids`、`retryable_tool_count`、`requires_user_tool_call_ids`、`requires_user_tool_count`、`resumable_tool_call_ids`、`resumable_tool_count`、`recovered_executed_tool_call_ids`、`recovered_executed_tool_count`、`recovered_skipped_tool_call_ids`、`recovered_skipped_tool_count`、`recovered_requires_user_tool_call_ids`、`recovered_requires_user_tool_count`、`safe_resume_tool_call_ids`、`review_required_tool_call_ids`、`needs_input_tool_call_ids`、`blocked_tool_call_ids`、`recovery_review_items`、`skipped_duplicate_tool_call_ids`、`skipped_duplicate_tool_count`、`last_tool_call_id`、`file_edit_count`、`check_state`、`recoverable`、`updated_at`；恢复场景额外携带 `source: "recovered"`、`recovered` 和 `recovered_pending_tool_count`；同时发布上下文压缩事件、记忆来源快照、Active skills 和记忆候选。
- `nanobot/utils/progress_events.py`
  - 为工具生命周期事件生成结构化元数据：`checkpoint_id` 关联当前 runtime checkpoint，`phase` 覆盖 `queued`、`start`、`running`、`end`、`error`，`risk_category` 覆盖 `read`、`write`、`shell`、`network`、`mcp`、`tool`，`risk_level` 覆盖 `low`、`medium`、`high`，`read_only`、`concurrency_safe`、`exclusive`、`config_key`、`scopes` 保存工具注册能力，`started_at`、`elapsed_ms`、`duration_ms` 保存非阻塞执行状态，`safety` 保存 UI 可展示的安全上下文。
- `nanobot/agent/tools/shell.py`
  - Shell 工具的危险命令数据来源。内置 deny 规则会拦截递归删除、磁盘格式化、裸盘写入、系统电源控制和 nanobot 内部记忆状态文件覆盖等操作，并返回具体原因。
- `nanobot/agent/memory_candidates.py`
  - 生成 `memory_candidate` 元数据：`type`、`target`、`content`、`reason`、`turn_id`、`sensitive`、`duplicate`；确认写入时再次校验敏感内容与重复内容。重复检测按目标文件隔离，支持完全包含和近似相似判断；重复写入返回 `duplicate_reason` 与 `existing_preview`，不包含额外敏感正文。
- `nanobot/agent/memory.py`
  - Consolidator / AutoCompact 生成上下文压缩元数据：`reason`、`source`、压缩前后消息数、归档消息数、保留消息数、估算 token、节省 token、摘要 token、摘要预览和 `summary_sections`。结构化字段包括 `overview`、`goal`、`constraints`、`files_touched`、`commands_run`、`failures`、`decisions`、`next_steps`；写入前会脱敏疑似凭据并限制每节条目数与单条长度；memory snapshot 会附加 `retrieved` 元数据汇总。
- `nanobot/agent/context.py`
  - 生成安全的记忆来源快照：`memory`、`user`、`soul`、`recent_history`、`session_summary` 的 `included`、存在状态、字符数、估算 token、条目数和更新时间；`retrieved` 只包含检索命中的 `included`、`entry_count`、`categories`、`reasons` 和 item 级 `id`、`source`、`category`、`reason`、`safety`、`updated_at`；不传输原始记忆正文或 snippet。构建系统提示时会基于当前任务自动选择工程 skill，并生成 active skills snapshot：`version`、`skills[]`、`selection_limit`、`updated_at`，其中 `skills[]` 只包含 `name`、`source`、`matched_keywords`、`priority`、`reason`；优先使用 `_last_summary.sections`，并把当前用户请求、近期历史中的文件路径、错误和命令信号合并为 `MemoryRetriever.query()` 查询，注入 `[Retrieved Memories]` 短片段、来源、安全等级、类别和命中原因。
- `nanobot/agent/retriever.py`
  - 轻量检索数据结构：倒排索引 term -> doc/tf、doc 元数据、更新时间、token 数、`safety` 评估、`category` 类别和 `match_reason` 命中原因；token 列表会保留完整文件路径，例如 `nanobot/api/server.py`；支持持久化、加载、重建、并发写入队列和按相关性查询。
- `webui/src/i18n/locales/*/common.json`
  - 页面文案、快捷动作提示词和工程活动面板文案。

## 后续数据缺口

- 恢复确认数据：需要为 `recovery_review_items` 继续补充可展示的参数摘要、确认状态、用户补充输入和恢复执行结果，不暴露敏感参数原文。
- 检查结果数据：需要结构化保存失败命令、退出码、关键错误行、关联文件和建议动作，避免 WebUI 只能展示原始日志。
- diff 预览数据：需要为文件变更提供行级摘要、大小限制、二进制标识和大文件截断原因。
- 记忆合并数据：需要记录相似长期记忆的候选组、合并原因、目标文件和用户确认状态，避免只做重复拦截。
- Skill 边界数据：需要为专用工程 skill 增加适用任务、冲突关系、触发阈值和上下文预算字段。

## 2026-05-22 本轮数据更新

- `agents.defaults.maxConcurrentTools`
  - 新增工具并发上限配置，默认 `4`，由 `AgentLoop` 传入 `AgentRunSpec.max_concurrent_tools`。
- `ToolProgressEvent`
  - `phase` 新增 `queued`；工具事件新增 `queued_at`、`batch_id`、`batch_index`、`batch_count`、`batch_size`、`concurrency_limit`、`queue_position`，用于表达工具排队、分批和限流状态。
- `ParsedToolTrace`
  - 新增调度字段和 `queued` 状态，WebUI 可从结构化事件还原工具调度摘要、单工具批次位置和耗时。
- 工具失败恢复字段
  - `failure_category` 描述失败类型，例如 `safety_block`、`workspace_boundary`、`external_lookup_repeated`、`tool_exception`、`tool_error_result`。
  - `recovery_action` 描述建议恢复动作，例如 `retry`、`retry_alternative`、`revise_arguments`、`revise_request`、`ask_user`、`use_existing_context`。
  - `retryable` 和 `needs_user_input` 提供前端展示与后续恢复逻辑可直接消费的布尔信号。
  - MCP 错误结果会被归类为 `mcp_timeout`、`mcp_connection_interrupted`、`mcp_protocol_error`、`mcp_permission_denied`、`mcp_tool_error`，并映射到同一组恢复字段。
- checkpoint 恢复策略字段
  - `retryable_tool_call_ids` / `retryable_tool_count` 记录恢复时判定为可重试但未自动执行的 pending 工具。
  - `requires_user_tool_call_ids` / `requires_user_tool_count` 记录恢复时判定为需要用户确认或补充输入的 pending 工具。
  - `resumable_tool_call_ids` / `resumable_tool_count` 记录恢复时判定为安全恢复候选的 pending 工具。当前判定要求工具注册元数据满足只读、并发安全、非独占，且工具名不是 Shell、写入类或 mutating MCP 名称模式。
  - `safe_resume_tool_call_ids` / `safe_resume_tool_count` 记录可通过 `/resume-safe-tools` 自动续跑的只读安全工具。
  - `review_required_tool_call_ids` / `review_required_tool_count` 记录默认需要用户审查后才能重试的 Shell、写入、mutating MCP 或其他非只读 / 独占工具。
  - `needs_input_tool_call_ids` / `needs_input_tool_count` 记录需要用户补充输入或修改请求的 pending 工具。
  - `blocked_tool_call_ids` / `blocked_tool_count` 记录被安全策略阻断的 pending 工具。
  - `recovery_review_items` 记录 UI 安全审查条目，每项包含 `tool_call_id`、`name`、`group`、`reason` 和 `recovery_action`，不包含工具参数或敏感正文。
  - `recovered_executed_tool_call_ids` / `recovered_executed_tool_count` 记录 `/resume-safe-tools` 已实际执行并写回 tool result 的安全工具。
  - `recovered_skipped_tool_call_ids` / `recovered_skipped_tool_count` 记录安全恢复入口跳过的工具。
  - `recovered_requires_user_tool_call_ids` / `recovered_requires_user_tool_count` 记录仍需要用户确认或补充信息的工具。
  - `compensation_tool_call_ids` 仍表示已经写入安全占位结果，避免模型上下文中遗留未闭合的 tool call。
- 工具注册元数据
  - `Tool.validate_registration()` 在注册时校验工具名、描述、对象参数 schema、`properties`、`required` 和 `_scopes`。
  - `ToolRegistry.get_metadata(name)` 返回单个工具的元数据副本，`get_metadata_map()` 返回全部注册工具元数据副本，避免调用方意外修改 registry 内部缓存。
- 工具事件能力字段
  - `read_only` 表示工具是否声明为只读。
  - `concurrency_safe` 表示调度器是否可把该工具与其他安全工具并发。
  - `exclusive` 表示该工具是否需要独占执行。
  - `config_key` 指向工具配置分组，例如 `exec`、`web`、`image_generation`。
  - `scopes` 表示工具注册范围，例如 `core`、`subagent`、`memory`。
- 路线图跟踪数据
  - `OPTIMIZATION_ROADMAP.md` 将剩余优化拆为 P0 到 P3：智能恢复执行、上下文压缩策略升级、长期记忆主动检索增强、Skills 系统工程化、MCP / 插件工具能力完善、WebUI 工程任务工作台。
  - 每个条目包含目标、当前状态、待做内容、验收标准和预计轮次，作为后续开发优先级依据。
- 2026-05-27 文档校准数据
  - 安全恢复入口、结构化上下文摘要、轻量记忆检索、MCP 能力推断和 MCP 失败分类已从路线图“待做”校准为“当前状态”。
  - 后续重点数据缺口收敛为写入 / Shell / needs input 的确认式恢复策略、插件诊断、长期记忆分类与命中原因、Skills 自动选择和 WebUI 工作台详情。
- 2026-05-27 P1 上下文压缩数据更新
  - `summary_preview` 和 `summary_sections` 会统一经过脱敏，覆盖私钥块、Authorization bearer、常见 `api_key` / `secret` / `token` / `password` 赋值和命令行敏感 flag。
  - `summary_sections` 每节最多保留 8 条；`commands_run` 单条最多 180 字符，`failures` 单条最多 320 字符，其他字段单条最多 240 字符。
  - 从归档消息推断失败信息时优先保留包含 error / failed / traceback / exception 的关键行，避免大段原始日志进入长期上下文。
- 2026-05-27 P1 长期记忆检索数据更新
  - retriever doc 会保存 `category`，当前类别包括 `project_fact`、`user_preference`、`assistant_style`、`decision`、`failure`、`command`。
  - query 结果会携带 `match_reason`，当前格式包括 `term:<token>`、`section:<summary_section>`、`path:<path_token>` 或 `category:<category>`。
  - ContextBuilder 只把短 snippet、source、safety、category、reason 注入 `[Retrieved Memories]`，不扩大长期记忆正文暴露面。
  - memory snapshot 新增 `retrieved` 汇总字段，供 WebUI 展示命中条目数、类别计数、命中原因、来源和安全等级；该字段不会包含 `snippet` 或检索正文。
  - 检索查询现在合并 session summary、当前请求和近期历史信号；当前请求或历史中的完整路径、失败关键词和命令关键词可直接提升相关记忆命中。
  - 记忆候选去重会规范化标点、大小写、列表前缀和部分中英文同义表达；相同 target 内近似重复会被拦截，不同 target 不互相影响。
- 2026-05-27 P2 Skills 数据更新
  - Skill metadata 新增工程任务匹配字段：`task_keywords` 用于关键词触发，`priority` 用于多 skill 排序；`always` 仍表示默认注入。
  - ContextBuilder 会把当前请求和结构化 session summary 作为 skill 选择输入，自动注入最多 2 个非 always 工程 skill。
  - 新增内置 `code-review` 和 `test-fix`，分别覆盖代码审查 / 回归风险、测试失败 / 构建错误场景。
  - `select_task_skill_matches()` 返回安全解释元数据：skill 名称、来源、命中关键词、优先级、原因和 skill 来源目录；兼容接口 `select_task_skills()` 仍只返回名称。
  - Active skills WebUI 事件只包含选择快照，不包含 Skill Markdown 正文；实时流与 transcript 回放统一使用 `activeSkills`。
