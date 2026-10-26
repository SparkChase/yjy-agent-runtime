# YJY Agent Runtime

YJY Agent Runtime 是一个从零实现的最小可用 Agent Runtime 教学项目。它不使用现成 Agent 框架承接主流程，而是自行实现 Agent Loop、工具调用、Session 隔离、Context 管理和执行追踪。

> 当前仓库处于项目初始化阶段。本文档先定义目标架构、运行约定和验收范围；具体命令会随着实现同步更新，不将尚未完成的能力描述为可用。

## 项目目标

本项目对应的 Vibe Coding 题目是“从零实现一个最小可用 Agent”，目标包括：

- 接收用户输入。
- 由 LLM 自主决定直接回复还是调用工具。
- 根据工具的名称、描述和参数 Schema 生成调用。
- 执行工具并把结果交还给 LLM。
- 根据最新状态继续循环或返回最终答案。
- 为不同窗口维护彼此隔离且可以恢复的 session。
- 在长对话中保留必要状态，并进行基础 context 压缩。
- 提供异常处理、结构化 trace 和自动化测试。

## 设计原则

1. 核心 Runtime 自研，不用 LangGraph、OpenHands、OpenClaw、Pi 等框架替代主循环。
2. 从清晰、可测的最小实现开始，避免过早设计复杂平台。
3. LLM 提供决策，程序负责解析、校验、执行和约束。
4. 不保存或展示模型私有思维链，只记录可公开的决策摘要与执行事件。
5. Session、Context、Tool 和 LLM 通过明确协议解耦。
6. 所有真实结果可追踪，所有 Mock 行为明确标注。

完整开发红线与协作规范见 ./AGENTS.md。

## Agent Loop

计划中的最小循环如下：

```text
用户输入
   │
   ▼
加载当前 Session
   │
   ▼
组装 Context
   │
   ▼
调用真实 LLM
   │
   ▼
解析结构化输出
   ├── final_answer ───────────────► 返回用户
   │
   └── tool_call
           │
           ▼
     Schema 校验与工具执行
           │
           ▼
     记录 Tool Result 与 Trace
           │
           └───────────────────────► 下一轮
```

Runtime 会设置最大循环轮次。达到上限、解析失败或依赖持续异常时，以明确终止原因安全结束，而不是无限循环。

## 工具系统

每个工具至少包含：

- `name`：供 LLM 选择的稳定名称
- `description`：说明适用场景和行为边界
- `parameters`：JSON Schema 风格的参数定义
- `execute`：实际执行入口

首批计划实现：

- `calculator`：执行受控数学计算
- `search`：提供可替换的 Mock 搜索
- `todo`：在当前 session 中记录和读取待办
- 可选扩展：`weather` 或 `read_docs`

注册表负责检查工具名唯一性、向 LLM 暴露 Schema、校验调用参数，并把调用分发给对应实现。

## LLM 输出协议

LLM 输出需要能够解析为两类决策：

```json
{
  "type": "tool_call",
  "decision_summary": "需要先计算表达式",
  "tool": {
    "name": "calculator",
    "arguments": {
      "expression": "18 * 7"
    }
  }
}
```

```json
{
  "type": "final_answer",
  "decision_summary": "已有足够信息回答",
  "answer": "计算结果是 126。"
}
```

`decision_summary` 是可审计的简短决策说明，不要求也不保存模型完整思维链。解析器必须处理非法 JSON、未知类型、未知工具和参数不符合 Schema 等情况。

## Session 管理

`session_id` 是状态隔离的基本边界。例如：

- 用户 A 的窗口 1 使用 `session-a-window-1`
- 用户 A 的窗口 2 使用 `session-a-window-2`

两个窗口分别保存消息、工具结果、摘要和待办事项，因此可以独立追问与恢复，彼此不会污染。

最小版本可以先使用进程内存储或本地文件存储，但业务接口不会绑定具体存储方式。后续可以替换成 SQLite 或其他持久化实现。

## Context 与 Memory

### 召回时机

每次 Agent Loop 调用 LLM 前，从当前 session 召回上下文。召回只发生在明确的 `session_id` 内，并优先选择：

1. 系统指令与工具定义。
2. 当前用户输入。
3. 最近若干轮用户消息与最终答复。
4. 与当前追问有关的近期工具调用及结果。
5. 更早历史形成的压缩摘要。

### 放置方式

模型上下文计划按以下顺序组织：

```text
系统约束
工具 Schema
历史摘要
最近对话
相关工具结果
当前用户输入
```

工具原始结果会先做长度限制或摘要，避免把无关的大段输出持续塞回模型。模型私有思维链不进入 session；只保存结构化决策、工具调用、工具结果和最终回答。

