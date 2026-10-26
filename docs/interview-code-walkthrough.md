# 面试带读：逐行讲清一个从零实现的 Agent Runtime

“Agent 就是一个循环。”

这句话是对的，也是面试里最危险的一句话。因为它太容易说，面试官听完只会追问一句：“那你自己写的循环，和 `while True: response = llm(...)` 有什么区别？”

这个仓库就是我准备好的答案：**3,832 行 Python，运行时只依赖 `httpx` 和 `jsonschema`，没有 LangGraph、没有 LangChain、没有 OpenHands。** 循环、解析、工具调度、Session、压缩、Trace 全部自己实现。

但“自己实现”本身不是加分项，讲清楚**每一处为什么这么写、边界在哪里、哪里还没做好**才是。所以这份文档不是功能介绍，是一份**带读脚本**：按面试的节奏，一个文件一个文件往下走，每停一处都告诉你——面试官会问什么，你该指哪一行。

> **核对基线**：`f22ad98`（2026-10-27），以及其后工作区里正在收尾的真实集成测试用例。这个仓库还在演进，行号会随提交漂移，所以本文的引用方式是**以类名 / 函数名为主锚点，行号只作定位参考**。如果行号对不上，按函数名搜索即可。

---

## 零、先给他一个 30 秒的定位

不要一上来就打开 `loop.py`。先给三个数字和一个结构图，让他知道自己在看什么规模的东西。

| 维度 | 事实 |
| --- | --- |
| 代码规模 | 源码 3,832 行，28 个 `.py` 文件 |
| 运行时依赖 | 只有 `httpx`（HTTP）和 `jsonschema`（参数校验） |
| 测试 | 150 个测试通过；真实 LLM 测试单独标记、默认跳过 |
| 分层 | `interfaces → infrastructure → agent → domain`，单向依赖，有测试守护 |

```text
src/yjy_agent_runtime/
├── domain/          领域层：不可变数据 + 构造期不变量，零 I/O
├── agent/           应用层：AgentLoop（384 行）+ 6 个端口协议
├── infrastructure/  基建层：LLM / Session / Tool / Context / Trace 的实现
└── interfaces/      入口层：CLI、.env 加载、对象装配
```

**带读原则**，先讲给他听，这决定了后面 40 分钟的节奏：

1. **从下往上读，不从 `main()` 往下读。** 先看“系统里有哪些稳定对象”，再看“循环怎么用它们”。反过来读会陷在装配代码里出不来。
2. **每一处都先讲约束，再讲实现。** 面试官真正在意的是你有没有想过“什么情况不该发生”，而不是你写了多少行。
3. **主动交代没做好的地方。** 这一条后面第八节单独讲，它是整场面试的胜负手。

---

## 一、第一幕：领域层——把非法状态挡在构造期

**入口文件**：`src/yjy_agent_runtime/domain/models.py`（302 行）

**这一幕要回答的问题**：你的系统里有哪些不可动摇的事实？哪些错误你根本不允许它被构造出来？

先给面试官一句话定调：

> 这个文件里没有任何 `if` 业务逻辑，只有 `__post_init__` 里的校验。我想做到的是：**核心循环里不需要再防御这些对象**，因为它们不可能处于非法状态。

### 1.1 `Message`：一个字段的三条规则

```python
@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("message content must not be empty")
        if self.role is MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.role is not MessageRole.TOOL and self.tool_call_id is not None:
            raise ValueError("only tool message may contain tool_call_id")
```

逐条讲：

| 代码 | 他在确认什么 |
| --- | --- |
| `frozen=True` | 消息一旦进入 Session 就不可变。Session 快照会在 Store 之间传递，如果消息能被原地改，某轮循环就可能悄悄改写历史，Trace 和实际输入会对不上 |
| `slots=True` | 长对话里 `Message` 实例很多，省内存；更重要的是防止有人 `msg.extra = x` 挂私有状态上去 |
| `content` 非空 | 空内容进 Context 是纯噪音，且 OpenAI 兼容接口会直接 400 |
| TOOL 必须有 `tool_call_id` | 这是“结果能对回哪次调用”的唯一依据 |
| 非 TOOL 不许有 `tool_call_id` | 反向约束。只写前半条，就会出现 USER 消息带着 call_id 的脏数据 |

**这里有个刻意的取舍，值得主动说**：`content` 不允许为空，所以工具返回空字符串时，执行器会把它转成 `InvalidToolResultError` 而不是塞一条空消息进去。这不是遗漏，是让“空结果”成为一个显式的错误事实。

### 1.2 `SessionSummary`：覆盖范围不能超过历史长度

```python
@dataclass(frozen=True, slots=True)
class SessionSummary:
    content: str
    covered_message_count: int

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("session summary content must not be empty")
        if self.covered_message_count < 1:
            raise ValueError("covered message count must be at least 1")
```

再看它和 `Session` 的联动——**这是整个压缩机制的地基**：

```python
@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    messages: tuple[Message, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    todos: tuple[str, ...] = ()
    summary: SessionSummary | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session id must not be empty")
        if self.summary is not None and self.summary.covered_message_count > len(
            self.messages
        ):
            raise ValueError(
                "summary cannot cover more messages than the session contains"
            )
```

**为什么这一行校验值钱？**

因为 `covered_message_count` 是“摘要和最近窗口的分界线”：`messages[:covered]` 已被摘要替代，不再发给模型；`messages[covered:]` 是最近窗口，原样保留。

如果这个数被错误地推进到超过 `len(messages)`，就会有两种灾难：

- 分界线越界，`messages[covered:]` 变成空切片 → **模型看不到任何最近消息**，包括用户刚说的话；
- 或者反过来，摘要声称覆盖了并不存在的消息 → 那批消息既不在摘要里（因为没被真正摘要过），也不在最近窗口里（因为被 covered 挡掉了）→ **永久丢历史**。

这一行校验把这两种状态变成了“构造不出来”。面试时可以说：**这不是防御式编程，是把一个不变量写成了类型的一部分。**

### 1.3 `ToolResult`：三个维度，缺一个就说不清

`ToolResult` 现在有六个字段，比一般实现多了一个 `timeout_semantics`：

```python
@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolResultStatus
    content: str
    error_type: str | None = None
    # 执行器给出的超时语义，供 Trace 与调用方判断“超时之后是否可能还有副作用”。
    timeout_semantics: TimeoutSemantics | None = None

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ValueError("tool result requires call id and tool name")
        if not self.content:
            raise ValueError("tool result content must not be empty")
        _ensure_no_surrogates(self.content, "tool result content")
        if self.status is ToolResultStatus.ERROR and not self.error_type:
            raise ValueError("error tool result requires error_type")
        if self.status is ToolResultStatus.SUCCESS and self.error_type is not None:
            raise ValueError("successful tool result must not contain error_type")
```

最后两行是**成对**的，很多人只写第一行。只写第一行的后果：一个 `status=success` 但带着 `error_type="TimeoutError"` 的结果会流进 Context，模型看到“成功”又看到错误类型，只能瞎猜。

而 `timeout_semantics` 解决的是第三个维度——**“失败”不等于“没发生”**：

```python
class TimeoutSemantics(StrEnum):
    SYNCHRONOUS = "synchronous"        # 有副作用工具同步执行，根本没有超时
    ABANDONED_SAFE = "abandoned_safe"  # 无副作用工具超时，线程被放弃但无害
    COOPERATIVE = "cooperative"        # 有副作用工具带截止时间，写入前会自我放弃
```

为什么必须有这个字段？因为 `status=error` + `error_type=TimeoutError` 这个组合是**歧义**的：

- 对 `calculator`，它意味着“没算出来，什么都没有发生”；
- 对 `todo add`，它可能意味着“我告诉你失败了，但两秒后待办真的写进去了”。

