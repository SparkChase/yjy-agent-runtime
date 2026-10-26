# Agent Runtime 修复与验收计划

编写日期：2026-10-26。

本计划只描述问题、修复方案和验收标准，不宣称任何未完成的修复已经生效。计划基于当前工作区
`7d13d36` 及其未提交修改。当前工作区已有 6 个修改文件和未跟踪的
`docs/runtime-audit.md`，实施时必须保留这些改动，不得覆盖或回滚。

## 1. 结论

项目不是“核心模块完全没有实现”。`llm`、`session`、`tools`、`trace`、`context`
五个顶层目录只有 `.gitkeep`，是早期目录迁移留下的空占位；真实实现位于
`infrastructure/` 下的同名子包。真正的问题是：

1. 目录结构让读者误以为模块缺失，分层边界也没有被测试保护。
2. Context 压缩没有真正限制模型输入大小。
3. 工具结果缺少结构化元数据，模型无法可靠区分成功、失败和对应调用。
4. 工具超时只停止等待，不停止执行；有副作用的工具可能在超时后继续写入。
5. CLI 不自动加载 `.env`，默认超时与真实服务不匹配，真实闭环没有自动化验收。
6. 默认测试入口可能执行真实网络请求；真实集成测试只验证单次决策，没有驱动 Agent Loop。
7. CLI 成功运行没有可查看、可落盘的 Trace，工具参数和结果摘要也没有记录。
8. Session 的工具调用、结果和工具消息分两次写入，进程中断可能留下半条交互。
9. 文档中的“已完成”“默认测试不访问网络”等表述与当前行为不完全一致。

