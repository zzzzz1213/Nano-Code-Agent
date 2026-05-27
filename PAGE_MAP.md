# 页面地图

## WebUI

- `webui/src/App.tsx`
  - 应用启动、鉴权、WebSocket client 初始化、侧边栏/设置页/对话页切换。
- `webui/src/components/Sidebar.tsx`
  - 会话列表、归档、置顶、搜索入口、设置入口。
- `webui/src/components/thread/ThreadShell.tsx`
  - 对话主壳、首页欢迎区、快捷动作、消息流、输入区组合；根据最新 checkpoint 的 `resumable_tool_count` 控制安全恢复入口，并在用户点击后发送 `/resume-safe-tools`。
- `webui/src/components/thread/ThreadHeader.tsx`
  - 当前会话标题、侧边栏切换、主题切换、设置入口；存在安全恢复候选时显示 `Resume safe tools` 按钮。
- `webui/src/components/thread/ThreadViewport.tsx`
  - 消息视口、空状态、滚动到底部控制。
- `webui/src/components/thread/ThreadComposer.tsx`
  - 文本输入、图片附件、图片生成模式、斜杠命令、发送/停止。
- `webui/src/components/thread/AgentActivityCluster.tsx`
  - Agent 工程活动面板。折叠展示当前阶段摘要，展开后先显示读取、工具、编辑、检查、完成的阶段化时间线、上下文压缩事件、长期记忆来源快照、Active skills、长期记忆候选与任务快照，再分组显示推理、工具步骤、检查结果和文件变更；长期记忆来源快照会展示检索命中的类别计数、命中原因、来源和安全等级，但不展示记忆正文；Active skills 只展示本轮启用 skill 的名称、来源和自动匹配原因，不展示 Skill Markdown 正文；工具步骤会显示后端传入的风险标签、阻断状态与注册能力标签；任务快照优先消费后端 checkpoint，缺失时再从现有活动数据重建阶段、工具数、文件数、检查状态和失败状态，并区分 `Live turn`、`Rebuilt from history`、`Recovered checkpoint`，恢复态会显示复用、补偿、可重试、需输入和可安全恢复候选数量；当后端提供 `recovery_review_items` 时，会展示 `Recovery review` 详情列表，列出工具名、分组、原因和建议动作。
- `webui/src/components/MessageBubble.tsx`
  - 单条消息渲染。普通 trace 仍以可折叠工具组显示，并提供短标签与原始调用明细。
- `webui/src/components/settings/SettingsView.tsx`
  - 模型、服务商、图片生成、网页搜索、运行时和高级设置。

## 项目规划文档

- `OPTIMIZATION_ROADMAP.md`
  - Mini-Nanobot 后续优化路线图。记录距离 AI 编程助手目标仍需完成的能力，包括恢复策略审查、上下文压缩保留策略、长期记忆分类检索、Skills 工程化、MCP / 插件工具能力完善和 WebUI 工程任务工作台，并为每项列出当前状态、待做内容、验收标准和预计轮次。
  - 新增简历描述对齐缺口，按 ReAct 闭环、多工具协作、上下文工程、跨会话记忆和编程助手体验标注仍需优化的 UI 与后端能力。

## WebUI 支撑模块

- `webui/src/lib/tool-traces.ts`
  - 工具调用 trace 的去重、格式化、解析、分类和短标签生成；同时标准化结构化工具生命周期事件，支持 `queued` / `start` / `running` / `end` / `error`，保留风险元数据、注册能力元数据、`checkpoint_id` 和耗时字段并推断运行状态。
- `webui/src/hooks/useNanobotStream.ts`
  - WebSocket 流式消息聚合。保留 `tool_events` 为 `UIMessage.toolEvents`，并接收 `checkpoint` 事件为 `UIMessage.checkpoint`、`context_compaction` 事件为 `UIMessage.contextCompaction`、`memory_snapshot` 事件为 `UIMessage.memorySnapshot`、`active_skills` 事件为 `UIMessage.activeSkills`、`memory_candidate` 事件为 `UIMessage.memoryCandidate`，让开始、结束、错误、后端 checkpoint、上下文压缩事件、记忆来源快照、Skill 选择快照和记忆候选进入同一条活动流。
