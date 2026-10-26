# AI Prompt 与问题解决记录

本文记录本题开发过程中使用的约束、提示词模板、问题证据和解决方式。它不是模型私有思维链，也不保存任何 API Key 或个人数据；只记录可以公开复现的工程决策。

## 1. 开发约束 Prompt

每次实现前使用以下任务模板：

```text
请在当前仓库内完成一个最小、可测试的 Agent Runtime 功能。

约束：
1. 不引入 LangGraph、OpenHands、OpenClaw、Pi 等 Agent 框架替代主循环。
2. 先阅读 docs/development-plan.md、README.md、相关源码和测试。
3. 明确输入、输出、不变量、失败方式和验收标准。
4. 工具执行前进行 Schema 校验；所有状态显式携带 session_id。
5. 不保存或展示模型私有思维链，只记录公开决策摘要、调用摘要、结果摘要和错误。
6. 默认测试不得依赖 API Key 或网络；真实 LLM 测试单独标记 integration。
7. 使用最小修改范围，完成后运行测试和静态检查。
8. 每个独立功能使用 Conventional Commits 创建独立提交。

交付时请说明：修改文件、测试证据、未解决阻塞和文档是否同步。
```

## 2. 本阶段实现 Prompt

```text
按题目要求完成剩余交付：至少三个工具（calculator、mock search、todo）、
Session 跨运行恢复、Context 追问和压缩、结构化 Trace、真实 LLM 适配器、
测试、README，以及 AI Prompt 与问题解决记录。

先把任务拆解并写入 docs/development-plan.md，再按小功能实现、测试、提交。
GitHub remote 和 GitHub 提交不属于本次任务范围。
```

## 3. 问题与解决记录

### 问题一：Trace 端口和 Recorder 无法接线

现象：AgentLoop 传递 `record("run_started", session_id=...)`，而正式 Recorder 只接受 `TraceEvent`；真实拼装时出现 `unexpected keyword argument 'session_id'`。

解决：统一 `TraceRecorder.record()` 为接收结构化 `TraceEvent`，由 AgentLoop 生成 `run_id`、`trace_id`、轮次和时间戳；补充 LLM、工具和终止事件测试。

### 问题二：题目要求三个工具，但仓库只有 Echo

现象：注册表和 Schema 机制存在，但内置工具只有 `echo`，不能满足题目的 calculator/search/todo 要求。

解决：

- `calculator` 使用受限 AST，只允许数字、括号、加减乘除、取模、幂和一元正负号。
- `search` 使用固定本地文档，明确标记为 mock，不访问网络。
- `todo` 使用当前 Session 的 `todos` 字段，工具执行器显式传入 `session_id`，支持 add/list。

### 问题三：CLI 每次启动都会丢失 Session

现象：进程内 `InMemorySessionStore` 可以测试隔离，但两次 CLI 启动会创建全新 Store，无法模拟窗口持续追问。

解决：新增 `JsonFileSessionStore`，以 Session ID 的 SHA-256 文件名保存 JSON 快照，使用临时文件 + `os.replace` 原子写入；CLI 通过 `YJY_SESSION_DIR` 配置目录。

### 问题四：LLM 输出需要可诊断，但不能保存私有思维链

现象：只能区分最终答案和工具调用时，Trace 缺少公开决策原因；直接保存完整 reasoning 又违反安全边界。

解决：协议增加可选 `decision_summary`，只接受公开短摘要并写入 Trace，不写入 Session；非法 JSON、未知决策类型和非字符串摘要都转换为明确解析错误。

### 问题五：真实 LLM 联调条件不可控

现象：适配器和 integration 测试已经存在，但当前执行环境没有 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。

解决：默认测试使用 `httpx.MockTransport` 验证完整闭环；真实测试标记为 `integration`，无配置时明确 skip。没有把 Mock 结果伪装成真实供应商调用结果。

### 问题六：CLI 退出码与 docstring 约定相反

现象：两处退出码和 `cli.py` docstring 的约定对不上，且都没有测试覆盖，门禁全绿也发现不了。

