# 上手与代码阅读指南

这份文档只解决两件事：

1. 用最短路径把项目跑起来，亲手看到三个工具、Session 隔离和 Context 压缩。
2. 告诉你按什么顺序读代码，每一处该看什么。

设计细节和历史问题记录放在其它文档里；这里以“能跑、能看懂”为准。

---

## 1. 先理解一条链路

整个项目只有一条主链路，所有代码都围着它转：

```text
用户输入
  │
  ▼
CLI（interfaces/cli.py）
  │  装配：JSON SessionStore + ToolRegistry + ContextBuilder + LLM 客户端
  ▼
AgentLoop.run()（agent/loop.py）
  │
  ├─ 1. 把 USER 消息写进当前 Session
  ├─ 2. ContextBuilder.build() 组装本轮模型输入
  ├─ 3. LLM 返回 JSON 决策
  │       ├─ final_answer  -> 写 ASSISTANT 消息 -> 结束
  │       └─ tool_calls    -> 执行工具 -> 写回 Session -> 回到第 2 步
  └─ 4. 达到最大轮次 / 依赖报错 -> 返回结构化终止原因
```

三个关键事实：

- **循环是显式的**：没有框架替你决定下一步，`for` 循环、终止条件、工具调度都在 `AgentLoop.run()` 里。
- **模型只输出 JSON**：`{"type":"final_answer",...}` 或 `{"type":"tool_calls",...}`，解析在 `OpenAICompatibleLLMClient._decode_decision()`。
- **Session 是唯一状态**：所有历史、工具结果、待办、摘要都按 `session_id` 存在 Session 里；进程本身不保存对话。

---

## 2. 准备环境

要求 Python 3.12+ 和 uv。

```bash
uv sync --locked
```

在项目根目录创建 `.env`（不要提交到 Git）：

```dotenv
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model
LLM_API_KEY=your-key
```

可选配置：

```dotenv
LLM_TIMEOUT_SECONDS=120
LLM_CONNECT_TIMEOUT_SECONDS=10
YJY_SESSION_DIR=.yjy-sessions
YJY_TRACE_DIR=logs
```

CLI 会自动读取**当前目录**的 `.env`。如果你在别的目录执行，会读不到配置，报  
`LLM_BASE_URL must not be empty`。

先确认能对话：

```bash
uv run yjy-agent-runtime --session-id hello "你好，请用一句话介绍你自己"
```

预期：stdout 输出回答，stderr 输出一行 `trace: logs/hello.jsonl`，退出码 0。

---

## 3. 三个工具逐个跑

三个工具都是模型根据“工具描述 + 参数 Schema”自主选择的，不是硬编码路由。想稳定触发，最直接的办法是**明确说出工具名**：

| 想触发        | 直接这样说                      |
| ---------- | -------------------------- |
| calculator | `用 calculator 计算 137 * 29` |
| search     | `用 search 搜索 session 隔离`   |
| todo 新增    | `用 todo 记一条待办：明天写周报`       |
| todo 查看    | `列出我的待办`                   |
| echo       | `用 echo 重复：hello`          |

如果只说“算一下”，模型可能直接心算而不调用工具；这不是 bug，是模型的选择。想验证工具链路，就把工具名说清楚。

下面每条命令都可以直接复制。建议每次用不同的 `--session-id`，避免历史互相影响。

### 3.1 calculator

```bash
uv run yjy-agent-runtime --session-id t-calc \
  "请用 calculator 计算 137 * 29，然后告诉我结果"
```

预期：模型先调用 `calculator`，再给出包含 `3973` 的最终回答。

### 3.2 search（mock）

```bash
uv run yjy-agent-runtime --session-id t-search \
  "请用 search 搜索 session 隔离相关内容，并简要总结"
```

预期：返回 `Session 必须显式携带 session_id，并保持不同窗口隔离。`

`search` 是本地 mock，只有三条固定文档，不访问网络。它按空格切词，任一关键词命中即可。

### 3.3 todo（带副作用，按 Session 隔离）

第一步，写入：

```bash
uv run yjy-agent-runtime --session-id t-todo \
  "用 todo 记一条待办：明天写周报"
```

第二步，**同一个 session_id** 读取：

```bash
uv run yjy-agent-runtime --session-id t-todo \
  "列出我的待办"
```

预期：第二条命令返回的待办列表包含 `明天写周报`。

换一个 session 再读：

```bash
uv run yjy-agent-runtime --session-id t-todo-other \
  "列出我的待办"
```

预期：`当前没有待办`。这就是窗口 1 / 窗口 2 互不影响的直接证据。

### 3.4 三个工具串起来（推荐演示）