同一个 `ToolResult`，两种截然不同的外部事实。不把这个区别显式写出来，调用方只能猜。这一段是第五幕的主线，先在这里埋下。

另外注意 `_ensure_no_surrogates`：终端把非法 UTF-8 字节按 `surrogateescape` 解码会产生 `\udcXX`，这种字符无法再编码成 UTF-8，写 Session 文件时才炸就太晚了。所以在领域对象构造期就拒绝——**错误发现得越早，代价越小。**

而且失败结果**不抛异常、照样写回 Session**——这是设计选择，不是偷懒：

> 工具失败是一个**可观察的事实**，不是程序崩溃。模型需要看到“这个工具报错了、错在参数”，才能决定换参数重试还是换方案。把异常直接抛出去终止循环，等于剥夺了模型的纠错能力。

### 1.4 `RunResult`：答案和终止原因绑死

```python
if (
    self.termination_reason is TerminationReason.FINAL_ANSWER
    and not self.answer
):
    raise ValueError("final-answer result requires answer")
if (
    self.termination_reason is not TerminationReason.FINAL_ANSWER
    and self.answer is not None
):
    raise ValueError("non-final result must not contain answer")
```

`TerminationReason` 是受控枚举，七个值：

```text
final_answer / max_iterations / llm_error / context_error
/ tool_error / invalid_decision / cancelled
```

**为什么不用字符串、不用 `None` 表示失败？**

因为调用方（CLI、以后的 HTTP 入口）需要按原因决定退出码和提示文案。`None` 无法区分“模型没收敛”和“网络断了”，而这两种情况对用户的建议完全不同：前者该拆小任务，后者该检查配置。

### 1.5 面试官在这一幕会问什么

**Q：为什么全用 `frozen=True`？性能不会有问题吗？**

> 有成本，但我接受。`frozen` 的 `__setattr__` 开销在微秒级，而这个系统 99% 的时间花在等 LLM 返回（实测单次 20 秒左右）。用这点开销换“历史不可能被中途改写”，非常划算。真正需要可变性能的地方是 Context 组装，那里我用的是本地变量和 list，不碰领域对象。

**Q：`MappingProxyType` 是不是多此一举？**

> `ToolCall.arguments` 是从模型 JSON 里解出来的 dict。如果直接存 dict，调用方拿到的引用就能改——执行器校验用的是 A 参数，实际执行用的是被改过的 B 参数，这种 bug 极难查。`MappingProxyType(dict(...))` 先复制再包只读，代价是一次浅拷贝。

**Q：为什么不给 `Message` 加 `id`？**

> 因为当前的定位是“一个 Session 内的有序序列”，位置本身就是身份，`covered_message_count` 就是按位置算的。加 ID 是为了分叉和跨分支引用，那是另一个能力，我没做（第八节会说）。**不加，是因为现在没有地方需要它**——不是忘了。

---

## 二、第二幕：端口层——循环到底依赖什么

**入口文件**：`src/yjy_agent_runtime/agent/ports.py`（162 行）

**这一幕要回答的问题**：如果明天换掉 LLM 厂商、换掉存储、换掉工具实现，`loop.py` 需要改几行？

答案是：**一行都不改。**

```text
ContextProvider   build(request, tool_results) -> Sequence[Message]
HistorySummarizer summarize(prev_summary, messages) -> str
LLMClient         decide(messages) -> AgentDecision
ToolExecutor      execute(session_id, call) -> ToolResult
SessionStore      get / get_or_create / append_message / append_tool_interaction
                  / set_summary / append_todo / list_todos
TraceRecorder     record(event) -> None
```

六个 Protocol，全部 `typing.Protocol` + 结构类型，不需要显式继承。`loop.py` 的 `__init__` 只接收这六个抽象：

```python
def __init__(
    self,
    *,
    context_provider: ContextProvider,
    llm_client: LLMClient,
    session_store: SessionStore,
    tool_executor: ToolExecutor,
    trace_recorder: TraceRecorder,
    config: LoopConfig | None = None,
) -> None:
```

**这里有一个细节值得指出来**：`ToolDefinition` 和 `Tool` 这两个“工具的契约”没有放在 `infrastructure/tools/`，而是放在了 `agent/ports.py`。

为什么？因为 `ContextBuilder` 需要把工具的名称、描述、参数 Schema 注入系统提示——也就是说，**Context 也要消费工具定义**。如果契约放在基建层，`infrastructure/context` 就得 import `infrastructure/tools`，两个基建子模块横向耦合，分层就破了。

> 判断契约该放哪一层，不看“谁实现它”，看“谁需要读它”。读的人在上层，契约就在上层。

这个改动是有历史痕迹的：`infrastructure/tools/registry.py` 里现在还留着一行 re-export，注释写的是“为兼容既有导入路径”。

### 2.1 面试官会问什么

**Q：为什么用 `Protocol` 而不是 `ABC`？**

> `Protocol` 是结构化子类型，实现方不需要知道协议存在、不需要继承。这对测试特别友好：`tests/test_agent_loop.py` 里的假 LLM 就是一个普通类，有 `decide` 方法就行，不用 `class FakeLLM(LLMClient)`。而且 `ABC` 会引入 MRO 和初始化顺序的复杂度，这里完全没有必要。

**Q：这和依赖倒置是什么关系？**

> 接口由**使用方**提出，不是由实现方定义。`LLMClient` 在 `agent/` 里，是因为循环先提出“我需要一个能根据消息返回决策的东西”；`OpenAICompatibleLLMClient` 在 `infrastructure/` 里，是去满足这个要求。方向是 `infrastructure → agent`，符合依赖倒置。

**Q：为什么不用 LangGraph？**

> 因为题目红线要求自己实现主循环。但更实际的理由是：LangGraph 把“模型决定调用什么工具并自动循环”封装掉了，我用它的话，这个项目里最值得讲的那 384 行就不存在了。**用框架不是错，但用框架之后要能讲清楚框架替你做了什么**——我现在能讲清楚，因为每一步都是我自己写的。

---

## 三、第三幕：主循环——384 行里真正的 6 个阶段

**入口文件**：`src/yjy_agent_runtime/agent/loop.py`，核心是 `run()`，第 76–292 行。

这是全场面试的主战场。建议**先讲骨架，再逐个展开**，不要一上来就逐行念。

### 3.1 骨架：三种出口，没有第四种

```python
def run(self, request: RunRequest) -> RunResult:
    run_id = str(uuid4())
    trace_id = str(uuid4())
    tool_results: list[ToolResult] = []

    self._session_store.get_or_create(request.session_id)
    self._session_store.append_message(
        request.session_id,
        Message(MessageRole.USER, request.user_input),
    )
    self._record_trace(TraceEventType.RUN_STARTED, ...)

    for iteration in range(1, self._config.max_iterations + 1):
        # 1. 构建 Context
        # 2. 请求 LLM
        # 3. 分叉：最终回答 → 返回；工具调用 → 执行并写回 → 下一轮
        ...

    # 4. 循环走完了还没答案
    return RunResult(..., termination_reason=TerminationReason.MAX_ITERATIONS, ...)
```

三种出口，全部返回 `RunResult`：

| 出口 | 触发位置 | `termination_reason` |
| --- | --- | --- |
| 拿到最终回答 | 第 168 行 `isinstance(decision, FinalAnswerDecision)` | `final_answer` |
| 依赖报错 | `_terminate_after_dependency_error()`，第 292 行 | `llm_error` / `context_error` / `tool_error` / `invalid_decision` |
| 轮次用尽 | 循环自然结束，第 286 行 | `max_iterations` |

