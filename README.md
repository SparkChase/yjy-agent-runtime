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
- 工具系统：注册表、唯一名称、JSON Schema 参数校验、未知工具、异常结果、结果大小限制，以及 `calculator`、mock `search`、Session-scoped `todo` 和 `echo`。
- Session：消息、工具交互、待办和摘要元数据按 `session_id` 隔离；支持进程内存储和 JSON 文件恢复。
- Context：系统约束、工具定义、最近历史、工具结果预算和增量历史摘要。
- Trace：`session_id`、`run_id`、`trace_id`、轮次、LLM/工具耗时、工具参数与结果摘要、终止原因，支持 JSONL 落盘和递归敏感字段脱敏。
- OpenAI Chat Completions 兼容适配器：从环境变量读取配置，区分网络、服务和响应解析错误。
- CLI 入口：`yjy-agent-runtime`。

当前仍有边界：

- 默认 CLI 使用 JSON 文件 SessionStore，文件目录由 `YJY_SESSION_DIR` 控制；核心层仍提供进程内 Store 供测试和嵌入使用。
- 默认测试不访问真实网络；真实供应商联调需要开发者自行提供配置。
- 适配器使用本项目约定的 JSON 决策协议，不接管供应商原生 Agent 循环。

本次需求验收、真实 API 验证与已复现问题见 [docs/runtime-audit.md](docs/runtime-audit.md)。

## 目录结构

```text
src/yjy_agent_runtime/
├── domain/                 # 领域层：稳定领域对象和不变量
├── agent/                  # 应用层：AgentLoop 和应用端口
├── infrastructure/         # 基建层：以下端口的具体实现
│   ├── context/            #   Context、历史选择和压缩
│   ├── llm/                #   OpenAI-compatible HTTP 适配器
│   ├── session/            #   内存与 JSON 文件 SessionStore
│   ├── tools/              #   工具注册、校验和执行
│   └── trace/              #   结构化事件和脱敏 Recorder
└── interfaces/             # 入口层：CLI、配置加载与对象装配
```

依赖方向保持为：

```text
interfaces -> infrastructure -> agent -> domain
```

领域层不依赖具体模型 SDK、数据库或命令行实现；该方向由
[tests/test_architecture.py](tests/test_architecture.py) 静态校验。逐层职责、Session
如何流转、Context 如何组装见 [docs/architecture-layers.md](docs/architecture-layers.md)。

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
LLM_TIMEOUT_SECONDS=120
LLM_CONNECT_TIMEOUT_SECONDS=10
YJY_SESSION_DIR=.yjy-sessions
YJY_TRACE_DIR=logs
```

真实密钥只能通过本地环境变量或未跟踪配置注入，不能写入代码、日志、测试快照或 Git 历史。

CLI 默认自动读取当前目录的 `.env`，因此把上面的变量写进 `.env` 后可直接运行；
显式环境变量优先于文件。也可用 `--env-file <path>` 指定文件，或 `--no-env-file`
完全关闭加载。

```bash
# .env 中已有配置时，无需 export
YJY_SESSION_DIR=.yjy-sessions uv run yjy-agent-runtime --session-id demo "请计算 6 * 7，并把结果记入 todo"
```

单次运行的轮次上限可通过 `--max-iterations` 设置，默认值为 8。

每次运行都会把脱敏后的运行轨迹写入 JSONL。默认路径为 `logs/<session-id>.jsonl`，
可用 `--trace-file <path>` 指定，或用 `--no-trace` 关闭。CLI 在 stderr 打印实际路径。

```bash
uv run yjy-agent-runtime --session-id demo --trace-file logs/demo.jsonl "列出我的待办"
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

内置工具：

- `calculator`：受限 AST 算术计算，不执行任意代码。
- `search`：固定本地数据的 mock 搜索，不访问网络。
- `todo`：`add`/`list` 当前 Session 的待办，带写入副作用标记。
- `echo`：用于验证调用链路的无副作用回显。

### Memory/Context 召回时机

每次 `RunRequest` 开始时，Runtime 先按 `session_id` 读取当前 Session，再构建本轮 Context。Context 依次放入系统约束和工具定义、较早历史摘要、最近消息和受字符预算限制的工具结果；其他 Session、私有思维链和超长原始结果不会进入 Context。

当历史超过 `max_history_messages` 时，移出最近窗口的连续公开历史会被摘要，摘要覆盖范围记录在 Session 的 `summary.covered_message_count`。后续追问先召回摘要，再召回最近消息；摘要失败不会推进覆盖范围，下一轮可以安全重试。待办不依赖摘要，而是作为当前 Session 的结构化状态由 `todo` 工具读取。

同一窗口继续对话时复用相同 `--session-id`；不同窗口使用不同 ID：

```bash
YJY_SESSION_DIR=.yjy-sessions uv run yjy-agent-runtime --session-id window-1 "记待办：查天气"
YJY_SESSION_DIR=.yjy-sessions uv run yjy-agent-runtime --session-id window-1 "列出我的待办"
YJY_SESSION_DIR=.yjy-sessions uv run yjy-agent-runtime --session-id window-2 "记待办：写周报"
```

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

详细阶段、验收标准和执行记录见 [docs/development-plan.md](docs/development-plan.md)。AI Prompt、问题证据和解决过程见 [docs/ai-prompt-and-problem-solving.md](docs/ai-prompt-and-problem-solving.md)。开发红线和提交规范见 [AGENTS.md](AGENTS.md)。