- `webui/src/i18n/locales/*/common.json`
  - 页面文案、快捷动作提示词和工程活动面板文案。

## WebUI 历史回放

- `nanobot/webui/transcript.py`
  - 将历史会话事件回放为 WebUI 消息。合并工具 trace、结构化 `toolEvents`、后端 `checkpoint`、`context_compaction`、`memory_snapshot`、`active_skills` 与 `memory_candidate`，确保刷新页面后检查状态、任务快照、上下文压缩记录、记忆来源快照、Skill 选择快照和记忆候选仍可恢复。

## 后端入口

- `nanobot/cli/commands.py`
  - CLI 命令入口。
- `nanobot/api/server.py`
  - OpenAI 兼容 API。
- `nanobot/channels/websocket.py`
  - WebUI 使用的 WebSocket 通道与 HTTP 辅助路由；同时提供长期记忆候选确认写入接口 `/api/webui/memory-candidate/commit`。
- `nanobot/command/builtin.py`
  - 内置斜杠命令处理。`/resume-safe-tools` 会调用 AgentLoop 的安全恢复逻辑，执行可恢复的只读 pending 工具，并向用户返回恢复、跳过、需审查、需补充输入和安全拦截的数量摘要。

## 后续页面缺口

- 恢复工作台：在 `AgentActivityCluster` / `ThreadHeader` 周边补充逐项确认按钮、参数摘要、失败原因和补充输入表单。
- 测试详情：在工程活动面板中对 pytest、ruff、npm / bun build 输出提取失败摘要、关键文件和可重试建议。
- 变更预览：在文件变更分组中补充 diff 摘要、大文件提示和二进制文件提示。
- 本轮总结：新增轻量总结卡片，展示本轮目标、已改文件、已跑检查、剩余风险和下一步。

## Agent 运行链路

- `nanobot/agent/loop.py`
  - 消息处理状态机、上下文构建、运行、保存、响应；runtime checkpoint 恢复时按 `tool_call_id` 复用已完成工具结果、跳过重复结果、为 pending 工具生成中断补偿，并基于工具注册元数据识别可安全恢复候选；恢复审查会把只读安全工具、Shell / 写入 / mutating MCP 工具、needs input 工具和安全拦截工具分别归入 `safe_resume`、`review_required`、`needs_input`、`blocked`；安全恢复入口会执行可恢复工具、替换占位 tool result、更新 `recovered_executed` / `recovered_skipped` / `recovered_requires_user` 字段，并发布带 `source: "recovered"` 的 WebUI checkpoint；上下文压缩发生时发布 WebUI 可见的压缩事件，在构建 LLM 上下文前发布记忆来源快照，保存轮次后为明确记忆意图生成待确认的记忆候选。
- `nanobot/agent/runner.py`
  - LLM 推理、工具调用、结果反馈、多轮迭代、checkpoint；checkpoint 会生成稳定 ID、待执行/已执行工具 ID 与可恢复标记，工具执行事件会携带统一风险分类、安全元数据、注册能力元数据、运行 heartbeat 与耗时字段。
- `nanobot/agent/tools/base.py`
  - 工具抽象基类、参数 schema 校验和注册契约。`Tool` 现在提供注册前校验与标准注册元数据，覆盖工具名、描述、对象参数 schema、scope、配置键和并发安全属性。
- `nanobot/agent/tools/registry.py`
  - 工具注册与执行入口。注册时统一执行工具契约校验，并缓存可查询的工具元数据；`get_definitions()` 仍负责生成 provider 使用的 function schema。
- `nanobot/agent/tools/loader.py`
  - 工具自动发现和插件加载入口。内置工具与 entry point 插件最终都通过同一 `ToolRegistry.register()` 注册路径进入标准校验。
- `nanobot/agent/tools/shell.py`
  - Shell 执行工具。负责 timeout、环境变量白名单、workspace guard、allow/deny 规则和危险命令拦截说明。
