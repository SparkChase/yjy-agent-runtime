# 分层与数据流说明

本文回答两个问题：代码分成哪几层、每层负责什么；以及一条用户消息在 Runtime 内部
如何流转。阅读源码时配合本文，可以按层定位文件，不必靠文件名猜测行为。

## 1. 四层结构

```text
src/yjy_agent_runtime/
├── domain/          领域层：稳定的数据结构与不变量，不访问任何 I/O
├── agent/           应用层：AgentLoop、端口协议和一次运行的编排
├── infrastructure/  基建层：LLM、Session、Tool、Context、Trace 的具体实现
└── interfaces/      入口层：命令行、配置加载和对象装配
```

依赖方向是单向的，只允许下游被上游依赖：

```text
interfaces ──► infrastructure ──► agent ──► domain
```

这条规则由 `tests/test_architecture.py` 静态解析 import 守护。任何一层反向依赖
（例如 `agent` 导入 `infrastructure`，或 `domain` 导入 `agent`）都会让测试失败。

### 领域层 `domain/`

- `models.py` 定义 `Message`、`ToolCall`、`ToolResult`、`Session`、`RunRequest`、
  `RunResult`、`TraceEvent` 和全部受控枚举。
- 只做构造期校验，例如工具消息必须有 `tool_call_id`、失败结果必须有 `error_type`。
- 不导入 HTTP、文件系统、命令行或其它内部层。

### 应用层 `agent/`

- `loop.py` 是 `AgentLoop`：显式线性循环，负责加载 Session、构建 Context、调用
  LLM、执行工具、写回 Session、判断终止。
- `ports.py` 定义能力契约：`ContextProvider`、`LLMClient`、`ToolExecutor`、
  `SessionStore`、`TraceRecorder`、`HistorySummarizer`，以及工具的稳定契约
  `ToolDefinition` 与 `Tool`。
- 应用层只依赖端口和领域对象，不知道背后是 HTTP、JSON 文件还是内存实现。工具契约
  放在这里而不是基建，是因为 Context 和模型都要消费它。

### 基建层 `infrastructure/`

| 子包 | 实现 | 关键文件 |
| --- | --- | --- |
| `llm/` | OpenAI Chat Completions 兼容适配器，解析 JSON 决策 | `openai_compatible.py` |
| `session/` | 进程内 Store 与 JSON 文件 Store，按 `session_id` 隔离 | `in_memory.py`、`file.py`、`invariants.py` |
| `tools/` | 工具注册表、参数校验、按副作用分流的执行与内置工具 | `registry.py`、`executor.py`、`deadline.py`、`builtin.py` |
| `context/` | 组装模型输入，做历史摘要与预算裁剪 | `builder.py` |
| `trace/` | 结构化事件、递归脱敏与 JSONL 落盘 | `redaction.py`、`in_memory.py`、`jsonl.py`、`fanout.py` |

基建层实现应用层端口，可以依赖 `agent` 与 `domain`，但相互之间不横向耦合，也不被
`agent` 反向依赖。

### 逐文件职责速查

按阅读顺序列出每个文件"干什么、为什么存在"，方便逐行对照源码。

领域层 `domain/`：
- `models.py`：全部不可变领域对象与受控枚举，附带构造期不变量校验。

应用层 `agent/`：
- `loop.py`：`AgentLoop` 线性循环与 `LoopConfig` 安全边界，编排一轮运行。
- `ports.py`：能力协议（Context/LLM/Tool/Session/Trace/History）与工具契约
  `ToolDefinition`、`Tool`。

基建层 `infrastructure/`：
- `llm/openai_compatible.py`：HTTP 适配器、配置读取、分阶段超时、决策 JSON 解析。
- `session/in_memory.py`：进程内 SessionStore 与 `SessionNotFoundError`。
- `session/file.py`：JSON 文件 SessionStore、原子替换与 `SessionStorageError`。
- `session/invariants.py`：工具交互一致性规则（调用/结果/消息互相对应、`call_id` 唯一）。
- `tools/registry.py`：按唯一名称保存工具、查找、导出定义视图。
- `tools/executor.py`：参数 Schema 校验、按 `has_side_effects` 分流执行、结构化成功/失败结果与 `timeout_semantics`。
- `tools/deadline.py`：协作式截止时间，供有副作用的工具在写入前放弃提交。
- `tools/builtin.py`：`echo`、`calculator`、mock `search`、Session 级 `todo`。
- `context/builder.py`：`ContextConfig`、抽取式摘要器与 `ContextBuilder`。
- `trace/redaction.py`：敏感字段脱敏规则，内存版与文件版共用。
- `trace/in_memory.py`：进程内 Recorder，事件留在内存供测试与调试。
- `trace/jsonl.py`：JSONL 落盘 Recorder 与 `TraceWriteError`。
- `trace/fanout.py`：把一条事件同时送给内存与文件 Recorder。