**没有第四种，也没有“悄悄返回 `None`”。** 这句话一定要说，因为它是 `RunResult.__post_init__` 里那两条校验存在的理由。

### 3.2 为什么必须先写 USER，再建 Context

```python
# 1. 先落盘用户输入，再构建 Context。ContextBuilder 只从 Session 读，
#    所以必须先写；否则本轮问题不会出现在模型输入里。
self._session_store.get_or_create(request.session_id)
self._session_store.append_message(
    request.session_id,
    Message(MessageRole.USER, request.user_input),
)
```

**这两行的顺序如果反了，会出一个非常隐蔽的 bug**：Context 里没有本轮问题，模型只能看着历史瞎答，而且不报错、不崩溃，只是答案莫名其妙。

代码里这句注释不是我事后补的，是因为这个坑真的踩过。

顺带解释为什么是 `get_or_create` 而不是 `get`：新 Session 第一次运行时不存在，`get` 返回 `None`，后面 `append_message` 会抛 `SessionNotFoundError`。

### 3.3 为什么每一轮都要重新构建 Context

```python
for iteration in range(1, self._config.max_iterations + 1):
    # 2. 每一轮都重新构建 Context：上一轮的工具结果已经写进 Session，
    #    模型必须看到它，才能决定"继续调工具"还是"给出最终回答"。
    session_before = self._session_store.get(request.session_id)
    try:
        messages = self._context_provider.build(request, tuple(tool_results))
    except Exception as error:
        return self._terminate_after_dependency_error(...)
```

这是**整个架构的支点**，值得多花两分钟讲：

> 模型是无状态的。它不“记得”上一轮，它每轮看到的都是重新拼出来的快照。所以工具结果必须**先落盘到 Session**，下一轮再由 `ContextBuilder` 重新读、重新压缩、重新拼。
>
> 这个设计的代价是每轮都要重新组装一次 Context（有压缩开销）；收益是**恢复能力**：进程崩了重启，只要 Session 在，下一轮照样能拼出完整输入。如果反过来把 messages 缓存在循环的局部变量里，进程一死全丢。

`session_before` 和 `session_after` 只用于 Trace 观察压缩效果，不参与业务判断——这个区分也要说清楚，否则面试官会以为你在循环里做了隐式状态。

### 3.4 依赖错误为什么不往上抛

```python
try:
    decision = self._llm_client.decide(messages)
except Exception as error:
    return self._terminate_after_dependency_error(
        request=request,
        run_id=run_id,
        trace_id=trace_id,
        iteration=iteration,
        reason=TerminationReason.LLM_ERROR,
        stage="llm",
        error=error,
        duration_ms=self._duration_ms(llm_started),
    )
```

注意这里 `except Exception` 是**故意的宽捕获**，但捕获之后做了三件事，不是吞掉：

1. 记一条 `dependency_failed` Trace，带 `stage` / `error_type` / `error_message`；
2. 返回带 `TerminationReason` 的 `RunResult`；
3. 由入口层决定退出码（CLI：`1`）。

> 核心循环不抛异常，是因为它要服务多种入口。CLI 需要退出码，HTTP 入口需要 JSON 响应，如果循环直接抛，每个入口都得重新 catch 一遍、重新分类一遍。把“错误分类”做在循环里，入口层只做“翻译”。

**但有一个例外必须讲**：工具执行器自身抛出的异常（不是工具内部失败）会终止循环为 `tool_error`；而工具内部失败（未知工具、参数非法、工具抛错、超时）会被 `RegistryToolExecutor` 转成 `ToolResult(status=error)`，**交给模型继续判断**。

这个区分是：

| 情况 | 谁处理 | 结果 |
| --- | --- | --- |
| 工具内部失败 | 模型 | 循环继续，模型看到错误后调整 |
| 执行器自身崩了 | 循环 | 终止，`tool_error` |

### 3.5 工具交互为什么必须一次写完

```python
# 4. 一次工具交互必须在同一次写入里落盘：调用、结果和 TOOL 消息
#    三者是同一件事。分成两次写，进程在中间崩溃就会留下"有结果
#    没消息"的半条交互，模型看到的历史会自相矛盾。
self._session_store.append_tool_interaction(
    request.session_id,
    call,
    tool_result,
    Message(
        MessageRole.TOOL,
        tool_result.content,
        tool_result.call_id,
    ),
)
```

**这一次调用同时写三样东西**：`ToolCall`、`ToolResult`、带 `tool_call_id` 的 `TOOL` 消息。

**为什么不能分两次写**——这是这里最值得讲的设计点：

> 一次工具交互是**一个事实**，不是两个。如果先写调用和结果、再写消息，中间崩溃就会留下“有结果没消息”的半条交互：`ContextBuilder` 组装时会看到一次调用、一份结果，但历史里没有对应的 TOOL 消息，模型看到的历史自相矛盾；审计也没法还原当时发生了什么。
>
> 单次文件写入是原子的（临时文件 + `fsync` + `os.replace`），所以只要把三样东西放进**同一次**写入，原子性就自动覆盖了它们。

而且接口层还堵住了另一条路：`append_message` **拒绝**单独追加 TOOL 消息。

```python
def reject_standalone_tool_message(message: Message) -> None:
    """拒绝单独追加的 TOOL 消息。

    TOOL 消息必须通过 ``append_tool_interaction`` 与调用、结果一起落盘；单独写
    会让"半条交互"重新变得可表示。
    """
    if message.role is MessageRole.TOOL:
        raise ValueError(
            "tool messages must be appended together with their tool interaction"
        )
```

这句话值得单独念一遍：**不是靠文档约定，是让非法状态在接口层不可表示。** 有人（包括未来的我）想偷懒分两次写，代码会直接拒绝。

配套的 `session/invariants.py` 把“什么算一致”收敛成纯函数，内存 Store、文件 Store 和读取校验共用同一套规则：

```python
def assert_tool_interactions_consistent(session: Session) -> None:
    """检查快照里的工具交互是否完整：调用 ID 唯一，消息必须能对上调用与结果。"""
```

**为什么要单独一个模块**：如果内存 Store 和文件 Store 各写一份校验，两份规则迟早漂移——一边允许重复 `call_id`，另一边拒绝，测试就会在两种 Store 上给出不同结果。把规则抽出来，两处 import 同一个函数，漂移就不可能发生。

### 3.6 面试官在这一幕会问什么

**Q：为什么用 `for` 不用 `while`？**

> 因为轮次上限是唯一的自然终止条件之一，`for iteration in range(1, max+1)` 让“最多 8 轮”这件事在语法上就成立，不需要额外的计数器变量，也不会出现忘了 `break` 的死循环。`iteration` 还能直接进 Trace。

**Q：多个工具调用是并行还是串行？**

> 串行。`for call in decision.tool_calls` 顺序执行。理由是：确认调用之间没有数据依赖、没有写入冲突、没有顺序要求之前，并行只是增加复杂度。而且 `todo add` 和 `todo list` 如果并行，结果就是不确定的。

**Q：`tool_results` 列表为什么在循环外定义？**

> 因为它是“本次运行的产物”，跨轮累积。但它**不参与业务判断**——真正决定模型看到什么的是 Session。`ContextBuilder._tool_envelopes()` 会用它做一次交叉校验：只有当前 Session 已记录的调用所对应的结果才被接受，防止调用方把别的 Session 的结果通过 `build()` 参数带进来。

**Q：如果模型连续调同一个工具 8 次都不收敛呢？**

> 会在第 8 轮结束后返回 `max_iterations`，CLI 打印“终止原因：max_iterations”，退出码 1。这是**明确失败**，不是静默截断。用户看到这个提示就知道该把任务拆小，而不是以为模型答完了。

---

## 四、第四幕：Context 组装——三层压缩