### 基础压缩

当消息数量或估算 Token 超过预算时：

1. 保留最近 N 条消息。
2. 将更早的对话压缩为一条结构化摘要。
3. 记录摘要覆盖的消息范围。
4. 后续继续在该摘要基础上增量压缩。

摘要应保留用户目标、已经确认的事实、未完成事项、关键工具结果和约束，不保留寒暄、重复内容及无关日志。

## Trace 与日志

每次运行计划记录以下结构化事件：

- `session_id`
- `trace_id`
- `loop_index`
- LLM 请求开始、结束、耗时和错误
- 解析后的决策类型
- 工具名称、脱敏参数、结果摘要、耗时和错误
- 最终终止原因

日志不记录 API Key，也不记录完整私有思维链。

## 计划目录

```text
.
├── AGENTS.md
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── uv.lock
├── scripts/
├── src/
│   └── yjy_agent_runtime/
│       ├── agent/
│       ├── context/
│       ├── llm/
│       ├── session/
│       ├── tools/
│       └── trace/
└── tests/
```

目录会在功能实现时逐步创建。

## 环境要求

- Python 3.12 或更高版本
- uv
- 一个兼容项目 LLM 适配器的真实 API Key

## 安装与运行

统一使用 uv：

```bash
uv sync
uv run yjy-agent-runtime
```

环境变量通过本地 `.env` 或 Shell 注入，可先从示例文件复制：

```bash
cp .env.example .env
```

```dotenv
LLM_BASE_URL=https://your-llm-provider.example/v1
LLM_MODEL=your-model-name
LLM_API_KEY=your-api-key
```

`.env` 已被 Git 忽略，仅提交不包含真实密钥的 `.env.example`。变量分别用于配置兼容接口地址、模型名称和 API Key。

## 测试

普通测试使用 Fake LLM 和可预测工具结果，不要求联网：

```bash
uv run pytest
```

真实 LLM 集成测试将单独标记，并在显式提供 API Key 时运行：

```bash
uv run pytest -m integration
```

计划覆盖：

- 直接回复
- 一次及多次工具调用
- 工具参数校验
- 工具异常与未知工具
- LLM 输出解析失败
- 最大循环轮次
- 不同 session 的状态隔离
- 历史压缩后的纯对话追问
- 历史压缩后的工具追问

## 仓库门禁与持续集成

本地提交前与 CI（GitHub Actions，push 与 pull request 触发）执行同一组检查：

```bash
uv sync --locked                              # 按 uv.lock 复现环境
uv run ruff check .                           # 静态检查
uv run python scripts/verify_md_links.py      # Markdown 本地链接检查
uv run python scripts/verify_doc_budgets.py   # 常驻文档词数预算
uv run pytest                                 # 单元测试
```

词数上限登记在 `scripts/doc-budgets.manifest.json`，处置规则见 `scripts/verify_doc_budgets.py`。

## 迭代路线

### Phase 1：项目骨架

- 使用 uv 初始化项目
- 定义领域模型与协议
- 接入测试、格式化与类型检查

### Phase 2：最小 Agent Loop

- 接入一个真实 LLM API
- 实现结构化输出解析
- 实现循环、终止条件与异常处理

### Phase 3：工具系统

- 实现工具注册机制
- 完成 calculator、mock search、todo
- 增加工具 Trace 与测试

### Phase 4：Session 与 Context

- 实现 session 隔离和恢复
- 支持连续追问
- 实现基础 context 压缩

### Phase 5：验收与记录

- 完成端到端测试
- 补充 AI Prompt 与问题解决记录
- 用真实 LLM 完成演示
- 整理 GitHub 仓库与提交说明

## AI Prompt 与问题解决记录

后续会单独记录：

- 使用过的关键 Prompt
- 重要设计分歧及选择理由
- LLM 输出协议的调试过程
- 工具 Schema 与解析错误案例
- Session 串线和 Context 过长等问题的复现与解决
- AI 辅助生成内容经过了哪些人工核验

该记录用于说明开发过程，不替代代码、测试和真实执行证据。

## 当前状态

- [x] 建立项目开发红线与协作规范
- [x] 编写初始 README 和系统设计
- [x] 使用 uv 初始化 Python 项目
- [x] 接入静态检查、文档门禁与持续集成
- [ ] 实现最小 Agent Loop
- [ ] 实现工具注册与三个基础工具
- [ ] 实现 Session 和 Context 管理
- [ ] 增加 Trace、异常处理和测试
- [ ] 接入真实 LLM 并完成演示
