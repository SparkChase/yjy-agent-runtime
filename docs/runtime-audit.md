# Agent Runtime 需求验收与问题记录

检查日期：2026-10-26。

> 后续状态（同日）：P1-01 有界压缩与 P1-02 工具结果元数据已在 `e57a872` 修复，
> `search` 多关键词匹配在 `aa9730d` 修复。当前离线门禁为 `123 passed, 2 deselected`，
> 真实集成测试 `2 passed`。本文其余内容保留为修复前的问题快照，不再代表当前状态。
>
> **2026-10-27 补记**：本文列出的四个遗留问题已全部修复并提交，证据见
> [runtime-remediation-plan.md](runtime-remediation-plan.md) 第 9 节。
> - P1-03 副作用工具超时后仍写入 → `8a747dd`（按副作用分流 + `timeout_semantics`）。
> - P2-01 工具交互两次写入 → `661564c`（一次快照写入调用/结果/工具消息）。
> - P2-02 `calculator` 无资源上限 → `f22ad98`（长度/嵌套/指数/结果四道闸门）。
> - P1-06 集成测试未驱动闭环 → `692266f`（真实 LLM 三场景，4 passed）。
>
> 当前门禁：离线 `150 passed, 4 deselected`，真实集成 `4 passed in 223.76s`。
> 下面"问题与处理建议"中对应的复现记录保留为历史证据，不再代表当前行为。

## 检查结论

主循环、工具注册与调度、LLM 决策解析、Session 和 Context 都由项目自行实现，生产依赖只有 `httpx` 与 `jsonschema`，符合“从零实现”的要求。`calculator`、mock `search`、`todo` 已具备，另有 `echo`。

目前不能认定题目已全部验收完成：基础压缩没有真正减少历史内容，真实工具闭环缺少自动化测试，CLI 的配置加载和日志可见性也有缺口。本次检查实际调用了本地 `.env` 配置的真实 LLM；默认超时配置下，任务在执行 `search` 后因第二次 LLM 请求超时而终止。

本记录基于检查起点 `7d13d36` 及当时工作区中的实现。检查开始时已有 `.gitignore`、LLM 适配器、CLI、真实 LLM 测试和适配器测试共五个文件的未提交修改。它们不属于本次问题记录的改动。

## 题目要求对照

| 要求 | 当前判断 | 依据与边界 |
| --- | --- | --- |
| 从零实现核心 Runtime | 满足 | 显式 `AgentLoop` 和自有端口、领域模型；未引入 Agent 框架 |
| 输入、决策、工具执行、继续或回答 | 已实现 | 离线闭环通过；真实默认 CLI 任务遇到 LLM 超时 |
| 至少三个工具 | 满足 | `calculator`、mock `search`、`todo`，另有 `echo` |
| 名称、描述、参数 Schema 与注册机制 | 满足 | `ToolRegistry`；工具定义进入系统消息；执行前使用 JSON Schema 校验 |
| 解析工具调用或最终回答 | 满足 | 自定义 JSON 决策协议；可解析公开 `decision_summary`，不保存私有思维链 |
| Session 隔离与继续聊天 | 已实现，有入口约束 | JSON 文件按 Session ID 隔离；CLI 必须为不同窗口传入不同 ID |
| 最大循环轮次 | 满足 | 默认 8，可通过 CLI 设置；有边界测试 |
| 纯对话和工具追问 | 部分验证 | 历史恢复、工具状态有离线测试；真实模型跨 CLI 追问缺少自动化验收 |
| Context 基础压缩 | 未满足实际缩减要求 | 默认摘要器保留并拼接全部旧消息；摘要没有长度预算 |
| 基本异常处理 | 已实现，有超时副作用问题 | 网络、解析、参数、未知工具与工具异常有处理；超时线程仍可继续执行 |
| 工具 Trace / 执行日志 | 部分满足 | 核心有结构化事件；CLI 成功日志不展示、不落盘，事件缺少参数与结果摘要 |
| 使用真实 LLM API | 已连通 | 显式注入 `.env` 后，已有单次决策集成测试通过 |
| 测试构建 | 仍需补齐验收测试 | 77 个离线测试通过；真实集成测试没有驱动 `AgentLoop` |
| README 与代码链接 | 已有基础 | README 有运行与 Memory 说明，配置示例需补充；已配置 GitHub origin，远端内容未在本次检查中验证 |

## 已执行的验证

| 检查 | 真实执行结果 |
| --- | --- |
| `uv run --locked pytest -m 'not integration' -q` | `77 passed, 1 deselected` |
| `uv run --locked ruff check .` | 通过 |
| `uv run --locked python scripts/verify_md_links.py` | 通过 |
| `uv run --locked python scripts/verify_doc_budgets.py` | 通过 |
| `uv run --locked pytest -m integration -q -rs --tb=short`，未加载 `.env` | `1 skipped, 77 deselected`，提示真实 LLM 配置不可用 |
| 未加载 `.env` 的 CLI，合成输入“你好” | 退出码 1，`LLMConfigurationError: LLM_BASE_URL must not be empty` |
| 通过 `uv run --locked --env-file .env` 注入配置后，运行现有集成测试 | `1 passed, 77 deselected`，耗时 45.72 秒 |
| `uv run --locked pytest --collect-only -q` | 收集 78 项，包含真实 LLM 集成测试 |