```bash
uv run yjy-agent-runtime --session-id demo-all \
  "请依次使用 calculator 计算 137*29，使用 search 搜索 session 隔离，最后用 todo 把计算结果记入待办"
```

我实测的运行轨迹是：

```text
第 1 轮：calculator(137*29) -> 3973
        search("session 隔离") -> 命中 Session 文档
第 2 轮：todo(add, "137*29 = 3973")
第 3 轮：final_answer
```

然后追问同一 session：

```bash
uv run yjy-agent-runtime --session-id demo-all \
  "我刚才算出了什么？待办里有什么？"
```

预期：回答同时包含 `3973` 和待办内容，证明历史与工具结果都被记住了。

### 3.5 连续对话：交互模式

前面每条命令都是一次进程、一条消息。想连续聊，用 `-i/--interactive`：

```bash
uv run yjy-agent-runtime --interactive --session-id chat
```

进入后直接输入，进程不退出，所有轮次共用同一个 Session：

```text
session: chat（/exit 退出，/new <id> 切换会话，/help 查看命令）
你> 你好，我叫小林
助手> 你好，小林！...
你> 我刚才告诉你我叫什么？
助手> 你刚才告诉我你叫小林。
```

| 命令                  | 作用                           |
| ------------------- | ---------------------------- |
| `/exit` 或 `/quit`   | 退出                           |
| `/new <session-id>` | 切换到另一个 Session（旧 Session 保留） |
| `/help`             | 查看命令                         |

注意：交互模式只是省掉“反复启动进程、反复输入 session id”，**不会减少模型延迟**。  
每条消息仍然要等一次或多次 LLM 返回。

---

## 4. 看运行过程：Trace 和 Session 文件

### 4.1 Trace

每次运行默认写 `logs/<session-id>.jsonl`，一行一个事件。可以直接看：

```bash
tail -n 5 logs/demo-all.jsonl
```

或者只打印关键事件：

```bash
uv run python - <<'PY'
import json
for line in open("logs/demo-all.jsonl", encoding="utf-8"):
    event = json.loads(line)
    attrs = event["attributes"]
    print(
        event["iteration"],
        event["event_type"],
        attrs.get("tool_name", ""),
        attrs.get("reason", ""),
        attrs.get("prompt_chars", ""),
    )
PY
```

事件顺序应该是：

```text
run_started
iteration_started -> llm_started -> llm_completed -> decision_made
tool_started -> tool_completed
...（下一轮）
run_terminated
```

几个值得看的字段：

| 字段                              | 含义                 |
| ------------------------------- | ------------------ |
| `run_id` / `trace_id`           | 一次运行的关联 ID         |
| `prompt_chars`                  | 本轮真正发给模型的字符数       |
| `session_chars_before`          | 压缩前 Session 的总字符数  |
| `summary_covered_message_count` | 摘要已经覆盖多少条旧消息       |
| `arguments` / `result_summary`  | 工具参数与结果摘要（已脱敏、已截断） |

### 4.2 终端里的耗时汇总

每次运行结束，CLI 会在 stderr 打印一行：

```text
耗时：总计 39.8s｜模型 39.7s（2 次）｜工具 0.06s（1 次）｜轮次 2
```

- **总计**：调用方用墙钟测的整轮耗时，包含 Context 组装、Session 读写。
- **模型**：所有 `llm_completed.duration_ms` 之和，次数就是模型请求数。
- **工具**：所有 `tool_completed.duration_ms` 之和，次数就是实际执行的工具数。
- 总计 ≥ 模型 + 工具，差额是本地开销。

交互模式下每轮回答后都会打印一行，方便你判断“是模型慢，还是工具慢”。

JSONL 里本来就有每次调用的 `duration_ms`。想看逐次明细：

```bash
uv run python - <<'PY'
import json
for line in open("logs/t-timing.jsonl", encoding="utf-8"):
    event = json.loads(line)
    if event["event_type"] in {"llm_completed", "tool_completed"}:
        attrs = event["attributes"]
        print(
            event["iteration"],
            event["event_type"],
            attrs.get("tool_name", ""),
            f'{attrs.get("duration_ms", 0):.1f}ms',
        )
PY
```

典型输出：

```text
1 llm_completed  16810.6ms
1 tool_completed calculator 21.8ms
2 llm_completed  20512.1ms
```

这就直接证明了：慢在模型，不在计算。

### 4.3 Session 文件

```bash
ls .yjy-sessions
```

文件名是 `sha256(session_id).json`，不是直接拼 session_id。这样即使 ID 里有 `/` 或  
`..`，也不会越出目录。

打开任意一个文件，能看到：

