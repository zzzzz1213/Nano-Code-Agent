# Nano-Code-Agent

面向工程任务的轻量级 AI Coding Agent。  
它基于 ReAct 式 Agent loop、可插拔工具系统、结构化上下文压缩、长期记忆与 WebUI 工作台，专注解决“让模型在真实代码仓库里持续完成任务”这件事。

> 这个项目不是一个只会聊天的通用机器人，而是一个更偏工程执行的 AI 助手：读代码、调用工具、修改文件、运行检查、恢复中断、总结结果。

## 项目定位

`Nano-Code-Agent` 是在 `nanobot` 基础上收束而来的 AI 编程助手版本，目标是把大模型常见的几个工程痛点做成可运行的闭环：

- 上下文窗口有限，长任务容易遗忘关键信息
- 工具调用不稳定，中断后容易重复执行或失去状态
- 工程任务往往需要多工具协作，而不只是单轮问答
- 用户偏好、项目事实、历史决策很难在跨轮次任务里持续复用

当前仓库已经把这些能力落到了一个可以本地运行、继续扩展的框架里。

## 核心能力

### 1. ReAct 式工程执行闭环

- 基于 `AgentLoop + AgentRunner` 执行多轮推理
- 支持 `LLM 推理 -> 工具调用 -> 结果反馈 -> 继续推理`
- 支持多工具协作、异步工具事件和阶段化任务状态
- 支持运行时 checkpoint，任务中断后可恢复上下文

### 2. 可插拔工具系统

- 工具通过统一 `Tool` 基类和 JSON Schema 暴露给模型
- 支持文件、Shell、Web、MCP、图片、子 Agent 等工具类型
- 支持工具注册元数据：`read_only`、`concurrency_safe`、`exclusive`、`scopes`
- 内置安全分级与 Shell 危险命令拦截

### 3. 智能恢复与故障诊断

- checkpoint 会记录 pending / completed / executed tool calls
- 恢复阶段会把工具分成 `safe_resume`、`review_required`、`needs_input`、`blocked`
- WebUI 可直接进行恢复审查、确认重试、补充输入
- MCP / 插件错误支持统一诊断标签、提示和下一步建议

### 4. 上下文压缩与长期记忆

- 自动压缩长会话，优先保留目标、约束、失败、文件、命令、决策、下一步
- 对摘要和日志做脱敏，避免敏感信息污染长期上下文
- 支持轻量长期记忆检索，按路径、失败、命令、决策等信号召回历史
- 支持长期记忆候选确认写入、相似合并、冲突审查

### 5. 面向工程任务的 Skills

- 默认内置 `coding-assistant`
- 已支持自动选择的工程 skill：
  - `code-review`
  - `test-fix`
  - `frontend-implementation`
  - `migration-planning`
  - `dependency-upgrade`
  - `docs-sync`
- 支持 skill 优先级、关键词匹配和冲突过滤

### 6. 可观测的 WebUI 工作台

- React + TypeScript + Vite WebUI
- 可视化展示工具步骤、文件变更、检查结果、恢复审查、上下文压缩、记忆快照、Active skills
- 已内置轻量工作台卡片：
  - `Task plan`
  - `Check results`
  - `Turn summary`
  - `Diff preview`

## 适合什么场景

- 在本地代码仓库中执行真实开发任务
- 需要“读代码 -> 改代码 -> 跑检查 -> 总结结果”的 AI 助手
- 想研究 Agent loop、工具调用、MCP、上下文工程、长期记忆落地方式
- 想基于现有框架继续扩展 channel、provider、tool 或 WebUI

## 架构概览

```text
Channel / WebUI
      |
      v
 MessageBus
      |
      v
 AgentLoop  -----> ContextBuilder -----> Session / Memory / Skills
      |
      v
 AgentRunner ----> LLM Provider
      |
      v
 Tool Registry ---> File / Shell / Web / MCP / Subagent / ...
      |
      v
 Progress Events / Checkpoint / Transcript / WebUI Workbench
```

核心模块：

- `nanobot/agent/loop.py`：任务主状态机、checkpoint 恢复、保存轮次
- `nanobot/agent/runner.py`：ReAct 工具循环、工具执行、多轮推理
- `nanobot/agent/context.py`：系统提示、Skills、Memory、历史上下文组装
- `nanobot/agent/memory.py`：会话压缩、结构化摘要、长期上下文支撑
- `nanobot/agent/retriever.py`：轻量长期记忆检索
- `nanobot/agent/tools/*`：工具实现与注册体系
- `webui/`：工程化聊天界面与任务工作台

## 技术栈

- 后端：Python 3.11+, asyncio, Typer, Pydantic
- Agent：ReAct loop, Tool Registry, Skills, Memory Retriever
- 工具协议：MCP
- 前端：React, TypeScript, Vite
- 运行形态：CLI + WebSocket Gateway + WebUI

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/zzzzz1213/Nano-Code-Agent.git
cd Nano-Code-Agent
```

### 2. 安装 Python 依赖

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. 启动 CLI

```bash
nanobot --help
```

项目使用 `Typer` CLI，入口在：

- `nanobot/cli/commands.py`

### 4. 启动 WebUI 开发环境

```bash
cd webui
npm install
npm run dev
```

常用前端命令：

```bash
npm run test
npm run build
```

### 5. 运行后端检查

```bash
pytest -q
ruff check nanobot tests
```

## 目录结构

```text
nanobot/
  agent/          # Agent loop、runner、context、memory、retriever
  channels/       # WebSocket 与多通道接入
  providers/      # 各类 LLM provider
  session/        # 会话与 WebUI turn 辅助层
  skills/         # 内置 skills
  webui/          # transcript / web bundle 入口

webui/
  src/            # React 前端源码

tests/            # Python / WebUI 对应测试
docs/             # 补充文档
```

## 当前已经落地的亮点

- 已具备恢复确认流，而不是只有“失败后重新来过”
- 已具备 MCP / 插件诊断增强，而不是只有裸异常
- 已具备结构化上下文压缩，而不是简单截断历史
- 已具备长期记忆检索、近似去重、相似合并
- 已具备工程 skill 自动选择与冲突过滤
- 已具备面向工程任务的 WebUI 工作台

这意味着它已经不仅是“能调用几个工具”，而是开始具备工程助手需要的持续执行能力。

## 文档索引

- [PROJECT_BRIEF.md](./PROJECT_BRIEF.md)：项目定位、当前能力与阶段总结
- [OPTIMIZATION_ROADMAP.md](./OPTIMIZATION_ROADMAP.md)：后续优化路线图
- [PAGE_MAP.md](./PAGE_MAP.md)：页面与模块地图
- [DATA_MAP.md](./DATA_MAP.md)：关键数据结构与数据流
- [CHANGELOG.md](./CHANGELOG.md)：变更记录

## 许可证

本项目采用 [MIT License](./LICENSE)。