真实 CLI 验证使用独立临时 Session 目录和合成任务：搜索 Session 说明、计算 `137 * 29`、把结果记入待办。为观察调用过程，临时保留了 CLI 内部的 TraceRecorder；核心循环、LLM 适配器、工具执行器和 JSON SessionStore 均使用当前项目实现。

默认 LLM 超时配置下的记录：

```text
第 1 轮：真实 LLM 返回 search 调用
         search 执行成功，返回明确标注的 mock 搜索结果
第 2 轮：LLMNetworkError: LLM request timed out
终止原因：llm_error
CLI 退出码：1
总耗时：60.26 秒
calculator / todo：没有执行
```

显式设置 `LLM_TIMEOUT_SECONDS=120` 的首次验证已在临时 Session 中观察到 `search`、`calculator`、`todo` 三次成功执行，待办为 `137 * 29 = 3973`。该次观察进程中断，没有取得最终回答或运行终止状态，不把它计为完整闭环通过。随后重新验证计算工具闭环和已保存 Session 的工具追问，结果待补充。

## 问题与处理建议

### P1-01：默认摘要器没有真正压缩 Context

位置：`src/yjy_agent_runtime/infrastructure/context/builder.py:47-59`、`:97-111`。

`ExtractiveHistorySummarizer.summarize()` 把旧摘要与每条消息的完整内容拼接起来。移出最近窗口的历史仍完整进入下一轮模型输入，长工具结果进入摘要后还会绕过最近消息的工具结果截断。既没有摘要长度上限，也没有模型输入的总字符或 Token 预算。

本次临时验证将最近窗口设为 2 条，每条用户消息约 1,000 字符：

| 历史消息数 | Context 总字符数 | 摘要字符数 | 模型输入消息条数 |
| --- | --- | --- | --- |
| 10 | 10,094 | 8,055 | 4 |
| 100 | 100,814 | 98,773 | 4 |

消息条数下降并不代表内容被压缩。建议为摘要和总 Context 设置明确预算，实现有长度约束的基础摘要，并测试压缩后对早期事实的追问。

### P1-02：默认真实 CLI 工具闭环在当前服务上超时

位置：`src/yjy_agent_runtime/infrastructure/llm/openai_compatible.py:45`、`:62`；`tests/integration/test_llm_integration.py:21-25`。

本地 `.env` 当前只配置了地址、模型和密钥，CLI 因而使用默认 30 秒超时。已有集成测试强制使用至少 120 秒，单次决策通过不能说明默认 CLI 同样能够完成任务。本次默认 CLI 在第二次真实 LLM 请求处超时，已经成功的搜索结果仍保留在 Session 中。

建议明确文档中的超时配置，并让真实端到端验收使用与实际 CLI 一致的配置。提高超时后的成功只能证明该配置下的能力，不能覆盖默认配置失败的记录。

### P1-03：真实集成测试没有验证 Agent 闭环

位置：`tests/integration/test_llm_integration.py:15-33`；`tests/test_end_to_end.py:63-111`。

真实测试仅调用 `client.decide()`，并断言 `decision is not None`；没有实例化 `AgentLoop`、注册工具、检查工具结果或待办，也没有校验回复内容。完整闭环测试使用 `httpx.MockTransport` 或预设决策，属于正确的离线测试，但不能替代真实模型按 Schema 自主调用工具的验收。

建议增加独立的真实端到端测试，至少断言：真实工具调用发生、工具执行成功、结果写入目标 Session、模型再次读取结果并返回最终答案；另覆盖同 Session 追问、另一 Session 的隔离，以及压缩后追问。

### P2-01：本地 `.env` 没有自动加载

位置：`src/yjy_agent_runtime/infrastructure/llm/openai_compatible.py:58-70`、`src/yjy_agent_runtime/interfaces/cli.py:43`；`README.md:144-148`。

配置只来自 `os.getenv()`，项目没有 dotenv 加载入口。当前 `.env` 中的三项配置非空，但普通 `uv run` 启动的进程中三项均未设置。因此，直接启动 CLI 会报配置错误，按 README 的集成测试命令会跳过。`.env` 已被忽略且未被 Git 跟踪。

建议明确使用 `uv run --env-file .env ...`，或实现并测试约定的本地配置加载入口。README 现有 `export` 示例可正常注入环境变量，但仅创建 `.env` 文件不会自动生效。

### P2-02：integration 标记没有排除默认真实网络调用

位置：`pyproject.toml:24-27`；`tests/integration/test_llm_integration.py:14-18`。

pytest 的标记只用于分类，不能自动跳过测试。默认收集包含真实测试；一旦进程已设置三项 LLM 配置，`uv run pytest -q` 就会执行真实请求。这与“默认测试不访问真实网络”的文档表述不一致。