```json
{
  "session_id": "demo-all",
  "messages": [...],
  "tool_calls": [...],
  "tool_results": [...],
  "todos": [...],
  "summary": null
}
```

`messages` 是给模型看的历史，`tool_calls` / `tool_results` 是结构化审计数据，  
`todos` 是 Session 级状态，`summary` 是压缩后的摘要。

---

## 5. 长上下文：最简单的压缩怎么观察

默认预算在 `ContextConfig` 里：

| 配置                      | 默认值   | 作用        |
| ----------------------- | ----- | --------- |
| `max_history_messages`  | 20    | 最近保留多少条消息 |
| `max_tool_result_chars` | 2000  | 单条工具结果上限  |
| `max_summary_chars`     | 4000  | 摘要长度上限    |
| `max_context_chars`     | 24000 | 整轮模型输入上限  |

压缩策略只有一句话：**超出最近窗口的旧消息，合并成一段摘要；摘要本身也有长度上限。**

### 5.1 造一个长 Session

```bash
uv run python - <<'PY'
from yjy_agent_runtime.domain import Message, MessageRole
from yjy_agent_runtime.infrastructure.session import JsonFileSessionStore

store = JsonFileSessionStore(".yjy-sessions")
sid = "long-demo"
store.get_or_create(sid)
store.append_message(sid, Message(MessageRole.USER, "请记住：验收代号是 ORANGE-42。"))
for i in range(40):
    role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
    store.append_message(sid, Message(role, f"第{i:02d}条：" + "背景资料" * 300))
print("messages:", len(store.get_or_create(sid).messages))
PY
```

### 5.2 追问早期事实

```bash
uv run yjy-agent-runtime --session-id long-demo \
  "我最开始告诉你的验收代号是什么？只回答代号。"
```

预期：回答 `ORANGE-42`。

再看 trace 里的三个数：

```bash
uv run python - <<'PY'
import json
for line in open("logs/long-demo.jsonl", encoding="utf-8"):
    event = json.loads(line)
    if event["event_type"] == "iteration_started":
        attrs = event["attributes"]
        print({
            "session_chars_before": attrs["session_chars_before"],
            "prompt_chars": attrs["prompt_chars"],
            "summary_chars": attrs["summary_chars"],
            "covered": attrs["summary_covered_message_count"],
        })
PY
```

我实测的一组数：Session 约 48,000 字符，压缩后模型输入约 23,000 字符，摘要 4,000  
字符，覆盖 26 条旧消息，早期代号仍然保留。

### 5.3 当前压缩的边界

- 摘要是**抽取式**的：把旧消息按行拼接后截断，不做语义总结。够用，但不是“智能摘要”。
- **单条消息本身超过约 2.2 万字符**时，没有旧历史可压，会以 `context_error` 结束。  
  这是当前唯一能稳定复现的运行失败；正常对话不会触发。
- 压缩只在 Context 构建时发生，不改写 Session 里的原始消息。

---


## 6. 代码阅读顺序

不要从 `builder.py` 开始，它是最绕的一个。按下面顺序读，每一步只回答一个问题。

| 顺序 | 文件                                                              | 读什么                                                      | 回答什么问题                           |
| -- | --------------------------------------------------------------- | -------------------------------------------------------- | -------------------------------- |
| 1  | `src/yjy_agent_runtime/domain/models.py`                        | `Message`、`ToolCall`、`ToolResult`、`Session`、`RunRequest` | 系统里有哪些稳定对象？哪些非法状态在构造期就被拒绝？       |
| 2  | `src/yjy_agent_runtime/agent/ports.py`                          | 六个 Protocol                                              | AgentLoop 需要哪些能力？为什么依赖抽象而不是具体实现？ |
| 3  | `src/yjy_agent_runtime/agent/loop.py`                           | 只读 `run()`                                               | 一轮循环的 6 个阶段是什么？三种出口分别在哪？         |
| 4  | `src/yjy_agent_runtime/infrastructure/context/builder.py`       | 只读 `build()`                                             | Context 的顺序是什么？超预算时先压什么、后压什么？    |
| 5  | `src/yjy_agent_runtime/infrastructure/llm/openai_compatible.py` | `decide()` + `_decode_decision()`                        | 模型返回的 JSON 怎么变成领域对象？错误怎么分类？      |
| 6  | `src/yjy_agent_runtime/infrastructure/tools/registry.py`        | 全部                                                       | 工具怎么注册、怎么按名字查找？                  |
| 7  | `src/yjy_agent_runtime/infrastructure/tools/executor.py`        | `execute()`                                              | 参数校验、超时、异常怎么变成 `ToolResult`？     |
| 8  | `src/yjy_agent_runtime/infrastructure/tools/builtin.py`         | 四个工具类                                                    | 每个工具的 Schema 和实现分别是什么？           |
| 9  | `src/yjy_agent_runtime/infrastructure/session/in_memory.py`     | 全部                                                       | 不可变快照怎么更新？                       |
| 10 | `src/yjy_agent_runtime/infrastructure/session/file.py`          | `_save()`                                                | 怎么保证写文件不会留下半个 JSON？              |
| 11 | `src/yjy_agent_runtime/infrastructure/trace/*.py`               | `redaction.py`、`jsonl.py`                                | Trace 怎么写、敏感字段怎么脱敏？              |
| 12 | `src/yjy_agent_runtime/interfaces/cli.py`                       | `build_loop()` + `main()`                                | 具体实现在哪里装配？退出码怎么定？                |
| 13 | `tests/test_agent_loop.py`、`tests/test_end_to_end.py`           | 全部                                                       | 正常路径和异常路径分别怎么测？                  |

