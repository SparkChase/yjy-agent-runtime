# YJY Agent Runtime

YJY Agent Runtime 是一个从零实现的最小 Agent Runtime 教学项目。核心 Agent Loop、工具调度、Session、Context 压缩、结构化 Trace 和 LLM 响应解析都由本项目自行实现，不依赖现成 Agent 编排框架。

## 当前状态

当前版本已经可以运行完整的本地闭环：

```text
用户输入
  -> ContextBuilder
  -> OpenAI-compatible LLMClient
  -> FinalAnswer 或 ToolCall
  -> ToolRegistry / RegistryToolExecutor
  -> SessionStore 和下一轮 Context
  -> 最终回答
```

已具备的能力：

- 领域模型和构造期约束：消息、工具调用、工具结果、Session、决策和终止原因。
- 显式线性 Agent Loop：直接回答、多工具调用、最大轮次、Context/LLM/工具错误和非法决策终止。
- 工具系统：注册表、唯一名称、JSON Schema 参数校验、未知工具、异常结果、结果大小限制和内置 `echo` 工具。
- 进程内 Session：消息、工具交互和摘要元数据按 `session_id` 隔离。
- Context：系统约束、工具定义、最近历史、工具结果预算和增量历史摘要。
- Trace：`session_id`、`run_id`、`trace_id`、轮次、LLM/工具耗时、终止原因和递归敏感字段脱敏。
- OpenAI Chat Completions 兼容适配器：从环境变量读取配置，区分网络、服务和响应解析错误。
- CLI 入口：`yjy-agent-runtime`。

当前仍有边界：

- Session 只保存在进程内，重启后不会恢复。
- 默认测试不访问真实网络；真实供应商联调需要开发者自行提供配置。
- 适配器使用本项目约定的 JSON 决策协议，不接管供应商原生 Agent 循环。

## 目录结构

```text
src/yjy_agent_runtime/
├── domain/                 # 稳定领域对象和不变量
├── agent/                  # AgentLoop 和应用端口
├── infrastructure/
│   ├── context/            # Context、历史选择和压缩
│   ├── llm/                # OpenAI-compatible HTTP 适配器
│   ├── session/            # 进程内 SessionStore
│   ├── tools/              # 工具注册、校验和执行
│   └── trace/              # 结构化事件和脱敏 Recorder
└── interfaces/             # CLI 入口
```

依赖方向保持为：

```text
基础设施层 -> 应用层 -> 领域层
```

领域层不依赖具体模型 SDK、数据库或命令行实现。

## 安装

环境要求：Python 3.12+ 和 uv。

```bash
uv sync --locked
```

## 配置与运行

适配器读取以下环境变量：

```dotenv
LLM_BASE_URL=https://your-llm-provider.example/v1
LLM_MODEL=your-model-name
LLM_API_KEY=your-api-key
LLM_TIMEOUT_SECONDS=30
```

真实密钥只能通过本地环境变量或未跟踪配置注入，不能写入代码、日志、测试快照或 Git 历史。

运行一次对话：

```bash
export LLM_BASE_URL="https://your-llm-provider.example/v1"
export LLM_MODEL="your-model-name"
export LLM_API_KEY="your-api-key"
uv run yjy-agent-runtime --session-id demo "请调用 echo 工具回显 hello，然后总结结果"
```

也可以省略消息参数，让 CLI 从 stdin 读取：

```bash
printf '%s\n' "你好" | uv run yjy-agent-runtime
```

LLM 返回的内容必须是以下两种 JSON 之一：

```json
{"type":"final_answer","answer":"..."}
```

```json
{"type":"tool_calls","tool_calls":[{"name":"echo","arguments":{"text":"hello"}}]}
```

工具调用后 Runtime 会把结构化工具结果保存到当前 Session，并继续下一轮，直到得到最终回答或达到最大轮次。

## 验证

默认检查不需要 API Key 或网络：

```bash
uv run ruff check .
uv run python scripts/verify_md_links.py
uv run python scripts/verify_doc_budgets.py
uv run pytest -q
```

当前默认测试包含领域约束、Agent Loop 正常和异常路径、工具 Schema、未知工具、Session 隔离、Context 预算和增量压缩、Trace 脱敏、LLM 适配器错误分类，以及一个使用 `httpx.MockTransport` 的完整闭环测试。

真实 LLM 测试被标记为 `integration`，没有配置时会跳过：

```bash
uv run pytest -m integration -q
```

因此，Mock 闭环通过不等于已经完成真实供应商联调；真实联调必须在配置有效的环境中单独执行并记录结果。

## 开发计划

详细阶段、验收标准和未完成事项见 [docs/development-plan.md](docs/development-plan.md)。开发红线和提交规范见 [AGENTS.md](AGENTS.md)。