当前可复现的基线：

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -m 'not integration' -q` | 77 passed, 1 deselected |
| `uv run --locked ruff check .` | 通过 |
| `uv run --locked python scripts/verify_md_links.py` | 通过 |
| `uv run --locked python scripts/verify_doc_budgets.py` | 通过 |
| `uv run --locked --env-file .env pytest -m integration -q` | 1 passed，单次真实 LLM 决策，耗时约 20 秒 |
| 真实 CLI 完整工具闭环 | 尚未取得最终回答和终止状态的完整证据 |

因此当前状态应描述为“离线闭环可用，真实 LLM 已连通，但完整真实验收未完成”，不能描述为
“题目已全部验收完成”。

## 2. 目标架构

保留现有分层，不为了改名做无收益的大规模迁移：

```text
src/yjy_agent_runtime/
├── domain/          # 领域层：纯数据、规则、不变量，不访问 I/O
├── agent/           # 应用层：AgentLoop、端口和一次运行的编排
├── infrastructure/  # 基建层：LLM、Session、Tool、Context、Trace 的具体实现
└── interfaces/      # 入口层：CLI、配置加载和对象装配
```

依赖方向：

```text
interfaces ──► infrastructure ──► agent ──► domain
```

约束：

- `domain/` 不导入 `agent/`、`infrastructure/`、`interfaces/`。
- `agent/` 只依赖 `domain/` 和自己定义的端口，不导入具体 HTTP、文件或 CLI 实现。
- `infrastructure/` 实现 `agent/` 的端口，可以依赖 `agent/` 和 `domain/`。
- `interfaces/` 负责读取配置、装配具体实现并调用应用层。
- 删除 `src/yjy_agent_runtime/{llm,session,tools,trace,context}/.gitkeep` 及空目录，逐个删除明确路径，不使用批量删除命令。
- 增加一个架构测试，扫描源码导入关系，防止应用层反向依赖基建层。

需要调整的一处具体耦合：`infrastructure/context/builder.py` 直接导入
`infrastructure/tools` 的 `ToolDefinition`。实施时把工具定义契约放到应用端口或领域层，
让 Context 只依赖抽象定义，避免两个基建子模块互相绑定。

## 3. 问题、修复与验收

> 状态图例：**[已修复]** 已实现、测试并提交；**[未修复]** 尚待处理。
> 逐条进度与提交哈希见第 4、5 节。

### P0-01 [已修复]：空目录造成“模块没有实现”的误解

现象：

- `src/yjy_agent_runtime/llm/`、`session/`、`tools/`、`trace/`、`context/` 只有 `.gitkeep`。
- 真实代码在 `src/yjy_agent_runtime/infrastructure/` 下，README 的目录树没有解释这层关系。

修复：

- 逐个删除上述五个空目录中的 `.gitkeep`，再逐个删除空目录。
- README 目录树只展示真实存在的分层和子包，并说明 `agent/` 是应用层。
- 增加架构测试，确认应用层没有导入 `infrastructure`。

验收：

- `rg --files src/yjy_agent_runtime` 不再出现五个空的顶层占位目录。
- 架构测试通过。
- 新读者只看 README 能准确找到 LLM、Session、Tool、Trace、Context 的实现位置。

### P1-01 [未修复]：Context 压缩没有真正减少输入

现象：

- `ExtractiveHistorySummarizer` 把旧摘要和每条旧消息的完整内容拼接，摘要没有长度上限。
- 长工具结果进入摘要后绕过最近消息的工具结果截断。
- 没有总 Context 字符或 Token 预算，消息条数下降不等于内容减少。
- 当前审计已用 10 条和 100 条千字消息复现总字符随历史线性增长。

修复：

- 为摘要增加 `max_summary_chars`，按行截断并保留首尾关键内容，明确写入截断标记。
- 为整个模型输入增加 `max_context_chars`；超预算时按完整消息边界裁剪最近历史，并同步推进摘要覆盖范围。
- 摘要前先限制单条工具结果，避免摘要绕过工具结果预算。
- 保留 `covered_message_count` 的单调性；摘要失败时不推进覆盖范围。
- 不拆分工具调用与工具结果这一组交互。

验收：

- 构造 100 条、每条约 1000 字符的历史，断言模型输入总字符不超过配置预算，摘要不超过摘要预算。
- 断言早期事实仍能被后续追问使用。
- 断言压缩后 `covered_message_count` 正确、不重复摘要、不遗漏消息。
- 断言摘要失败后覆盖范围不变，下一次可以重试。
- 用真实 LLM 做一次“早期事实追问”，回答必须包含预置事实，不能只依赖 Mock。

### P1-02 [未修复]：工具结果缺少模型可用的结构化元数据

现象：

- Session 保存了 `ToolCall` 和 `ToolResult`，但模型历史只读取 `Message`。
- 工具消息只有 `tool_call_id` 和内容，模型看不到工具名、参数、成功/失败状态和错误类型。
- `ContextBuilder.build()` 接收 `tool_results` 参数却直接丢弃。

修复：

- 在 Context 中把每个工具消息渲染成结构化信封，至少包含：
  `call_id`、`tool_name`、`arguments`、`status`、`result`、`error_type`。
- 参数和结果分别按预算截断；敏感字段沿用脱敏规则。
- 多个工具调用按 `call_id` 一一对应，错误结果必须保留 `error_type`。
- 不把模型私有思维链写入 Session 或 Context。

验收：

- 离线测试覆盖成功、失败、一次多个工具调用和错误恢复。
- 模型 Context 中能明确区分 `success` 与 `error`，并看到对应工具名和参数。
- 真实 LLM 在一次任务中连续调用至少两个工具后，最终回答能引用工具结果。

### P1-03 [未修复]：工具超时后仍可能发生副作用

现象：

- `RegistryToolExecutor` 用守护线程加 `join(timeout)`；超时只是停止等待，工作线程仍在运行。
- 已有复现：超时后立即返回错误，稍后待办仍被写入。

修复：

- 明确区分“等待超时”和“执行已取消”。
- 对 `has_side_effects=True` 的工具，默认在调用线程同步执行，不把“等待超时”伪装成“执行已取消”。
- 需要超时保护的副作用工具必须实现协作式截止时间检查，并在写入前检查截止时间。
- 无副作用工具可以继续使用线程超时；超时后即使线程结束，也不会改变外部状态。
- Trace 中记录 `timeout_semantics`，让调用方知道该次超时是否可能仍有后续副作用。

验收：

- 构造延迟写入的副作用工具，执行器超时后等待超过工具延迟，断言待办仍为空。
- 无副作用慢工具仍返回结构化 `TimeoutError`。
- 测试覆盖同步副作用路径、协作取消路径和纯工具超时路径。

### P1-04 [已修复]：CLI 配置入口和默认超时不可用

现象：

- 配置只读 `os.getenv()`，普通 `uv run` 不会加载 `.env`。
- `.env` 没有设置超时，CLI 使用 30 秒；真实任务在第二次 LLM 请求处超时。
- 集成测试强制至少 120 秒，与实际 CLI 配置不一致。

修复：

- 在 `interfaces/` 增加配置加载模块，支持 `--env-file`，默认尝试当前目录 `.env`，并允许 `--no-env-file` 关闭。
- 显式环境变量优先于 `.env`；不打印密钥。
- 默认读取超时调整为适合真实服务的值，并允许 `LLM_TIMEOUT_SECONDS` 覆盖；`.env.example` 和 README 同步。
- 使用 `httpx.Timeout` 区分连接超时和读取超时，避免所有阶段共用同一个短超时。
- CLI 与真实集成测试使用同一套配置加载逻辑。

验收：

- 在临时目录放置 `.env`，直接运行 CLI 能读取配置；显式环境变量能覆盖文件值。
- `--no-env-file` 时不会读取 `.env`。
- 缺少配置时给出明确错误，不发起网络请求。
- 真实 CLI 在默认配置下完成一次多轮工具任务，退出码为 0。

### P1-05 [已修复]：默认测试可能访问真实网络

现象：

- `integration` 标记只做分类，不会自动跳过。
- 一旦进程已有三项 LLM 配置，`uv run pytest -q` 会执行真实请求。
- README 却声称默认测试不访问真实网络。

修复：

- 在 `pyproject.toml` 或 `conftest.py` 中让默认测试入口排除 `integration`。
- 保留单独的真实测试入口，并确保显式 `-m integration` 可以覆盖默认排除。
- 增加一个测试或命令行检查，证明在已配置 LLM 环境下默认入口仍不访问网络。

验收：

- 已加载 `.env` 时执行 `uv run pytest -q`，不产生真实 LLM 请求。
- 未配置时执行 `uv run pytest -m integration -q`，明确 skip 并说明原因。
- 配置有效时执行真实入口，测试真实通过。

### P1-06 [未修复]：真实集成测试没有驱动 Agent Loop

现象：

- 现有真实测试只调用 `client.decide()`，断言 `decision is not None`。
- 没有验证工具注册、Schema 暴露、工具执行、Session 写入、下一轮 Context 和最终回答。
- 完整闭环测试使用 `MockTransport` 或预设决策，不能替代真实模型自主调用工具。

修复：

- 增加真实端到端测试，使用真实 LLM、真实工具注册表、真实 AgentLoop 和临时 Session 目录。
- 场景一：真实模型调用 `calculator` 计算 `137 * 29`，并调用 `todo` 写入结果，最终回答包含 `3973`。
- 场景二：同一 Session 再问待办，真实模型调用 `todo list` 并回答包含之前写入的内容。
- 场景三：另一个 Session 问待办，回答不得包含第一个 Session 的内容。
- 场景四：预置长历史后追问早期事实，验证压缩后仍能回答。
- 断言至少发生一次真实工具调用；断言 Session 中存在对应的 `ToolCall` 和成功 `ToolResult`。
- 不对模型措辞做精确匹配，只断言事实、工具事件和终止原因。

验收：

- `uv run --env-file .env pytest -m integration -q` 真实通过。
- 测试输出包含真实模型标识、耗时、工具调用次数和 Trace 路径；不输出 API Key。
- 任一场景失败时保留原始错误和 Trace，不用 Mock 或重试伪造成功。

### P1-07 [已修复]：CLI Trace 不可见且信息不完整

现象：

- CLI 只在内存中保存 Trace，成功运行后进程结束即丢失。
- 失败时只打印最近一个依赖错误，没有完整执行链。
- 工具事件没有参数和结果摘要。

修复：

- 增加 JSONL TraceRecorder，按 `run_id` 写入文件。
- CLI 增加 `--trace-file` 或 `YJY_TRACE_DIR`，默认写入 `logs/`；成功和失败都输出 Trace 路径。
- 记录 `session_id`、`run_id`、`trace_id`、轮次、LLM 耗时、工具名、脱敏参数、结果摘要、状态、错误类型和终止原因。
- 不记录完整 Prompt、私有思维链和密钥。
- 对 Trace 写入失败保留明确错误，不静默吞掉。

验收：

- 一次真实 CLI 运行后 Trace 文件存在，事件可按 `run_id` 关联。
- Trace 中能看到两次以上工具调用、参数、结果摘要和最终终止原因。
- 人为注入 `api_key`、`token`、`cookie` 等字段，文件中只出现 `[REDACTED]`。
- Trace 写入失败时 CLI 返回非零退出码并给出可诊断错误。

### P2-01 [未修复]：Session 工具交互不是原子写入

现象：

- `append_tool_interaction()` 保存一次，`append_message()` 再保存一次。
- 两次写入之间进程中断，会留下有调用/结果但没有工具消息，或有消息但没有结构化结果的半条交互。

修复：

- 增加一次性写入工具调用、结果和工具消息的存储方法。
- 文件 Store 使用一次原子替换保存完整交互。
- 增加 Session 一致性检查：工具消息必须有对应调用和结果，调用 ID 不得重复。

验收：

- 正常路径下一次调用同时产生三类数据。
- 模拟第二次写入失败时，不出现半条交互。
- 两个 Session 的工具数据仍然完全隔离。

### P2-02 [未修复]：计算工具存在资源耗尽风险

现象：

- `calculator` 允许任意长度表达式和任意指数，例如超大幂运算可能长时间占用 CPU 或内存。

修复：

- 限制表达式长度、指数绝对值和结果规模。
- 超限时返回明确错误，不执行危险运算。

验收：

- 正常算术通过。
- 超长表达式、超大指数和非法语法快速失败。
- 测试不会因资源耗尽而卡住。

### P2-03 [部分修复]：文档与代码能力不一致

现象：

- README 的目录说明把 Session 只写成进程内 Store，实际已有 JSON 文件 Store。
- README 声称默认测试不访问真实网络，但默认收集包含 integration。
- `development-plan.md` 把“结果摘要”等能力标为已完成，但代码尚未记录工具参数和结果摘要。
- 真实集成测试已能通过单次决策，但文档仍写成“因缺少凭据跳过”。

修复：

- 修复完成后统一更新 README、开发计划和审计记录。
- 每个“已完成”条目必须能对应代码、测试和真实命令证据。
- 真实验收记录写明日期、模型、耗时、命令、结果和未覆盖边界，不写密钥。

验收：

- 文档中的目录、命令、能力和测试状态与代码一致。
- Markdown 链接和文档预算检查通过。
- 审阅者能从文档复现离线测试和真实 LLM 验收。

## 4. 实施顺序与提交边界

每个步骤独立提交，提交前检查 `git status --short` 和 diff，不使用 `git add -A`。

| 顺序 | 任务 | 状态 | 提交 |
| --- | --- | --- | --- |
| 0 | 修复与验收计划 | 已完成 | `a20b680` |
| 1 | 架构清理 | 已完成 | `84223dc` |
| 2 | 配置与 CLI | 已完成 | `ac76cae`、`70f21ba` |
| 3 | Trace | 已完成 | `d60f849`、`f324ad8`、`aa620fc`、`19e3cfa` |
| 4 | Context 压缩 | **未开始（下一步）** | 预期 `fix(context): 实现有界压缩与工具结果元数据` |
| 5 | 工具执行 | 未开始 | 预期 `fix(tools): 修正超时副作用与计算边界` |
| 6 | Session 原子写 | 未开始 | 预期 `fix(session): 原子保存工具调用与结果` |
| 7 | 测试矩阵 | 部分完成（Trace/CLI/配置/架构测试已随各步补齐；真实端到端未做） | 预期 `test: 补齐真实 LLM 与异常路径验收` |
| 8 | 文档与真实验收 | 部分完成（逐层与逐文件文档已写；最终真实验收未做） | 预期 `docs: 记录真实 LLM 验收结果` |

现有未提交修改不属于本计划，不应混入上述提交。若这些修改与某项修复重叠，先由用户确认其归属，
再按功能拆分提交。

## 5. 已完成步骤的证据（截至 2026-10-26）

计划起点为 `7d13d36`。以下步骤已实现、测试并提交，工作区保持干净。

### Step 1 架构清理（`84223dc`）

- 删除只有 `.gitkeep` 的五个顶层空包 `llm/session/tools/trace/context`（逐个显式路径删除）。
- `ToolDefinition` / `Tool` 契约上移到 `agent/ports.py`，`infrastructure/tools/registry.py` 保留 re-export。
- 新增 `tests/test_architecture.py`，静态校验 `interfaces -> infrastructure -> agent -> domain`。
- 新增 `docs/architecture-layers.md`。

### Step 2 配置与 CLI（`ac76cae`）

- `interfaces/config.py`：解析 `.env`、显式环境变量优先、`--env-file` / `--no-env-file`。
- 默认读取超时 120s，新增 `LLM_CONNECT_TIMEOUT_SECONDS=10`，改用分阶段 `httpx.Timeout`。
- `tests/test_config.py` 覆盖解析、优先级、禁用与错误语义。

### Step 3 Trace（`d60f849`、`f324ad8`、`aa620fc`、`19e3cfa`）

- `trace/` 拆分为 `redaction.py`（共享脱敏）、`in_memory.py`、`jsonl.py`、`fanout.py`。
- `AgentLoop` Trace 增加 `prompt_chars`、工具 `arguments`、`result_summary`，按上限截断。
- CLI `--trace-file` / `--no-trace` / `YJY_TRACE_DIR`，成功与失败都打印 trace 路径。
- `tests/conftest.py` 让默认门禁排除 integration。
- 新增 `tests/test_jsonl_trace.py`、`tests/test_loop_trace.py`、`tests/test_cli.py`。

真实 LLM 证据（默认 `.env`，多工具任务）：Trace 14 行，`calculator` 得 `3973`、`todo` 写入待办、终止原因 `final_answer`；工具参数与结果摘要齐全，`run_id/trace_id/session_id` 可关联，文件中无 API Key。

### 当前门禁基线

```text
uv run --locked ruff check .                 # 通过
uv run --locked pytest -q                    # 111 passed, 1 deselected
uv run --locked python scripts/verify_md_links.py     # 通过
uv run --locked python scripts/verify_doc_budgets.py  # 通过
uv run --locked --env-file .env pytest -m integration -q   # 1 passed（真实单次决策）
```

## 6. 验收矩阵

### 6.1 离线门禁

```bash
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked python scripts/verify_md_links.py
uv run --locked python scripts/verify_doc_budgets.py
```

要求：

- 第一条命令不发起真实网络请求，即使当前 shell 已加载 `.env`。
- 所有单元测试和 Mock 闭环测试通过。
- 静态检查、链接检查和文档预算检查通过。

### 6.2 真实 LLM 门禁

以下命令必须使用真实配置，不能替换为 MockTransport、预设决策或手工伪造的 Session：

```bash
uv run --locked --env-file .env pytest -m integration -q
uv run --locked --env-file .env yjy-agent-runtime \
  --session-id real-acceptance-1 \
  --trace-file logs/real-acceptance-1.jsonl \
  "请使用 calculator 计算 137 * 29，并使用 todo 把结果记入待办，最后回答结果。"