**入口文件**：`src/yjy_agent_runtime/infrastructure/context/builder.py`（614 行，全项目最绕的一个文件）

**这一幕要回答的问题**：历史一直涨，你怎么保证发给模型的东西不超预算，同时又不丢关键信息？

先给结论，再拆实现。**顺序永远是先讲结论。**

### 4.1 输出顺序是固定的四段

```text
[system]   角色约束 + 工具名称/描述/参数 Schema/副作用标记
[system]   较早历史摘要            ← 可选，受 max_summary_chars
[最近消息 ...]                     ← 受 max_history_messages
[TOOL 信封 ...]                    ← 受 max_tool_result_chars
```

对应 `_assemble()`（第 361 行）。顺序不能乱：系统约束必须第一段，摘要必须紧跟系统约束、在具体消息之前，否则模型会把摘要当成“用户刚说的话”。

### 4.2 `covered` 是分界线，不是计数器

```python
original_summary = session.summary
original_covered = (
    original_summary.covered_message_count if original_summary else 0
)
...
# covered 是"摘要与最近窗口的分界线"：
#   messages[:covered] 已被摘要替代，不再进入模型输入；
#   messages[covered:] 是最近窗口，原样保留。
# 它只能单调递增，否则同一批消息会被重复摘要。
covered = original_covered
```

**为什么强调“单调递增”？** 因为摘要是有损的。同一批消息被摘要两次，不是“更准确”，而是“在已经失真的文本上再失真一次”。所以 `covered` 只允许往前走，永远不回头。

### 4.3 三层预算，从粗到细

```python
# 第一层：消息条数预算。目标是"移出最近窗口"的旧消息前缀。
target_count = min(
    len(session.messages),
    max(0, len(session.messages) - self._config.max_history_messages),
)
```

```python
# 第二层：总字符预算。仍超预算时按完整消息组继续摘要旧消息。
while self._context_chars(context) > self._config.max_context_chars:
    next_covered = self._next_group_boundary(session.messages, covered)
    last_group_start = self._last_group_start(session.messages)
    if next_covered <= covered or next_covered > last_group_start:
        break
    ...
```

```python
# 第三层：最近消息组不能拆；若仍超预算，只缩短摘要的"展示长度"。
if self._context_chars(context) > self._config.max_context_chars and (
    summary_content is not None
):
    ...
    allowed_summary_chars = (
        self._config.max_context_chars - fixed_chars - len(prefix)
    )
```

**用数字讲一遍，面试官立刻能听懂：**

假设 `max_context_chars = 24000`，历史 100 条、每条约 1000 字符（总共约 10 万字符）：

1. 第一层：`max_history_messages = 20`，所以前 80 条被移出窗口，整批摘要 → 输入从 10 万降到约 2 万。
2. 如果还是超（比如系统提示 + 工具定义本身就很大），第二层按消息组继续往摘要里推，一次推一组，直到压进 24000。
3. 如果推到“只剩最后一组”还超，第三层缩短摘要的**展示长度**，最近消息一个字不动。
4. 如果缩到摘要都没了还超 → 抛 `ContextBudgetError`，运行以 `context_error` 结束。

**第 4 步是关键取舍，必须主动说**：

> 单条消息本身超过约 2.2 万字符时（比如一次粘贴几万字），没有旧历史可压，会以 `context_error` 结束。这是当前唯一能稳定复现的运行失败。
>
> 为什么不直接截断用户输入？因为截断会把用户的问题改得面目全非，而且模型不知道自己看到的是残缺的。**宁可明确失败，也不静默篡改。**

### 4.4 消息组：工具调用和结果同进同出

```python
@staticmethod
def _message_groups(messages: Sequence[Message]) -> list[tuple[int, int]]:
    """把必须同进同出的消息划为一组。

    - 连续的 TOOL 消息视为同一批工具结果，不能只保留其中一条。
    - 紧邻 TOOL 消息之前的 ASSISTANT 消息视为工具调用声明，与结果同组。
    - 其它消息各自成组。
    """
```

配合 `_group_floor()`（第 512 行）：

```python
@classmethod
def _group_floor(cls, messages: Sequence[Message], target: int) -> int:
    """把目标下标向下取到消息组边界，避免摘要半个工具交互。"""
    if target <= 0:
        return 0
    if target >= len(messages):
        return len(messages)
    for start, end in cls._message_groups(messages):
        if target < end:
            return min(target, start)
    return len(messages)
```

**这段是全场最容易被问倒的地方，讲清楚就能加分。**

问题：如果按条数切，切点正好落在“模型说要用 calculator”和“calculator 返回 3973”之间，会发生什么？

模型会看到：`助手：我要调用 calculator` … 然后没了。它只有两种反应——要么以为工具还在跑，要么重新调一次。前者卡住，后者浪费一轮。

所以 `_group_floor` 把切点**向下取到组边界**：宁可少摘要一条，也不留半个交互。`min(target, start)` 这一句的意思是“如果目标落在这组中间，就退到这组的开头”。

### 4.5 摘要失败不推进覆盖范围

```python
# 只有成功组装后才落盘，失败时覆盖范围保持原值，下一轮可重试。
if summary_content is not None and (
    covered > original_covered
    or (
        original_summary is not None
        and summary_content != original_summary.content
        and covered == original_covered
    )
):
    self._session_store.set_summary(
        session.session_id,
        SessionSummary(summary_content, covered_message_count=covered),
    )
```

摘要先在内存里累积，**整个 Context 成功组装后才写回 Session**。这是“要么全成功，要么什么都不变”的语义。

如果摘要器抛错，`covered` 还是原值，下一轮从同一个位置重试——不会出现“覆盖范围前进了，但摘要没写完”的坏状态。那种状态是灾难性的：那批消息既不在摘要里，也不在最近窗口里，永久消失。

### 4.6 工具信封：模型需要知道的不只是结果文本

```python
payload: dict[str, Any] = {
    "call_id": result.call_id,
    "tool_name": result.tool_name,
    "arguments": arguments,
    "status": result.status.value,
    "result": _render_tool_result(result.content, max_result_chars),
    "error_type": result.error_type,
}
return json.dumps(payload, ensure_ascii=False, sort_keys=True)
```

**为什么要这么麻烦，不直接把结果文本塞给模型？**

因为只有文本时，模型分不清三件事：这是哪个工具返回的、参数是什么、成功了还是失败了。于是它会瞎猜，比如把 `"unknown tool: calc"` 当成计算结果。

信封把这些变成结构化字段，`status` 明确写 `success` / `error`，`error_type` 明确写 `ToolArgumentsValidationError`。

而且注意 `_render_tool_result`（第 597 行）的顺序：

```python
try:
    decoded = json.loads(content)
except json.JSONDecodeError:
    return truncate_text(content, limit)
if not isinstance(decoded, (Mapping, list)):
    return truncate_text(content, limit)
redacted = _redact_arguments(decoded)
return truncate_text(
    json.dumps(redacted, ensure_ascii=False, sort_keys=True),
    limit,
)
```

**先解析完整 JSON、再脱敏、再截断。** 如果顺序反了（先截断再脱敏），一个长 JSON 可能在敏感字段之前就被切断，但前缀里的密钥已经进了 Context。这个顺序是安全要求，不是代码风格。

### 4.7 面试官在这一幕会问什么

**Q：为什么按字符数算，不按 token？**

> 因为 token 计数依赖具体模型的分词器，要引 `tiktoken` 或调供应商接口，会把一个纯离线可测的模块变成网络依赖。字符数是保守估计：中文约 1 字符 ≈ 1 token，英文约 4 字符 ≈ 1 token，所以按字符卡预算对中文场景是偏紧的（安全方向）。
>
> 真要精确，应该给 `ContextConfig` 加一个 `count_tokens: Callable` 注入点——这也是我知道的改进方向，但现在没做。