- `nanobot/utils/progress_events.py`
  - 结构化进度事件工具。为工具 start/running/end/error 事件附加 `checkpoint_id`、`risk_category`、`risk_level`、`safety`、开始时间和耗时元数据。
- `nanobot/session/webui_turns.py`
  - WebUI turn 辅助层。发布运行状态、turn_end、轻量任务 checkpoint、上下文压缩事件、记忆来源快照、Active skills 与记忆候选，供实时界面和历史 transcript 复用；checkpoint 会暴露工具 ID、计数、可恢复标记、复用工具数、补偿工具数、重复跳过数、安全恢复候选数、审查分组计数，以及已恢复执行、已跳过、仍需用户确认的工具计数。
- `nanobot/agent/memory_candidates.py`
  - 长期记忆候选构建与确认写入逻辑。负责保守识别显式记忆意图、过滤敏感内容、按目标文件做完全/近似重复判断，并把确认后的非重复条目追加到对应长期记忆文件；重复结果会返回原因和已有条目预览。
- `nanobot/agent/context.py`
  - 系统提示、Skills、Memory、运行时上下文组装；保留 always skills，并基于当前请求、session summary 和 skill metadata 自动选择工程任务 skill；同时生成 active skills snapshot，记录本轮注入 skill 的名称、来源、命中关键词、优先级和原因；优先使用 `_last_summary.sections` 注入结构化 Archived Context Summary，并通过结构化 summary、当前用户请求和近期历史中的文件路径 / 错误 / 命令信号构造检索查询，再由轻量检索器注入相关历史片段；检索片段会展示来源、安全等级、类别和命中原因；同时提供安全的 memory snapshot 元数据，描述本轮上下文使用了哪些长期记忆来源。
- `nanobot/agent/memory.py`
  - 记忆文件读写、Consolidator / AutoCompact 和压缩事件构建。压缩摘要会解析或推断 `overview`、`goal`、`constraints`、`files_touched`、`commands_run`、`failures`、`decisions`、`next_steps`，并在写入 session metadata、WebUI 事件和轻量检索器前进行敏感信息脱敏与限长；构建 memory snapshot 时会附加检索命中的安全元数据汇总，不包含正文或 snippet。
- `nanobot/agent/retriever.py`
  - 轻量长期记忆检索器。负责索引压缩摘要、持久化 `memory/retriever_index.json`、按查询词和时间权重返回相关历史片段，并附带只读 / 需确认等安全评估、轻量类别和命中原因元数据；tokenize 会保留完整文件路径 token，使 `path:` 命中原因能覆盖当前请求中的文件路径。
- `nanobot/agent/tools/mcp.py`
  - MCP 工具、资源和 prompt wrapper。resource / prompt 默认声明为只读；普通 MCP tool 会基于 annotations、名称、描述和 schema 保守推断只读、并发安全和独占能力；tool / resource / prompt 对 timeout、SDK cancel 和部分 transient connection error 提供明确结果或一次重试。

## 2026-05-22 本轮页面/模块更新

- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P2 Skills 工程化第二阶段：记录 active skills snapshot、WebSocket / transcript / WebUI 事件链路和活动面板可观测卡片。
- `nanobot/agent/skills.py` / `nanobot/agent/context.py`
  - SkillsLoader 新增 `select_task_skill_matches()`；ContextBuilder 新增 `build_active_skills_snapshot()`，输出安全的 skill 选择解释元数据。
- `nanobot/agent/loop.py` / `nanobot/session/webui_turns.py` / `nanobot/channels/websocket.py` / `nanobot/webui/transcript.py`
  - 新增 `active_skills` 事件透传和历史回放，不传输 Skill Markdown 正文。
- `webui/src/lib/types.ts` / `webui/src/hooks/useNanobotStream.ts` / `webui/src/components/thread/AgentActivityCluster.tsx`
  - 前端新增 `UIActiveSkills` 类型、实时流吸收逻辑和 `Active skills` 紧凑展示。
- `tests/agent/test_skills_loader.py` / `tests/agent/test_context_builder.py` / `webui/src/tests/agent-activity-cluster.test.tsx`
  - 新增可解释 skill 匹配、active skills snapshot 和 WebUI 安全展示测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P2 Skills 工程化第一阶段：记录工程任务 skill metadata 匹配、`code-review` 和 `test-fix` 自动选择。