读 `loop.py` 时，建议对照 `docs/architecture-layers.md` 第 2 节的数据流，一行一行对。

---

## 7. 常见报错对照

| 现象                                                      | 原因                   | 处理                                                          |
| ------------------------------------------------------- | -------------------- | ----------------------------------------------------------- |
| `LLMConfigurationError: LLM_BASE_URL must not be empty` | 当前目录没有 `.env`，或变量名写错 | 在项目根目录创建 `.env`，或 `--env-file` 指定                           |
| `LLM service returned HTTP 401`                         | Key 无效或过期            | 换 Key；不要写进代码或 Git                                           |
| `LLMNetworkError: LLM request timed out`                | 网络慢或网关排队             | 调大 `LLM_TIMEOUT_SECONDS`，或换网络                               |
| `2 skipped`（`pytest -m integration`）                    | 没有把 `.env` 传进 pytest | `uv run --env-file .env pytest -m integration -q`           |
| `UnicodeEncodeError: ... surrogates not allowed`        | 终端/输入法把非法 UTF-8 字节解码成 `\udcXX` | 入口现在会提示“输入包含无法解码的字符”；把终端编码设为 UTF-8 后重试 |
| `context_error`                                         | 单条输入超过 Context 预算    | 拆短输入；后续会加友好提示                                               |
| `PermissionError: mkdir .../pytest-of-root`             | 沙箱不允许写系统临时目录         | `TMPDIR=/tmp/xxx uv run pytest -q --basetemp=/tmp/xxx/base` |
| `search` 返回“未找到”                                        | 查询词和三条 mock 文档完全不匹配  | 换关键词，例如 `session`、`context`、`agent loop`                    |
| 运行输出 `终止原因：max_iterations`                              | 模型一直调工具没收敛           | 调大 `--max-iterations`，或把任务拆小                                |

---

## 8. 一次完整验收清单

按顺序打勾，全部通过就说明你本地链路是通的：

- [x] `uv run yjy-agent-runtime --session-id hello "你好"` 退出码 0
- [x] `calculator` 算出 `137 * 29 = 3973`
- [x] `search` 命中 `Session 必须显式携带 session_id`
- [x] `todo add` 后，同 session `todo list` 能看到
- [x] 另一个 session `todo list` 返回“当前没有待办”
- [x] 三工具串联任务退出码 0，trace 中有两次以上 `tool_completed`
- [x] 同 session 追问能同时引用计算结果和待办
- [x] 长历史压缩后仍能回答 `ORANGE-42`
- [x] `logs/<session>.jsonl` 存在，且没有 API Key
- [x] `uv run pytest -q` 全绿

---

## 9. 已知边界（先知道，不用现在修）

1. 单条超长输入会 `context_error`（见 5.3）。
2. 工具调用和工具消息分两次写 Session，进程在两次写之间崩溃会留下半条交互。
3. 工具超时只是“停止等待”，后台线程可能继续执行；对 `todo` 有理论上的晚写风险。
4. `search` 只有三条固定文档，是 mock，不是真实搜索。
5. CLI 默认一次进程一条消息；`--interactive` 支持同进程多轮，但没有流式输出，每轮仍要等模型完整返回。

这些都不影响题目要求的演示，但读代码时会遇到，先在这里说明，避免误以为是 bug。

---

## 10. 建议的下一步

1. 你按第 3、5、8 节亲手跑一遍，确认输出和文档一致。
2. 我陪你按第 6 节的顺序读 `loop.py` 和 `builder.py`，一次只讲一个函数。
3. 如果决定继续收尾：先修“单条超长输入”的友好提示，再把三份状态文档收敛成一份。
4. 最后补真实 LLM 验收记录，提交并 push GitHub。