**Q：摘要器是抽取式的，质量够吗？**

> 不够，这是明确的能力边界。`ExtractiveHistorySummarizer` 只是把旧消息按角色拼接后截断，不做语义总结。它能保住“用户说过什么、工具返回了什么”这些字面事实，但保不住“这些事实之间的因果关系”。
>
> 我把它做成 `HistorySummarizer` 协议，就是为了以后换成 LLM 摘要器时不动 `ContextBuilder`。**现在的选择是“可预测、零成本、可离线测试”，不是“效果最好”。**

**Q：会不会把重要信息压没了？**

> 会，而且我知道会。所以我做了三件事兜底：
> 1. 摘要保留首尾——`truncate_summary` 首尾各占一半预算，最早的事实和最近的进展都留着；
> 2. 待办不进摘要——`todo` 是 Session 的结构化状态，`todo list` 直接读，不依赖压缩；
> 3. 原始消息不动——压缩只改变“这次发什么”，不改写 Session 里的历史，所以理论上还能从原始消息重建。

**Q：多个 Session 会不会串？**

> 不会，因为 `build()` 第一件事就是 `self._session_store.get(request.session_id)`，只读当前 Session。`_tool_envelopes()` 还额外做了一次交叉校验：只接受当前 Session 已记录的调用所对应的结果，防止调用方把别的 Session 的结果通过参数塞进来。这一条有测试覆盖（`tests/test_session_store.py`、`tests/test_context_builder.py`）。

---

## 五、第五幕：工具执行——四道关 + 三种超时语义

**入口文件**：`src/yjy_agent_runtime/infrastructure/tools/executor.py`（290 行）+ `src/yjy_agent_runtime/infrastructure/tools/deadline.py`

**这一幕要回答两个问题**：

1. 模型说“删掉这个目录”，你的系统凭什么不照做？
2. 工具超时了，它到底做没做？—— 第二个问题才是真正区分深浅的地方。

### 5.1 四道关：一条直线

`execute()`（第 66 行）：

```text
1. 查注册表         → 未知工具？返回 ToolResult(error, UnknownToolError)
2. JSON Schema 校验  → 参数非法？返回 ToolResult(error, ToolArgumentsValidationError)
3. 按副作用分流      → 见 5.3，这是唯一有分支的一步
4. 结果检查          → 非字符串 / 空字符串？返回 InvalidToolResultError
```

**每一关的失败都不是抛异常，而是返回结构化 `ToolResult`。** 这是刻意的：模型需要看到失败原因才能调整方案。

参数校验用的是标准 JSON Schema：

```python
validate(
    instance=dict(call.arguments),
    schema=dict(tool.definition.parameters),
)
```

工具自己声明 Schema，执行器统一校验，工具实现里不需要再写一遍 `if "text" not in arguments`。

`TodoTool` 的 Schema 用了 `oneOf`，把 `add` 和 `list` 两种动作的参数约束分开：

```python
"oneOf": [
    {
        "type": "object",
        "properties": {
            "action": {"const": "add"},
            "text": {"type": "string", "minLength": 1},
        },
        "required": ["action", "text"],
        "additionalProperties": False,
    },
    {
        "type": "object",
        "properties": {"action": {"const": "list"}},
        "required": ["action"],
        "additionalProperties": False,
    },
]
```

`additionalProperties: False` 是重要的一行：模型如果多传一个字段（比如 `{"action":"list","text":"x"}`），会直接被拒，而不是被静默忽略。**静默忽略会让模型以为参数生效了。**

### 5.2 真正的分水岭：超时到底意味着什么

先讲一个几乎所有手写 Agent 都会踩的坑。

用线程做超时，最直觉的写法是 `thread.join(timeout)`。但它**只是不再等待，不会杀死线程**。Python 里没有安全的线程强杀机制：`Thread.kill` 不存在，`PyThreadState_SetAsyncExc` 属于未定义行为。

于是就有了这个状态：

```text
执行器 → 模型：这个工具超时了，失败了
两秒后 → 磁盘：待办真的被写进去了
```

模型以为没记上，用户发现记上了。**这不是性能问题，是可解释性问题**——系统对外宣称的事实和实际发生的事实不一致。

这个仓库的处理方式是：**承认 `join` 停不了线程，然后按副作用把工具分成三类。**

### 5.3 分流：副作用决定执行方式

```python
def _dispatch(
    self,
    tool: object,
    session_id: str,
    arguments: object,
) -> tuple[Any, TimeoutSemantics]:
    """按副作用标记选择执行方式，并返回对应的超时语义。"""

    if tool.definition.has_side_effects:  # type: ignore[attr-defined]
        if hasattr(tool, "invoke_with_session_deadline"):
            return self._invoke_cooperatively(tool, session_id, arguments)
        return (
            self._invoke_inline(tool, session_id, arguments),
            TimeoutSemantics.SYNCHRONOUS,
        )

    if hasattr(tool, "invoke_with_session"):
        return (
            self._invoke_with_timeout(
                tool, (session_id, arguments), session_aware=True,
            ),
            TimeoutSemantics.ABANDONED_SAFE,
        )
    return (
        self._invoke_with_timeout(tool, arguments),
        TimeoutSemantics.ABANDONED_SAFE,
    )
```

三条路径，一一对应三种语义：

| 工具类型 | 执行方式 | `timeout_semantics` | 超时后外部状态会变吗 |
| --- | --- | --- | --- |
| 有副作用 + 没有截止时间 | **调用线程同步执行，不设超时** | `SYNCHRONOUS` | 不存在“超时”这种状态 |
| 有副作用 + 实现了截止时间 | 工作线程 + `Deadline` | `COOPERATIVE` | 不会，工具在写入前自己放弃 |
| 无副作用 | 工作线程 + `join(timeout)` | `ABANDONED_SAFE` | 不会，因为它本来就不写东西 |

**第一类：`_invoke_inline`（第 164 行）**

```python
def _invoke_inline(self, tool, session_id, arguments) -> Any:
    """在调用线程同步执行副作用工具，不存在"超时"这种模糊状态。"""
    if hasattr(tool, "invoke_with_session"):
        return tool.invoke_with_session(session_id, arguments)
    return tool.invoke(arguments)
```

没有线程，没有 `join`，没有超时。**它可能很慢，但它不会骗人。** 当前的 `TodoTool` 走的正是这条路——它没有实现 `invoke_with_session_deadline`，所以是同步的。

这是一个刻意的取舍：`todo add` 只是往 JSON 文件里追加一个字符串，慢不到哪里去；如果哪天它变成一次远程 API 调用，就该升级成第三类，而不是退回去用假超时。

**第二类：`_invoke_cooperatively`（第 176 行）**

```python
deadline = Deadline(self._timeout_seconds)
...
worker = Thread(target=invoke, daemon=True)
worker.start()
worker.join(self._timeout_seconds)
if worker.is_alive():
    raise ToolExecutionTimeout(
        f"tool execution exceeded {self._timeout_seconds:g} seconds "
        "and was cancelled at its deadline",
        TimeoutSemantics.COOPERATIVE,
    )
```

看起来还是 `join`，但语义变了：**工具手里握着同一个 `Deadline`**，它在真正写入前会检查，过期就自己放弃。

```python
class Deadline:
    """一次工具调用的截止时间，只能判断是否过期，不能强制中断执行。"""

    __slots__ = ("_expires_at",)

    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("deadline timeout must be positive")
        self._expires_at = monotonic() + timeout_seconds

    def raise_if_expired(self) -> None:
        """在提交副作用之前调用；已过期则抛出 TimeoutError 放弃写入。"""
        if self.expired:
            raise TimeoutError("tool deadline exceeded before commit")
```