- `nanobot/agent/skills.py` / `nanobot/agent/context.py`
  - SkillsLoader 新增 `select_task_skills()`；ContextBuilder 在默认 coding skill 之外自动注入匹配到的工程任务 skill。
- `nanobot/skills/code-review/SKILL.md` / `nanobot/skills/test-fix/SKILL.md`
  - 新增代码审查和测试修复内置 skill，服务简历中的 AI 编程助手与工程可靠性场景。
- `tests/agent/test_skills_loader.py` / `tests/agent/test_context_builder.py`
  - 新增 skill keyword / priority 匹配和 ContextBuilder 自动注入测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P1 记忆候选近似去重更新：记录 target-scoped duplicate 判断、`duplicate_reason` 和 `existing_preview`。
- `nanobot/agent/memory_candidates.py` / `webui/src/lib/types.ts`
  - 记忆候选确认写入新增近似重复元数据；前端 API 类型补充 duplicate reason 与已有条目预览字段。
- `tests/agent/test_memory_candidates.py`
  - 新增近似重复、不同 target 不误判和不相关内容可写入测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P1 检索相关性更新：记录当前请求、近期历史文件/错误/命令信号参与检索，以及完整路径 token 命中。
- `nanobot/agent/context.py` / `nanobot/agent/retriever.py`
  - ContextBuilder 检索查询新增 current request 和 recent history signals；MemoryRetriever 保留完整路径 token 并优先给出 `path:` 命中原因。
- `tests/agent/test_context_retriever_integration.py` / `tests/agent/test_memory_retriever.py`
  - 新增当前请求触发检索、完整路径命中和 decision 优先分类测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P1 memory snapshot visibility 更新：记录 `retrieved` 元数据汇总和 WebUI 安全展示。
- `nanobot/agent/memory.py` / `webui/src/components/thread/AgentActivityCluster.tsx` / `webui/src/lib/types.ts`
  - Memory snapshot 新增 retrieved metadata：命中条目数、类别计数、命中原因、来源、安全等级和更新时间；前端只展示元数据，不提供正文/snippet 查看入口。
- `tests/agent/test_context_memory_snapshot.py` / `webui/src/tests/agent-activity-cluster.test.tsx`
  - 新增 memory snapshot 检索元数据测试，并断言不泄露检索正文或 snippet。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P1 长期记忆检索更新：记录检索类别、命中原因和 ContextBuilder 注入元数据。
- `nanobot/agent/retriever.py`
  - 新增长期记忆轻量分类与 `match_reason`，检索结果会说明命中 term / section / path。
- `nanobot/agent/context.py`
  - `[Retrieved Memories]` 注入行新增 category / reason 元数据，并让 `build_messages()` 传入 session metadata 支持结构化 summary 检索。
- `tests/agent/test_memory_retriever.py` / `tests/agent/test_context_retriever_integration.py`
  - 新增分类、命中原因和结构化 session summary 检索测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P1 上下文压缩更新：记录压缩摘要脱敏、长日志限长和关键失败信息保留策略。
- `nanobot/agent/memory.py`
  - 新增摘要清洗流程，统一处理 summary text、`summary_sections` 和从归档消息推断出的命令 / 失败条目。
- `tests/agent/test_memory_compaction_event.py`
  - 新增压缩摘要脱敏与限长测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 P0 确认式恢复更新：记录 Shell / 写入 / mutating MCP / needs input / blocked 工具的审查分组，以及 `/resume-safe-tools` 的细分反馈。
- `webui/src/components/thread/AgentActivityCluster.tsx`
  - 任务快照新增 `Recovery review` 详情列表，消费 `recovery_review_items`，展示待恢复工具的分组、原因和建议动作，不展示工具参数。
- `webui/src/tests/agent-activity-cluster.test.tsx`
  - 补充 recovered checkpoint 快照测试，覆盖恢复审查详情列表。