uv run --locked --env-file .env yjy-agent-runtime \
  --session-id real-acceptance-1 \
  --trace-file logs/real-acceptance-2.jsonl \
  "列出我的待办。"
uv run --locked --env-file .env yjy-agent-runtime \
  --session-id real-acceptance-2 \
  --trace-file logs/real-acceptance-3.jsonl \
  "列出我的待办。"
```

通过标准：

- 第一条命令真实通过，至少一个场景产生真实工具调用。
- 第一次 CLI 退出码为 0，最终回答包含 `3973`。
- 对应 Session 中存在 `calculator` 和 `todo` 的成功结果，待办包含 `3973`。
- 第二次 CLI 回答包含第一次写入的待办。
- 第三次 CLI 不包含第一个 Session 的待办，证明隔离。
- Trace 文件存在，包含工具参数、结果摘要和 `final_answer` 终止原因，敏感字段已脱敏。

### 6.3 压缩后追问的真实 LLM 验收

预置一个包含早期事实的长 Session，例如早期用户消息写明“验收代号是 ORANGE-42”，再追加
足够多的历史触发压缩，然后用真实 LLM 提问：

```text
我最开始告诉你的验收代号是什么？只回答代号。
```

通过标准：

- 模型回答包含 `ORANGE-42`。
- 本次模型输入的字符数不超过配置预算。
- Session 摘要覆盖范围正确推进，且没有把其他 Session 的内容带入。
- 该场景的 Trace 记录压缩前后的消息数量和字符数。

### 6.4 证据记录

验收记录至少包含：

- 日期、提交哈希、使用的模型名称（不写密钥）。
- 每条命令、退出码、耗时和关键输出摘要。
- 真实工具调用次数和工具名。
- Trace 文件路径和 `run_id`。
- 未通过项、原始错误和剩余阻塞。

Mock 测试只能作为回归门禁，不能计入真实 LLM 验收通过项。

## 7. 已确认的决策

1. **目录命名**：保留 `agent/` 作为应用层，不重命名；重点是分层清晰，且由架构测试守护。
2. **`.env` 行为**：CLI 默认加载当前目录 `.env`，支持 `--no-env-file` 关闭，显式环境变量优先（已实现）。
3. **真实验收成本**：真实验收必须打真实付费 LLM，不能用 Mock 或预设决策代替（已确认）。
4. **入口形态**：CLI 可验收即可，不强制 HTTP；后续如需 HTTP 入口，复用同一套 `agent` 与 `infrastructure`。
5. **代码可读性**：要求注释清楚、分层清楚、逐层数据流写清楚（已通过 `docs/architecture-layers.md` 落地，后续步骤需延续）。

## 8. 下一步交接：Step 4 Context 有界压缩

### 8.1 目标

当前压缩是"假压缩"：`ExtractiveHistorySummarizer` 把旧摘要与每条旧消息全文拼接，
摘要无长度上限，模型输入也无总预算。用 100 条、每条约 1000 字符的历史可复现约
10 万字符的模型输入。Step 4 要让"压缩"真正减少输入，同时不丢早期关键事实。

### 8.2 要改的文件

- `src/yjy_agent_runtime/infrastructure/context/builder.py`（主改动）
- 可能扩展 `agent/ports.py` 的 `HistorySummarizer` 契约
- 测试：`tests/test_context_builder.py`，可新增 `tests/test_context_budget.py`

### 8.3 具体要求

1. `ContextConfig` 增加 `max_summary_chars` 与 `max_context_chars`，均须为正整数并在构造期校验。
2. 摘要器输出受 `max_summary_chars` 约束：按行累积，保留首尾关键内容，写入明确截断标记。
3. 摘要前先对单条工具结果套用 `max_tool_result_chars`，避免长工具结果绕过限制进入摘要。
4. 组装完成后，若整个模型输入超过 `max_context_chars`，按完整消息边界裁剪最近历史，
   并同步推进 `summary.covered_message_count`，保持其单调、不重复、不遗漏。
5. 摘要失败时不推进覆盖范围，下一轮可安全重试（沿用现有语义）。
6. 不拆分"工具调用 + 工具结果"这一组交互。
7. 保持"不把私有思维链写入摘要/Context"。

### 8.4 验收标准

离线：

- 构造 100 条、每条约 1000 字符历史，断言模型输入总字符 ≤ `max_context_chars`，摘要 ≤ `max_summary_chars`。
- 早期事实在压缩后仍能在 Context 中体现。
- `covered_message_count` 正确、不重复摘要、不遗漏消息。
- 摘要失败后覆盖范围不变。
- 工具结果不因进入摘要而绕过长度限制。

真实 LLM（必须真实付费调用，不能用 Mock）：

- 预置含早期事实的长 Session（例如早期写入"验收代号是 ORANGE-42"），触发压缩后追问；
  模型回答必须包含 `ORANGE-42`。
- 记录本次真实输入的字符数与压缩前后消息数。

### 8.5 完成定义

按仓库规范：行为满足验收、异常与边界已处理、测试真实通过、文档同步、无敏感信息泄露，
并且独立提交（预期提交信息 `fix(context): 实现有界压缩与工具结果元数据`）。

### 8.6 继任者注意

- 起始状态：`git status` 干净，HEAD 为 Step 3 后的提交。
- 默认门禁已是 `pytest -q`（自动排除 integration）；真实测试用 `uv run --locked --env-file .env pytest -m integration -q`。
- 真实验收会真实产生费用，必须执行。
- 每完成一个小功能立即独立提交，提交前看 `git status --short` 与 diff，不使用 `git add -A`。