这是 Python 里**唯一诚实的超时**：不是“从外面砍断”，而是“在提交点自己检查”。所以 `Deadline` 的文档里写了一条硬约定：

> 真正产生外部副作用之前必须调用 `raise_if_expired()`，且检查与写入之间不应再插入长时间等待。

`monotonic()` 而不是 `time.time()`，是因为后者会被系统时钟调整影响——NTP 校时一跳，你的超时判断就乱了。

**第三类：`_invoke_with_timeout`（第 214 行）**

对 `calculator`、`search` 这类纯函数，超时后线程跑完也就跑完了，不改变任何外部状态，所以可以安全地“放弃”它。语义记为 `ABANDONED_SAFE`——**名字里的 safe 是承诺，不是猜测**：这个工具按定义就不写外部状态。

### 5.4 语义要跟着结果一路走下去

超时不是抛个异常就完了，语义必须能传到调用方。所以定义了一个带语义的异常：

```python
class ToolExecutionTimeout(TimeoutError):
    """执行器判定超时，并携带"是否可能仍有后续副作用"的语义。"""

    def __init__(self, message: str, semantics: TimeoutSemantics) -> None:
        super().__init__(message)
        self.semantics = semantics
```

`execute()` 捕获它时，把 `semantics` 写进 `ToolResult`：

```python
except ToolExecutionTimeout as error:
    return self._error_result(
        call, "TimeoutError", str(error), semantics=error.semantics,
    )
```

然后 `AgentLoop` 把它落到 Trace（`loop.py` 第 277 行）：

```python
timeout_semantics=(
    tool_result.timeout_semantics.value
    if tool_result.timeout_semantics is not None
    else None
),
```

再然后 `JsonFileSessionStore` 把它写进快照、并在读取时还原（旧快照没有这个字段时保持 `None`，不伪造）：

```python
# 旧快照没有这个字段，缺省即"未记录超时语义"，不伪造。
timeout_semantics=(
    TimeoutSemantics(item["timeout_semantics"])
    if item.get("timeout_semantics")
    else None
),
```

**这条链路值得完整讲一遍**：领域对象定义语义 → 执行器产生语义 → 循环写进 Trace → 存储持久化 → 读回来还原。一条事实从产生到可审计，中间没有一处丢失。

### 5.5 面试官会问什么

**Q：为什么不直接用 `asyncio.wait_for`？**

> 因为整个运行时是同步的（`httpx.Client` 而非 `AsyncClient`），引入 async 会污染整条调用链。更关键的是，`asyncio.wait_for` 同样不会真的取消底层阻塞调用——它只是取消 `await`，线程池里的任务照跑。**问题不在用哪个 API，在于“取消”这件事在 Python 里本来就做不到强制。**

**Q：那你这套方案能保证“恰好执行一次”吗？**

> 不能，而且我不宣称能。`COOPERATIVE` 只保证“检查点和写入点之间不会插入长时间等待”，如果写入本身写到一半崩溃，仍然可能是半个状态。真正的“恰好一次”需要外部系统支持幂等键，这不是运行时单方面能给的。**我能保证的是：不会出现“报告超时了但稍后又写入”这种自相矛盾。** 这比“假装有事务”诚实。

**Q：同步执行不怕卡死整个循环吗？**

> 怕，所以我只对声明了 `has_side_effects=True` 的工具这么做，而且当前唯一的副作用工具是本地文件追加。真要有慢的副作用工具，正确做法是实现 `invoke_with_session_deadline` 走协作路径，而不是退回去用假超时。**这个取舍的方向是“宁可慢，不撒谎”，不是“宁可快，随便”。**

**Q：工具结果大小怎么限制？**

> 两道：执行器里 `max_result_chars=10000` 截断一次（`_limit_content`），Context 组装时 `max_tool_result_chars=2000` 再截一次。前者防止 Session 被写爆，后者防止模型输入被撑爆。两处都带 `...[truncated]` 标记，模型能看出被截断了。

**Q：`calculator` 会不会被 `9**9**9` 打挂？**

> 以前会。现在有四道闸门：表达式长度 200 字符、嵌套 32 层、指数绝对值 64、结果规模（整数 1024 位 / 浮点 1e100），任何一道超限立即失败。
>
> 关键是**检查顺序**：`9**9**9` 的指数本身要先算成 387420489，所以必须在做幂运算**之前**检查指数，而不是算完之后再判断结果大小。

---

## 六、第六幕：三个容易讲出彩的细节

时间够就讲，不够就跳过。这三个细节的共同点是：**看起来是小事，实际是设计取舍。**

### 6.1 为什么不用供应商原生 function calling

**入口**：`infrastructure/llm/openai_compatible.py`，第 137 行 `decide()`。

适配器要求模型返回两种 JSON 之一：

```json
{"type":"final_answer","answer":"...","decision_summary":"公开摘要"}
```

```json
{"type":"tool_calls","tool_calls":[{"name":"echo","arguments":{"text":"hi"}}],"decision_summary":"公开摘要"}
```

解析在 `_decode_decision()`（第 220 行），全部本地校验：`type` 必须是这两个之一，`answer` 必须非空字符串，`tool_calls` 必须是非空数组，每个 call 的 `name` 必须非空、`arguments` 必须是对象。

> 题目要求“自行实现 LLM 输出解析”。用原生 `tool_calls` 字段的话，OpenAI SDK 会替你把 JSON 解成对象，这个能力就不存在了。改成自定义协议，控制流 100% 在自己手里——代价是模型可能不听话（返回带 Markdown 代码块包裹的 JSON 就直接挂），收益是解析规则完全可测。

**顺带一个不得不提的副作用**：`_encode_messages()`（第 186 行）把内部 `TOOL` 消息改写成 `USER` 消息。

```python
if message.role is MessageRole.TOOL:
    content = f"工具执行结果（调用 {message.tool_call_id}）：{message.content}"
    encoded.append({"role": MessageRole.USER.value, "content": content})
```

因为没用原生 tool 角色，就没法满足 `assistant.tool_calls` 和 `tool` 消息的严格配对要求。所以工具结果被当成一段普通上下文交给模型，调用 ID 保留在文本里，模型仍能把结果和某次调用对上。

**这是一个“协议降级”，不是 bug**，但它确实损失了结构化信息——所以 `ContextBuilder` 才要额外渲染工具信封来补回来。

### 6.2 为什么文件名是 sha256，为什么用 `os.replace`

**入口**：`infrastructure/session/file.py`。

```python
def _path_for(self, session_id: str) -> Path:
    # 文件名用 sha256，而不是直接拼 session_id：
    # session_id 来自命令行，可能包含 "/"、".." 等字符，直接拼会越出目录
    # 或产生非法文件名。哈希后既安全，又能保持"一个 Session 一个文件"。
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return self._directory / f"{digest}.json"
```

```python
def _save(self, session: Session) -> None:
    # 先写同目录临时文件、fsync，再 os.replace 原子替换：
    # 进程在任何时刻崩溃，磁盘上要么是旧快照，要么是新快照，不会出现半个 JSON。
    payload = self._encode_session(session)
    path = self._path_for(session.session_id)
    with self._lock:
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._directory,
                prefix=".session-", suffix=".tmp", delete=False,
            ) as temporary:
                json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
            raise SessionStorageError(
                f"unable to save session: {session.session_id}"
            ) from error
```

三个点：