- `nanobot/agent/loop.py`
  - checkpoint 恢复策略表显式化，mutating MCP 名称模式会进入 `review_required`，只读 MCP 仍可作为安全恢复候选。
- `nanobot/command/builtin.py`
  - `/resume-safe-tools` 返回文案区分需审查、需输入和安全拦截的 pending 工具数量。
- `tests/agent/test_loop_save_turn.py` / `tests/command/test_router_dispatchable.py`
  - 新增确认式恢复分组和命令反馈测试。
- `PROJECT_BRIEF.md` / `OPTIMIZATION_ROADMAP.md` / `PAGE_MAP.md` / `DATA_MAP.md` / `CHANGELOG.md`
  - 2026-05-27 文档校准：同步当前代码中已经落地的安全恢复入口、结构化上下文摘要、轻量记忆检索、MCP 能力推断和 MCP 失败分类状态，并重新标注剩余优化缺口。
- `nanobot/agent/runner.py`
  - MCP 错误结果分类进入统一恢复元数据链路，区分 timeout、连接中断、协议错误、权限问题和普通 MCP 工具错误。
- `tests/tools/test_mcp_tool.py` / `tests/agent/test_runner_core.py`
  - 新增 MCP 普通工具能力推断、resource/prompt 注册元数据和 runner MCP 失败分类测试。
- `webui/src/components/thread/AgentActivityCluster.tsx`
  - 工具步骤区新增调度摘要、调度徽标和恢复建议徽标，展示 running / queued 数量、并发上限、批次数、批次序号、队列位置、运行耗时，以及 Retryable / Blocked / Needs input 等失败恢复状态。
- `webui/src/lib/tool-traces.ts`
  - 支持 `queued` 阶段，解析 `batch_id`、`batch_index`、`batch_count`、`batch_size`、`concurrency_limit`、`queue_position` 等调度字段，并保留 `failure_category`、`recovery_action`、`retryable`、`needs_user_input` 恢复字段。
- `nanobot/agent/runner.py`
  - 并发安全工具按 `max_concurrent_tools` 分批执行，执行前发布排队事件，开始、心跳、结束事件保留同一组调度元数据；工具失败时补充恢复分类和建议动作。
- `nanobot/agent/loop.py`
  - runtime checkpoint 恢复 pending 工具时会根据恢复字段记录 `retryable_tool_call_ids` 和 `requires_user_tool_call_ids`，并为恢复占位 tool result 写入更明确的策略说明。
- `nanobot/session/webui_turns.py`
  - WebUI checkpoint 输出新增 retryable / requires-user 工具 ID 和计数，供任务快照展示恢复策略闭环。
- `nanobot/config/schema.py`
  - `agents.defaults.maxConcurrentTools` 暴露工具并发上限配置。
- `nanobot/agent/tools/base.py` / `nanobot/agent/tools/registry.py` / `nanobot/agent/tools/loader.py`
  - 新增工具注册标准化能力：`Tool.validate_registration()` 负责静态契约检查，`Tool.registration_metadata()` 和 registry metadata API 暴露调度与插件可复用的能力描述，loader 的自动发现工具也复用同一注册路径。
- `nanobot/agent/runner.py` / `nanobot/utils/progress_events.py`
  - 工具生命周期事件新增注册能力字段，运行时从 `ToolRegistry.get_metadata()` 读取并透传 `read_only`、`concurrency_safe`、`exclusive`、`config_key`、`scopes`。
- `webui/src/lib/tool-traces.ts` / `webui/src/components/thread/AgentActivityCluster.tsx`
  - 前端解析工具能力字段并在工程活动工具行显示 `Parallel safe`、`Read only`、`Exclusive`、`Config: ...` 标签。
- `OPTIMIZATION_ROADMAP.md`
  - 新增后续优化路线图，作为后续“进行下一轮优化”时的任务来源和验收参考。
- `nanobot/agent/loop.py` / `nanobot/session/webui_turns.py` / `webui/src/components/thread/AgentActivityCluster.tsx`
  - P0 智能恢复执行第一阶段：恢复 checkpoint 时识别 `resumable_tool_call_ids`，WebUI checkpoint 类型和任务快照展示 `Resumable` 状态。