- `_run_interactive` 结尾固定 `return 0`。某一轮以 `llm_error` 结束、或 `loop.run` 直接抛异常时，进程仍然退出 0，脚本和 CI 会把失败判成成功。
- `--max-iterations 0` 的校验写在 `LoopConfig.__post_init__`，而 `LoopConfig` 在 `build_loop` 里构造，抛出的 `ValueError` 被 `main` 的运行失败分支捕获，退出码 1；docstring 约定“参数或配置有问题，根本没有发起运行”应为 2。

根因：`main` 把“参数/配置阶段”和“运行阶段”塞进同一个 `try`，退出码只能区分“有没有抛异常”，区分不了“有没有发起运行”；交互模式的返回值只表达“进程正常退出”，不表达“每一轮是否拿到答案”。

解决（`95d892e`）：

- `main` 拆成参数预检、配置阶段、运行阶段三段。`--max-iterations < 1` 在装配依赖前返回 2；配置阶段只捕获 `EnvFileError`、`LLMConfigurationError` 和 `.env` 读写/解码错误并返回 2，其余异常照常抛出，不把代码缺陷伪装成配置问题。
- `_run_interactive` 用 `failed` 标记汇总每轮结果：任一轮没拿到最终答案就返回 1，先失败后成功仍是失败；用户 Ctrl-C 主动中断的那一轮不计为失败。
- 配置失败不再创建 `TraceSink`，不会为一次没发起的运行留下空 trace 文件。
- 缺失 `LLM_API_KEY` / `LLM_BASE_URL` 的退出码由 1 改为 2，与 docstring 对齐，原先断言 1 的两个测试同步更新。

验证：新增 4 个用例覆盖交互模式失败轮、异常轮、先失败后成功、`--max-iterations 0`；修复前后用探针脚本实测，同一场景的退出码从 `0 / 1` 变为 `1 / 2`；离线门禁 132 passed, 2 deselected。

### 问题七：四个"已声明但未修"的坑

现象：修复计划把四个问题标成"[未修复]"，代码里也留着"这是已知边界"的注释，
于是它们既没有被修，也不再被当成缺陷报告。四个问题分别是：副作用工具超时后仍写入
（P1-03）、一次工具交互分两次落盘（P2-01）、`calculator` 没有资源上限（P2-02）、
集成测试没有驱动完整闭环（P1-06）。

根因：不是判断错误。四个问题被排在计划的 Step 5-7，Step 1-4 完成后排期转向 CLI 与
文档，没有明确 owner 重新认领；而"已知边界"写得太清楚，反而让它们看起来像已经
处理过的取舍。

解决（`8a747dd`、`661564c`、`f22ad98`、`692266f`）：

- 执行器读 `has_side_effects`：副作用工具同步执行，或实现 `Deadline` 协作取消；
  超时语义 `timeout_semantics` 进 `ToolResult`、Trace 与 JSON 快照。
- `append_tool_interaction(session_id, call, result, message)` 一次快照写完三类
  数据；`append_message` 拒绝单独追加 TOOL 消息；文件 Store 读取时校验旧快照。
- `calculator` 四道闸门：表达式 200 字符、嵌套 32 层、指数 ±64、结果 1024 位 / 1e100，
  指数检查发生在幂运算之前。
- 集成测试改为真实 LLM + 真实注册表 + 真实 AgentLoop + 临时 Session 目录，
  覆盖工具闭环、同 Session 追问、跨 Session 隔离和压缩后追问。

验证：离线 `150 passed, 4 deselected`；真实 `grok-4.7` 下 integration
`4 passed in 223.76s`，其中工具闭环 3 轮、`calculator`/`todo` 均成功、待办含
`3973`。逐条根因、残余边界见 `docs/runtime-remediation-plan.md` 第 9 节。

## 4. 可复现验证命令

```bash
uv run pytest -q
uv run --env-file .env pytest -m integration -q
uv run ruff check .
uv run python scripts/verify_md_links.py
uv run python scripts/verify_doc_budgets.py
```

默认测试必须在无网络、无 API Key 时通过；integration 测试只有配置真实服务后才可能产生真实联调证据。