1. **sha256 防目录穿越**：`session_id` 来自命令行，`../../etc/passwd` 这种值如果直接拼，就写到目录外面去了。
2. **临时文件必须在同目录**：`os.replace` 只有在同一文件系统内才是原子的，跨文件系统的 rename 是“复制 + 删除”，不原子。所以 `dir=self._directory` 是必需的。
3. **`fsync` 再 `replace`**：只 `replace` 不 `fsync`，断电时可能 rename 完成了但数据还在页缓存里，文件变成空文件。这是很多“原子写”实现的漏洞。

### 6.3 Trace 脱敏是递归的

**入口**：`infrastructure/trace/redaction.py`。

```python
sensitive_field_names: frozenset[str] = frozenset(
    {
        "api_key", "apikey", "authorization",
        "cookie", "password", "secret", "token",
    }
)
```

`redact_attributes` 递归处理 `Mapping` / `list` / `tuple`，命中字段名就替换成 `[REDACTED]`。

**为什么只匹配字段名，不做内容识别（比如正则找 `sk-` 开头的字符串）？**

> 因为内容识别永远有漏网之鱼，而且会误伤。字段名匹配是“由写 Trace 的代码保证命名正确”，这是可控的；正则匹配是“猜测哪些字符串像密钥”，这是不可控的。**安全边界应该建立在结构上，不是建立在模式识别上。**

同时 `openai_compatible.py` 在抛 `LLMServiceError` 时也做了一次兜底：

```python
detail = detail.replace(self._config.api_key, "[REDACTED]")
detail = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", detail)
```

因为网关的 401 响应体可能把 Key 原样回显。

---

## 七、高频追问的 12 个问题（速查表）

面试时间不够时，直接照这张表答，每题 30 秒。

| # | 问题 | 30 秒回答 | 想让他看到什么 |
| --- | --- | --- | --- |
| 1 | 你的循环和 `while True` 有什么区别 | 三种受控出口 + 每轮重建 Context + 结构化终止原因，没有隐式状态 | 你知道“能跑”和“可运维”的区别 |
| 2 | 为什么不用框架 | 红线要求；而且用框架后最值得讲的部分就不存在了 | 你能说清框架替你做了什么 |
| 3 | 怎么保证不无限循环 | `max_iterations` + `for range`，第 8 轮后返回 `max_iterations` | 终止条件是结构性的，不是靠自觉 |
| 4 | 工具失败怎么办 | 转成 `ToolResult(error)` 交给模型，不终止循环 | 你把纠错能力留给模型 |
| 5 | 工具超时真的停了吗 | `join` 停不了线程，所以按副作用分三类：同步执行 / 协作截止 / 安全放弃 | 你懂并发语义，不是会调 API |
| 6 | 上下文超了怎么办 | 三层：条数 → 总字符 → 摘要展示长度；最后明确失败 | 分层降级，不是一刀切 |
| 7 | 会不会丢历史 | 原始消息不动，只改“这次发什么”；`covered` 单调，失败不推进 | 压缩是有损派生，不是破坏 |
| 8 | 多个 Session 会串吗 | 所有读写显式带 `session_id`，`build()` 只读当前 Session | 隔离是协议层保证的 |
| 9 | 怎么防密钥泄露 | Trace 递归按字段名脱敏 + HTTP 错误体兜底替换 | 两道防线，不是一道 |
| 10 | 崩溃了会怎样 | 单次写原子（临时文件 + fsync + replace）；工具交互三样东西一次写完，接口层拒绝单独追加 TOOL 消息 | 你分得清“单写原子”和“事务原子”，并且让非法状态不可表示 |
| 11 | 怎么测的 | 150 个离线测试，真实 LLM 单独分组、默认跳过 | 测试不依赖不可控输出 |
| 12 | 最大的不足是什么 | 真实集成测试写了但没在真 Key 环境跑过；另有三个想清楚后保留的取舍 | **诚实 + 会权衡** |

---

## 八、我知道的边界：修掉的、没验证的、以及不打算修的

这一节是整场面试的胜负手。**主动说，比被问出来强十倍。**

但“主动交代”不等于把没做的事堆在一起。我把它分成三类，因为它们的性质完全不同：

| 类别 | 含义 | 面试价值 |
| --- | --- | --- |
| 已经修掉 | 发现 → 定位 → 修 → 补测试 | 证明我会自查，不是写完就交 |
| 还没验证 | 用例写了，但没在真实环境跑过 | 证明我分得清“写完”和“验证完” |
| 不打算修 | 想清楚后保留的取舍，不是遗漏 | 证明我会权衡，不是堆功能 |

### 8.1 已经修掉的五件事

这五条都是“代码能跑，但语义不一致”。它们只有真读自己的代码才会发现——测试不会主动告诉你，除非你先想到要测它。

**（1）副作用工具超时后仍然写入** —— 最严重的一个，也是第五幕的主线。

以前所有工具都走 `Thread + join(timeout)`，`has_side_effects` 声明了但没人读。`todo add` 超时后向模型报失败，线程继续跑，待办照样写进去。

修法是按副作用分流成 `SYNCHRONOUS` / `COOPERATIVE` / `ABANDONED_SAFE` 三种语义，`Deadline` 负责协作式放弃，语义一路传到 `ToolResult` → Trace → 快照。

补的测试：`tests/test_tool_executor.py` 三条路径各有用例，包括“超时后等待，断言待办仍为空”这条核心断言；`tests/test_loop_trace.py` 断言 Trace 里真的记了 `timeout_semantics`。

**（2）一次工具交互写两次**

以前 `append_tool_interaction` 和 `append_message(TOOL)` 是两次落盘，中间崩溃留下半条交互。

修法是把 TOOL 消息并进 `append_tool_interaction(session_id, call, result, message)`，一次 `os.replace` 写完三样东西；同时 `append_message` **拒绝**单独追加 TOOL 消息——让“半条交互”在接口层就不可表示。

配套新增 `session/invariants.py`，把“什么算一致”收敛成几个纯函数（调用 ID 唯一、消息必须能对上调用和结果），内存 Store、文件 Store 和读取校验共用同一套规则，避免两处实现各自漂移。

**（3）`calculator` 没有资源上限**

`9**9**9` 语法合法，但会让解释器算到天荒地老。现在有四道闸门：表达式长度 200 字符、嵌套 32 层、指数绝对值 64、结果规模（整数 1024 位 / 浮点 1e100）。

这里有个容易被忽略的细节：**`9**9**9` 的指数本身要先算出来（387420489）**，所以指数检查必须发生在做幂运算之前，而不是算完之后再判断结果大小。

**（4）交互模式永远返回 0**

`_run_interactive` 以前固定 `return 0`，即使某一轮以 `llm_error` 结束。脚本化调用会把失败当成成功。修法是用 `failed` 标记汇总整轮结果，`Ctrl-C` 主动中断的那轮不计为失败。

**（5）`--max-iterations 0` 的退出码不对**

`LoopConfig` 抛的 `ValueError` 被外层 `except` 吞成 1，但 docstring 写的是 2。根因是**校验位置错了**：参数校验被放在依赖装配之后。修法是把检查前移到装配之前，配置加载单独成一个阶段。

顺带解决一个副作用：以前一次根本没发起的运行也会创建一个 Trace 文件。

> 还有一个不在计划里的小修：终端把非法 UTF-8 字节按 `surrogateescape` 解码会产生 `\udcXX`，这种字符无法再编码成 UTF-8，会在写 Session 文件时才炸。现在领域对象构造期就拒绝，错误提前到“根本进不了系统”。

### 8.2 还没验证的一件事：真实集成测试

原来 `tests/integration/` 只有 2 个用例：一个单次决策，一个压缩后追问。它们都没有驱动完整工具闭环——其中一个的 `tool_executor=UnusedTools()`，被调用就 `AssertionError`。

Mock 闭环能证明代码路径通，**不能证明模型真的会按协议调工具**。所以我补了两个关键场景：

