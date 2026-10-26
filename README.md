# YJY Agent Runtime

YJY Agent Runtime 是一个从零实现的最小可用 Agent Runtime 教学项目。项目不借助现成 Agent 框架接管核心流程，而是自行实现领域模型、Agent Loop、工具调用、Session、Context 和 Trace。

> 本文档以当前 `main` 分支代码为准。尚未实现的能力统一标记为“规划中”，不作为当前可用能力宣传。

## 当前实现状态

当前版本已经具备：

- 不依赖模型厂商或存储实现的领域对象。
- 显式、线性的 Agent Loop。
- 最终回答和一个或多个工具调用两类结构化决策。
- 最大循环轮次与明确的终止原因。
- Context、LLM、工具执行和 Trace 的应用端口。
- 使用 Fake 与 Stub 的确定性单元测试，不需要 API Key 或网络。
- LLM 环境变量示例，但尚未接入真实 LLM 适配器。

当前版本尚未具备：

- 工具注册表、工具 Schema 校验和内置工具实现。
- Session 存储、恢复与跨运行连续追问。
- Context 预算、历史压缩和 Memory。
- 可用于生产的 Trace 实现。
- 真实 LLM 适配器和可交互 CLI。

因此，当前 `uv run yjy-agent-runtime` 仍只是项目入口占位，不代表完整 Runtime 已可运行。

## 分层架构

项目采用领域层、应用层和基础设施层的边界：

```text
外部入口
   │
   ▼
基础设施层
真实 LLM、工具、存储、Context、Trace、CLI 的具体实现
   │ 实现应用端口
   ▼
应用层
Agent Loop 与用例编排
   │ 使用领域对象
   ▼
领域层
稳定概念、状态、不变量和终止语义
```

依赖方向必须保持为：

```text
基础设施层 ──► 应用层 ──► 领域层
```

领域层不依赖应用层或基础设施层；应用层不直接依赖具体模型 SDK、数据库或命令行实现。

### 领域层

位置：`src/yjy_agent_runtime/domain/`

领域层当前包含：

- `MessageRole`、`Message`
- `ToolCall`、`ToolResult`、`ToolResultStatus`
- `FinalAnswerDecision`、`ToolCallDecision`、`AgentDecision`
- `RunRequest`、`RunResult`
- `TerminationReason`

这些对象负责表达 Runtime 的稳定事实，并在构造阶段拒绝空消息、非法工具结果、重复调用 ID 等无效状态。领域层不调用 LLM、不执行工具，也不访问存储。

### 应用层

位置：`src/yjy_agent_runtime/agent/`

应用层当前包含：

- `AgentLoop`：编排 Context、LLM 决策、工具执行和终止。
- `LoopConfig`：定义最大循环轮次。
- `ContextProvider`、`LLMClient`、`ToolExecutor`、`TraceRecorder`：声明循环所需的抽象端口。

应用层只说明一次 Agent 运行如何推进，以及需要哪些外部能力，不决定这些能力如何实现。

### 基础设施层

基础设施层负责实现应用端口，包括真实 LLM 客户端、工具注册表、SessionStore、ContextBuilder、TraceRecorder 和 CLI。

当前仓库尚未提交正式的基础设施实现。测试中的 Fake、Stub 和内存记录器只用于验证应用层行为，不属于可交付的运行时基础设施。

后续代码应集中放入 `src/yjy_agent_runtime/infrastructure/`，并按照能力继续细分，例如：

```text
infrastructure/
├── llm/
├── tools/
├── session/
├── context/
└── trace/
```

入口适配代码放入 `src/yjy_agent_runtime/interfaces/`，例如 CLI。只有实际实现出现时才创建对应目录，避免用空目录表示进度。

## 当前 Agent Loop

当前循环真实执行以下步骤：

```text
接收 RunRequest
      │
      ▼
由 ContextProvider 构建消息
      │
      ▼
由 LLMClient 返回结构化决策
      │
      ├── FinalAnswerDecision ──► 返回 RunResult
      │
      └── ToolCallDecision
              │
              ▼
       ToolExecutor 逐个执行工具
              │
              ▼
       将 ToolResult 交给下一轮 Context
```

循环达到 `max_iterations` 时以 `MAX_ITERATIONS` 终止。工具执行会显式接收 `session_id`。当前循环不会持久化消息或工具结果，也尚未统一转换 LLM、Context 和工具端口抛出的异常。

## 目录结构

```text
.
├── AGENTS.md
├── README.md
├── docs/
│   └── development-plan.md
├── src/yjy_agent_runtime/
│   ├── __init__.py
│   ├── agent/
│   │   ├── loop.py
│   │   └── ports.py
│   └── domain/
│       └── models.py
└── tests/
    ├── test_agent_loop.py
    ├── test_domain_models.py
    └── test_smoke.py
```

`docs/development-plan.md` 描述目标架构和后续阶段；本 README 描述当前代码已经具备的真实能力。

## 环境与配置

- Python 3.12 或更高版本
- uv

安装依赖：

```bash
uv sync --locked
```

真实 LLM 尚未接入，但配置占位已记录在 `.env.example`：

```dotenv
LLM_BASE_URL=https://your-llm-provider.example/v1
LLM_MODEL=your-model-name
LLM_API_KEY=your-api-key
```

`.env` 已被 Git 忽略，真实密钥不得写入代码、日志、测试或 Git 历史。

## 验证

当前代码可执行以下检查：

```bash
uv run ruff check .
uv run python scripts/verify_md_links.py
uv run python scripts/verify_doc_budgets.py
uv run pytest
```

当前测试覆盖领域模型约束、直接回答、单次及多次工具调用、最大轮次终止，以及工具执行显式携带 `session_id`。真实 LLM 集成测试尚不存在。

## 后续开发顺序

1. 修正并稳定应用端口的类型契约与异常语义。
2. 建立 `infrastructure/`，实现工具注册、参数校验和基础工具。
3. 实现 SessionStore，并验证不同 Session 隔离及同一 Session 连续追问。
4. 实现 Context 预算、工具结果限制和历史压缩。
5. 实现结构化 Trace 与敏感字段脱敏。
6. 接入真实 LLM 适配器和 CLI，最后进行端到端验收。

更细的任务、分层职责和验收标准见 `docs/development-plan.md`；开发红线和提交规范见 `AGENTS.md`。