### 入口层 `interfaces/`

- `config.py`：`.env` 解析与注入，显式环境变量优先。
- `cli.py`：`TraceSink`、参数解析、装配具体实现、调用 `AgentLoop`、输出结果与错误。
- 只有入口层知道"具体用哪个 Store、哪个 LLM 客户端"。换一个入口（HTTP 服务或
  交互式命令行）复用的是同一套 `agent` 与 `infrastructure`。

## 2. 一次运行的数据流

以 CLI 一次调用 `yjy-agent-runtime --session-id window-1 "计算 137 * 29"` 为例：

```text
1. interfaces/cli.py
   读参数与环境变量 -> 构造 OpenAICompatibleLLMClient、JsonFileSessionStore、
   ToolRegistry、RegistryToolExecutor、ContextBuilder、TraceRecorder -> AgentLoop

2. AgentLoop.run(RunRequest(session_id, user_input))
   - session_store.get_or_create(session_id)
   - append_message(USER, user_input)
   - 记录 run_started

3. 循环第 1 轮
   - context_provider.build(request, tool_results)
       读取当前 Session -> 需要时压缩历史 -> 组装
       [system 约束+工具 Schema][较早历史摘要][最近消息][工具结果]
   - llm_client.decide(messages) -> FinalAnswerDecision 或 ToolCallDecision

4a. 若为工具决策
   - tool_executor.execute(session_id, call)
       ToolRegistry 查找 -> JSON Schema 校验
       -> 无副作用: 线程超时 / 有副作用: 同步执行或协作式 Deadline
       -> ToolResult(success 或 error, 含 timeout_semantics)
   - append_tool_interaction(session_id, call, result, message)
       一次快照写入调用、结果和 TOOL 消息
   - 记录 tool_started / tool_completed，进入下一轮

4b. 若为最终回答
   - append_message(ASSISTANT, answer)
   - 生成 RunResult(final_answer) 并返回

5. 终止
   - 记录 run_terminated，返回 RunResult
```

## 3. Session 在每一层怎么流转

Session 是"当前对话的全部已确认事实"，由 `session_id` 唯一确定，任何读写都必须
显式携带它。

| 层 | 对 Session 的职责 |
| --- | --- |
| `domain` | 用 `Session` 不可变快照表达消息、工具交互、待办和摘要元数据，并校验一致性 |
| `agent` | 只通过 `SessionStore` 端口读写；每轮开头加载，工具结果和助手回答立即写回 |
| `infrastructure/session` | 实现端口：内存 Store 供测试与嵌入，JSON 文件 Store 供跨进程恢复；按 `session_id` 哈希成独立文件 |
| `infrastructure/context` | 只读取当前 `session_id` 的历史与摘要，绝不读取其它 Session |
| `infrastructure/tools` | 需要 Session 的工具（如 `todo`）通过 `invoke_with_session(session_id, ...)` 显式拿到当前 Session |

关键隔离规则：

- 不同 `session_id` 的消息、工具结果、待办和摘要互不可见。
- `todo` 待办是 Session 结构化状态，不依赖历史摘要，因此不会被压缩丢。
- 摘要只记录公开内容，覆盖范围写入 `summary.covered_message_count`，不推进就无法
  重复摘要，推进成功才落盘。

## 4. Context 组装规则

`ContextBuilder.build()` 固定输出顺序：

1. `system`：系统约束 + 工具名称、描述、参数 Schema、副作用标记。
2. `system`：较早历史摘要（存在时）。
3. 最近历史消息：在消息条数预算内。
4. 工具结果：受字符预算限制，超长部分截断。

默认不进入 Context 的内容：其它 Session 的任何内容、模型私有思维链、可再次读取的
超大原始输出。默认摘要器是可替换的能力，通过 `HistorySummarizer` 端口注入。

## 5. 异常与终止

- LLM 网络、服务或解析错误：终止原因为 `llm_error`，保留原始错误类型与消息。
- Context 构建失败：`context_error`。
- 工具执行器自身抛出异常：`tool_error`；可恢复的工具失败（未知工具、参数非法、
  工具内部异常、超时）会转为 `ToolResult(error)` 交给模型继续判断，而不是终止循环。
  超时结果还带 `timeout_semantics`，说明“等待超时”之后是否可能仍有副作用。
- 达到最大轮次：`max_iterations`。
- 非法决策类型：`invalid_decision`。

每次终止都会记录 `run_terminated`，与 `run_id`、`trace_id` 关联。