| 场景 | 断言 |
| --- | --- |
| 真实模型自主调 `calculator` + `todo` | 最终回答含 `3973`；Session 里有两次成功 `ToolResult`；`termination_reason == final_answer` |
| 同 Session 追问 + 跨 Session 隔离 | 同 Session 能答出之前写入的待办；另一个 Session 的回答**不含**它 |
| 预置长历史后追问早期事实 | 回答含 `ORANGE-42`，`prompt_chars ≤ 预算` |

但有一件事必须说清楚：**这三个用例我没有在真实 Key 的环境里跑过。**

默认门禁会排除 `integration` 标记，所以前面那个“150 个测试通过”**不包含它们**。按本仓库的规矩，没跑过的测试不能写成通过——这条比多报几个数字重要。

要真跑是这条命令，会真实产生费用：

```bash
uv run --env-file .env pytest -m integration -q
```

通过标准：输出模型名、耗时、工具调用次数、Trace 路径，不输出 Key；三个场景全绿。任一失败时保留原始错误和 Trace，**不用重试或 Mock 伪造成功**。

**为什么这条要单独拎出来讲**：因为它是唯一一条“我的系统要和真实模型对赌”的测试。其他 150 个测试证明我的代码自洽；这一条才证明模型愿意按我设计的协议工作。

### 8.3 不打算修的三个取舍

这三条不是遗漏，是想清楚后决定保留的。面试时主动说“我知道它不完美，但我选了这个”，比说“以后会改”更有分量。

**（1）摘要是抽取式的，不是语义摘要**

`ExtractiveHistorySummarizer` 只是把旧消息按角色拼接后截断。它能保住字面事实，保不住因果关系。

为什么保留：语义摘要要额外调一次模型，有成本、有延迟、有随机性，还要处理“摘要不合格”的分支。当前阶段“可预测、零成本、可离线测试”更重要。它被放在 `HistorySummarizer` 协议后面，换实现不动 `ContextBuilder`。

**（2）用字符数代替 token 数**

token 计数依赖具体模型的分词器，要引 `tiktoken` 或调供应商接口，会把一个纯离线可测的模块变成网络依赖。字符数对中文是偏紧的估计（安全方向）。

真要精确，应该给 `ContextConfig` 加 `count_tokens: Callable` 注入点。

**（3）单条超长消息直接失败，不截断**

一次粘贴几万字，没有旧历史可压，会以 `context_error` 结束。

为什么不在超预算时截断用户输入：截断会把问题改得面目全非，而且模型不知道自己看到的是残缺的。**宁可明确失败，也不静默篡改。**

---

## 九、三套带读脚本

时间不同，路线不同。**开场先问一句“您想看整体设计还是某个具体机制”**，然后选路线。

### 5 分钟：只讲一个故事

```text
1. 30 秒定位（3,832 行 / 2 个依赖 / 150 个测试）
2. 打开 loop.py 第 76 行，讲三种出口（2 分钟）
3. 讲“为什么每轮重建 Context”——模型无状态，Session 是唯一事实源（1.5 分钟）
4. 主动交代 8.2（真实集成测试缺口）和 8.3 的三个取舍（1 分钟）
```

**目标**：让他记住一件事——**这个人知道自己的系统哪里会坏。**

### 15 分钟：走完主链路

```text
1. 30 秒定位 + 分层图（1 分钟）
2. domain/models.py：Message 三条规则 + Session 的 covered 校验（3 分钟）
3. agent/ports.py：六个 Protocol，为什么 ToolDefinition 放应用层（2 分钟）
4. agent/loop.py：三种出口 + 每轮重建（4 分钟）
5. context/builder.py：三层预算 + 消息组（4 分钟）
6. 主动交代 8.2 的缺口与 8.3 的取舍（1 分钟）
```

**目标**：让他确认你**真的自己写了循环和压缩**。

### 45 分钟：完整带读 + 现场演示

```text
1. 定位与路线（2 分钟）
2. 第一幕 领域层（6 分钟）
3. 第二幕 端口层（4 分钟）
4. 第三幕 主循环（10 分钟）
5. 第四幕 Context（12 分钟）
6. 第五幕 工具执行（8 分钟，讲三种超时语义，这是加分项）
7. 第六幕 两个细节（2 分钟，可选）
8. 边界说明：修掉的 / 没验证的 / 不打算修的（3 分钟）
```

**目标**：让他觉得“这个项目可以继续做下去”，而不只是“作业做完了”。

> 如果时间只够讲一个技术点，选**第五幕的三种超时语义**。它是这个项目里唯一一个“先有 bug、再想清楚语义、最后落成枚举和截止时间”的完整闭环，比讲循环更有说服力。

---

## 十、现场演示三条命令

别只讲代码，能跑起来的东西说服力完全不同。

**第一条：一次带工具的真实运行**

```bash
uv run yjy-agent-runtime --session-id demo \
  --trace-file logs/demo.jsonl \
  "用 calculator 计算 137 * 29，并用 todo 把结果记进待办"
```

看点：退出码 0，回答含 `3973`，stderr 打印耗时汇总（模型 / 工具 / 轮次分开算）。

**第二条：同一个 Session 追问**

```bash
uv run yjy-agent-runtime --session-id demo "列出我的待办"
```

看点：能说出上一条写入的内容。证明 Session 跨进程恢复有效。

**第三条：看 Trace**

```bash
python - <<'PY'
import json
for line in open("logs/demo.jsonl", encoding="utf-8"):
    event = json.loads(line)
    attrs = event["attributes"]
    print(event["iteration"], event["event_type"],
          attrs.get("tool_name", ""),
          attrs.get("status", ""),
          attrs.get("timeout_semantics", ""),
          attrs.get("reason", ""))
PY
```

看点：`run_started → iteration_started → llm_completed → decision_made → tool_started → tool_completed → ... → run_terminated`，全部可按 `run_id` 关联，没有 API Key。工具事件里还带 `status` 和 `timeout_semantics`——**一条事实从执行器产生，到落盘可查，中间没有丢失**。

**如果他要看测试**：

```bash
TMPDIR=/tmp/yjy-pytest uv run pytest -q --basetemp=/tmp/yjy-pytest/base
```

`TMPDIR` 那一段要解释：沙箱环境不允许写系统临时目录，会报 `PermissionError: mkdir .../pytest-of-root`，这不是代码问题。

---

## 十一、这个项目最想被记住的一句话

如果面试只剩最后 30 秒，说这句：

> 这个项目里最难的从来不是那个 `for` 循环，而是**四件“让错误无法发生”的事**：领域对象在构造期就拒绝非法状态；Context 压缩失败时不推进覆盖范围，宁可重试也不丢历史；工具失败转成结构化结果交给模型，而不是抛异常终止；超时不是“不再等待”，而是要用一个枚举说清“之后还会不会写入”。
>
> 循环只是把这些约束串起来。约束想清楚了，循环就短了——384 行，其中一半是注释。

至于边界，我分三类说清楚：已经修掉的五件、还没验证的一件、以及想清楚后决定不修的三件。**知道哪里会坏、以及为什么暂时不修，比假装哪里都不会坏，更接近工程。**

---

## 附：相关文档

- [分层与数据流](architecture-layers.md)：四层职责、Session 如何流转、Context 如何组装
- [上手指南](hands-on-guide.md)：按顺序跑起来再读代码，附 13 步阅读顺序
- [修复与验收计划](runtime-remediation-plan.md)：第八节问题的原始来源与复现路径。注意该文档仍把若干条目记为“未修复”，实际已在后续提交中修完，尚未同步
- [运行审计](runtime-audit.md)：需求验收记录与已复现问题
- [开发计划](development-plan.md)：阶段划分与执行记录