建议让离线测试入口显式排除 integration，并保留单独的真实测试入口；对配置已存在的默认执行路径也做验证。

### P2-03：CLI 成功调用的 Trace 不可见，事件信息不完整

位置：`src/yjy_agent_runtime/interfaces/cli.py:51`、`:70-95`；`src/yjy_agent_runtime/agent/loop.py:177-185`、`:216-227`。

CLI 创建的 TraceRecorder 只保存在内存中，成功时仅输出最终答案，进程结束后事件丢失；失败时也只打印最近依赖错误。核心工具事件记录了名称、调用 ID、状态和耗时，没有记录调用参数与结果摘要。这是使用 CLI 看不到完整执行链路的直接原因。

建议提供脱敏的 Trace 输出或 JSONL 文件入口，记录参数和受限结果摘要，并用 `session_id`、`run_id`、`trace_id` 串联每次调用。

### P2-04：工具超时后仍可能发生副作用

位置：`src/yjy_agent_runtime/infrastructure/tools/executor.py:106-125`。

执行器使用守护线程和 `join(timeout)`。返回 `TimeoutError` 时，工作线程仍可运行；后续写入可能发生在 Agent 已经向模型报告失败之后。

本次使用明确标注的临时工具替身复现：工具延迟 0.05 秒后写入内存待办，执行器超时设为 0.001 秒。立即返回 `error / TimeoutError`，待办为空；线程结束后，待办变为 `('late write',)`。该验证未调用外部服务。

建议明确区分“等待超时”与“执行已取消”，对具有副作用的工具提供提交前的截止时间检查、可协作取消或适当的执行隔离；测试超时之后的真实状态，避免重复执行已发生的写入。

### P2-05：模型 Context 缺少完整的工具交互元数据

位置：`src/yjy_agent_runtime/agent/loop.py:202-215`；`src/yjy_agent_runtime/infrastructure/context/builder.py:95-96`；`src/yjy_agent_runtime/infrastructure/llm/openai_compatible.py:139-147`。

Session 保存了结构化工具调用和结果，但模型历史只读取 `messages`。其中的工具消息只有调用 ID 与内容，适配器再把它编码成用户消息；实际工具名、参数、成功或失败状态、错误类型及对应助手调用均没有完整回填。

简单工具任务可能成功，但多工具结果关联、错误恢复及后续追问缺少可靠元数据。建议在自定义 JSON 协议中携带调用与结果的结构化摘要，并测试成功和失败结果在模型 Context 中可以区分。

## 当前 CLI 怎么通信

这是一次消息对应一次进程调用的 CLI。消息来自位置参数，或省略参数时从 stdin 读取一行。程序加载指定 Session，执行多个 Agent 内部轮次，通过 HTTP POST 调用兼容的 `/chat/completions` 接口，最后将答案写入 stdout，错误写入 stderr。当前没有持续聊天的交互式输入循环或 HTTP 服务入口。

LLM 根据系统消息中的工具 Schema 返回项目约定的 JSON 决策；本地 Runtime 负责解析、校验和执行工具，再请求下一次决策。API 服务没有替项目执行 Agent Loop。

在仓库根目录使用已配置的本地文件运行：

```bash
LLM_TIMEOUT_SECONDS=120 uv run --locked --env-file .env yjy-agent-runtime --session-id window-1 "计算 137 * 29，并把结果记入待办"
LLM_TIMEOUT_SECONDS=120 uv run --locked --env-file .env yjy-agent-runtime --session-id window-1 "列出我的待办"
LLM_TIMEOUT_SECONDS=120 uv run --locked --env-file .env yjy-agent-runtime --session-id window-2 "记待办：写周报"
```

同一窗口复用同一 ID，另一个窗口使用另一个 ID，并共用同一个 `YJY_SESSION_DIR`。默认目录是 `.yjy-sessions`。省略 `--session-id` 时，两边都会使用 `default`，因此不同终端窗口不会自动产生不同 Session。

每轮 Context 放入系统约束和工具 Schema、较早历史摘要、最近消息以及受字符限制的工具结果。待办作为当前 Session 的结构化状态，由 `todo` 读取；没有向量检索式的长期 Memory，也没有跨 Session 的隐式召回。

## 后续完成条件

- [ ] 修正基础压缩并证明总 Context 有界，压缩后仍可追问。
- [ ] 明确 `.env` 加载与超时配置，验证实际 CLI 入口。
- [ ] 增加真实工具闭环与 Session 追问、隔离测试。
- [ ] 保证默认测试在已配置 LLM 环境下仍不调用真实网络。
- [ ] 提供 CLI 可查看的脱敏 Trace，补充参数和结果摘要。
- [ ] 处理或明确工具超时后仍执行的副作用语义。
- [ ] 补全模型 Context 中的工具交互元数据。

本轮完成检查、复现和问题记录；以上 Runtime 修复尚未实施。
