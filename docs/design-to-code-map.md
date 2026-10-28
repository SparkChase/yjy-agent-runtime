# 从设计到代码：三层架构的逐概念对照

> 用途：面试前 30 分钟过一遍，确保**讲的每一句设计，都能立刻指到一行代码**。
> 基准：`37d951d`，`src` 3,832 行 / 28 个 `.py`；`pytest -q` → **150 passed, 4 deselected**。
> 引用方式：`文件:行号`。行号会随提交漂移，锚点以「函数名 / 类名」为准。

---

## 零、先承认一件事：这份代码和那套设计**不完全一样**

视频里那套设计（`Context[O]` 泛型、`Assistant` / `ToolCallReviewer` 角色对象、
`ModelClient.generate(context, settings)`、`AgentService`、`main.py` 组装根）是一个
**更完整、更教科书**的形态。

这个仓库落地的是它的**简化版**：概念对得上，但命名、边界、对象数量都不一样。

所以今晚最危险的动作，是**照着设计的词讲代码**。面试官一边听一边看屏幕，
你讲 `ToolCallReviewer`，他找不到类，第一印象就崩了。

**正确姿势**：先讲设计，再主动交代"我这一版收敛成了什么、为什么、代价是什么"。
本文第 1 节那张总表，就是给你干这个用的——**主动交代，比被问出来强十倍**。

---

## 一、总表：设计里的 12 个概念，落在这份代码的哪里

| 视频设计 | 本仓库的对应物 | 状态 |
| --- | --- | --- |
| 领域层 `Session` / `Message` | `domain/models.py:226` / `:90` | ✅ 有，且更严格（不可变 + 构造期校验） |
| `UserMessage` / `AssistantMessage` / `ToolResult` 三种消息类 | 单一 `Message` + `MessageRole` 枚举 | ⚠️ **形态不同**，见 3.1，这是最需要主动讲的一处 |
| `Tool`：`name/description/parameters/execute()` | `ToolDefinition` + `Tool` 协议（`invoke`） | ✅ 有，多一个 `has_side_effects` |
| `Context[O]` 泛型（instruction/tools/messages/output_type） | `ContextProvider.build() -> Sequence[Message]` | ❌ **没有泛型**，见 3.3 |
| `ModelClient.generate(ctx, settings)` | `LLMClient.decide(messages) -> AgentDecision` | ⚠️ 参数少一个，见 3.4 |
| `ModelSettings(model_id, effort)` | 无，模型名在 `OpenAICompatibleConfig` | ❌ **没有**，见 3.4 |
| `Assistant` 角色（自己的指令 + 工具） | 指令在 `ContextConfig.system_prompt`（`builder.py:134`） | ⚠️ 有内容，没有对象 |
| `ToolCallReviewer` 角色 | 无 | ❌ **没有**，见 3.5，最大的一处差距 |
| `Decision(should_allow, reason)` | 无 | ❌ 没有 |
| `AgentService`（`new_session/list_sessions/chat`） | `AgentLoop.run()` + `SessionStore` 方法 | ⚠️ 拆开了，见 3.6 |
| `WebApi` / `CliApp` | 只有 `CliApp`（`interfaces/cli.py`） | ⚠️ 只有一个入口 |
| `main.py` 组装根 | `cli.py:153` 的 `build_loop()` | ⚠️ 和入口合并在一个文件 |
| 多厂商 adapter（OpenAI/Anthropic/Google） | 只有 `OpenAICompatibleLLMClient` | ⚠️ 端口留好了，只实现了一个 |

一句话版本，可以直接背：

> 视频那套是**理想分层的完整形态**；我这一版把"角色对象"和"泛型 Context"这两层
> 抽象去掉了，换成了**更少的对象 + 更强的构造期约束**。抽象少了，但每条不变量
> 都能被测试守住。下面我逐个说清楚代价。

---

## 二、三层到底怎么落地

设计里的三层叫 领域层 / 应用层 / 适配层。这个仓库叫：

```text
src/yjy_agent_runtime/
├── domain/          领域层  ← models.py 一个文件，零 I/O，零 import 外部库
├── agent/           应用层  ← loop.py（编排）+ ports.py（6 个端口协议）
├── infrastructure/  适配层  ← llm / session / tools / context / trace
└── interfaces/      入口层  ← cli.py + config.py
```

**和设计的差别只有一处**：设计把"入口"和"组装根"分开写（`WebApi` / `main.py`），
这里把入口独立成第 4 层，组装根塞在入口里。

依赖方向是单向的：

```text
interfaces → infrastructure → agent → domain
```

**这条不是靠自觉，是靠测试守住的。** `tests/test_architecture.py:22`：

```python
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    "domain": set(),                      # 领域层不许依赖任何内部层
    "agent": {"domain"},                  # 应用层只认识领域对象
    "infrastructure": {"domain", "agent"},# 适配层实现应用层端口
    "interfaces": {"domain", "agent", "infrastructure"},
}
```

它用 `ast` 静态解析每个文件的 import，发现反向依赖就 fail（`:64` 起）。

**面试时这句要讲出来**：

> 分层最容易变成一句口号。我把它写成了一条会红的测试——只要有人在 `agent/`
> 里 `from ...infrastructure.llm import ...`，CI 立刻挂。架构约束和业务逻辑一样，
> 需要有执行机制。

---

## 三、逐概念对照

### 3.1 Session / Message —— 我和视频设计得最不一样的一处

#### 视频设计

```text
UserMessage(text)
AssistantMessage(text, tool_calls)
ToolResult(tool_call_id, content)
```

三个类，`AssistantMessage` 里**装着** `tool_calls`。也就是说：模型的一次决策，
本身就是一条可持久化的消息。

#### 这份代码

`domain/models.py:90`，只有**一个** `Message`：

```python
@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole          # system / user / assistant / tool
    content: str
    tool_call_id: str | None = None   # 只有 TOOL 角色允许有

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("message content must not be empty")
        _ensure_no_surrogates(self.content, "message content")
        if self.role is MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.role is not MessageRole.TOOL and self.tool_call_id is not None:
            raise ValueError("only tool message may contain tool_call_id")
```

而"模型决策"是**另一组类型**（`models.py:145` / `:158`）：

```python
class FinalAnswerDecision:   answer: str
class ToolCallDecision:      tool_calls: tuple[ToolCall, ...]

type AgentDecision = FinalAnswerDecision | ToolCallDecision   # models.py:173
```

#### 为什么这么分？这是今晚最值钱的一段

设计的写法里，`AssistantMessage` 同时扮演两个角色：

1. **模型的瞬时输出**（要被执行、被校验、可能被拒绝）
2. **历史的持久事实**（要被写进 Session、发给下一轮模型）

**这两个东西的生命周期不一样，硬塞进一个类会出事。**

具体会出什么事？假设模型说"我要调 3 个工具"，你先把
`AssistantMessage(text, tool_calls=[c1, c2, c3])` 写进 Session，然后逐个执行：

- 第 1 个成功 → 写入 result_1
- 第 2 个执行到一半，进程被 kill

磁盘上就留下了：**一条声称调了 3 个工具的消息 + 1 个结果**。
下一轮读回来，模型看到"我说要调 c2、c3，但没有结果"，它只能瞎猜。

这份代码的处理是：**工具调用根本不进 `messages`，它是并列的独立事实**。
看 `models.py:226` 的 `Session`：

```python
@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    messages: tuple[Message, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()       # ← 独立存放
    tool_results: tuple[ToolResult, ...] = ()   # ← 独立存放
    todos: tuple[str, ...] = ()
    summary: SessionSummary | None = None
```

三者靠 `call_id` 关联，并且**必须一次写完**（`session/invariants.py:37`）：

```python
def validate_tool_interaction(call, result, message) -> None:
    if call.call_id != result.call_id or call.name != result.tool_name:
        raise ValueError("tool call and result must refer to the same interaction")
    if message.role is not MessageRole.TOOL:
        raise ValueError("tool interaction message must use the tool role")
    if message.tool_call_id != call.call_id:
        raise ValueError("tool message must reference the same call id")
    if message.content != result.content:
        raise ValueError("tool message content must match the tool result")
```

写入口只有一个：`SessionStore.append_tool_interaction()`（`ports.py:127`），
并且明确规定 **TOOL 消息不许单独追加**（`invariants.py:24`）：

```python
def reject_standalone_tool_message(message: Message) -> None:
    if message.role is MessageRole.TOOL:
        raise ValueError(
            "tool messages must be appended together with their tool interaction"
        )
```

**读回来还要再查一遍**（`session/file.py:60-65`），因为老版本可能留下过坏快照：

```python
try:
    assert_tool_interactions_consistent(session)   # invariants.py:61
except ValueError as error:
    raise SessionStorageError(
        f"session snapshot has an incomplete tool interaction: {session_id}"
    ) from error
```

> **这里可以放一句重话**：我不是"写的时候小心一点"，我是让"半条工具交互"
> **在类型层面不可表示**——单独 append 会抛，构造快照要过校验，读盘要再过一次。
> 三道闸门，任何一道都足以拦住它。

#### 代价，必须主动说

这个选择有真实成本，别装看不见：

1. **历史里没有"带 tool_calls 的 assistant 消息"**。所以发给模型时，工具结果
   只能以 `TOOL` 角色出现，而 OpenAI 协议没有对应位置，适配器只能把它改写成
   `USER` 消息（`llm/openai_compatible.py:197`）：

   ```python
   if message.role is MessageRole.TOOL:
       content = f"工具执行结果（调用 {message.tool_call_id}）：{message.content}"
       encoded.append({"role": MessageRole.USER.value, "content": content})
   ```

   这是**故意放弃供应商原生 tool 角色**换来的代价，换来的是"控制流完全在本地"。

2. **没法用 `assistant.tool_calls` 的原生配对校验**。协议层少了一道外部保险，
   所以我自己在 `invariants.py` 补了三道。

面试官如果追问"那模型会不会分不清哪条是工具结果？"——答：
`call_id` 写在文本里，且 `render_tool_envelope()`（`builder.py:564`）把参数、状态、
错误类型都结构化进去了，比裸文本强。

---

### 3.2 Tool —— 从 `execute()` 到 `invoke()` + `has_side_effects`

视频设计：`Tool(name, description, parameters, execute(arguments) -> str)`。

这份代码把"工具长什么样"拆成**契约**和**实现**两块，都在应用层
`agent/ports.py:24`：

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    has_side_effects: bool = False          # ← 设计里没有，我加的
```

```python
class Tool(Protocol):                        # ports.py:51
    @property
    def definition(self) -> ToolDefinition: ...
    def invoke(self, arguments: Mapping[str, Any]) -> str: ...
```

**为什么多一个 `has_side_effects`？** 因为它决定了"超时意味着什么"。
这是第 5 节的主线，先记住一句话：

> 无副作用的工具超时 = 什么都没发生；有副作用的工具超时 = **可能已经写进去了**。

一个布尔字段，把这两种情况在执行前就分开了（`tools/executor.py:142`）：

```python
if tool.definition.has_side_effects:
    if hasattr(tool, "invoke_with_session_deadline"):
        return self._invoke_cooperatively(tool, session_id, arguments)
    return self._invoke_inline(tool, session_id, arguments), TimeoutSemantics.SYNCHRONOUS
```

**为什么放在应用层而不是适配层？** 因为 `Context` 要把它塞进系统提示
（`builder.py:403`），`ToolRegistry` 要按它校验，模型要看到它。三方都要用，
那就不是某一方的私有细节。

注册表本身很薄，`tools/registry.py:15`，只有 40 行，三件事：

```python
def register(self, tool):        # 重名直接拒绝，不静默覆盖
def get(self, name):             # 未知名字抛 KeyError，保留语义
def definitions(self):           # 给 LLM 看的只读视图
```

具体工具有 4 个，`tools/builtin.py`：`echo`（`:12`）、`calculator`（`:34`）、
`search`（`:147`）、`todo`（`:186`）。**`todo` 是唯一带副作用的**（`:211`），
也是唯一实现了 `invoke_with_session` 的——因为它要往当前 Session 写待办，
必须显式拿 `session_id`。

---

### 3.3 Context —— 为什么我没用 `Context[O]` 泛型

#### 视频设计

```text
Context[O]: instruction, tools, messages, output_type
ModelClient.generate(context: Context[O]) -> O
```

好处很直观：**同一个 `generate`，传 `output_type=AssistantMessage` 就回复用户，
传 `output_type=Decision` 就审核工具**。一个方法，两种用途，类型安全。

#### 这份代码

`agent/ports.py:68`：

```python
class ContextProvider(Protocol):
    def build(self, request: RunRequest, tool_results: Sequence[ToolResult]) -> Sequence[Message]:
        """返回当前 Session 本轮需要发送给模型的消息。"""
```

只有 `messages`，没有 `instruction`、没有 `tools`、没有 `output_type`。
另外两个东西去哪了？

| 设计里的字段 | 这份代码在哪 |
| --- | --- |
| `instruction` | `ContextConfig.system_prompt`（`builder.py:134`），拼进第一条 system 消息 |
| `tools` | 序列化成 JSON 拼进同一条 system 消息（`builder.py:407-402`） |
| `messages` | 就是返回值 |
| `output_type` | **没有**，由 `LLMClient.decide()` 的返回类型 `AgentDecision` 固定 |

#### 代价与理由

**理由一：泛型 Context 会把"解析"推给适配器。**

`generate(context[O]) -> O` 意味着适配器要负责"按 `O` 解析"。一旦 `O` 变成
联合类型，适配器里就会出现 `if output_type is ...` 的分支——**类型参数在运行时
被当标签用**，泛型只买到了 IDE 提示，没买到真正的安全。

这份代码的做法：解析逻辑集中在 `_decode_decision()`（`openai_compatible.py:219`），
返回一个**封闭联合**：

```python
type AgentDecision = FinalAnswerDecision | ToolCallDecision   # models.py:173
```

循环里用 `isinstance` 分派（`loop.py:255` / `:194`），非法类型落到
`INVALID_DECISION` 终止。**出口是穷举的，不靠泛型约束。**

**理由二：`instruction + tools` 最终都要变成字符串，那就不该是三个字段。**

`builder.py:407` 的 `_system_content()` 把它们拼成一条：

```python
encoded = json.dumps(definitions, ensure_ascii=False, sort_keys=True)
return f"{self._config.system_prompt}\n可用工具定义：{encoded}"
```

`sort_keys=True` 是为了**同一组工具每次序列化结果一致**——否则 prompt 前缀
每次都变，任何前缀缓存都失效，Trace 对比也做不了。

**理由三（这条最实在）：少一层抽象，就少一处能骗过测试的地方。**

`Context` 是个纯数据容器，它自己的正确性没法测，只能测"用它的人"。而
`ContextProvider` 是个**行为**——`tests/test_agent_loop.py:28` 里
`StubContextProvider` 可以直接替换它，整个循环不用碰模型。

> 面试答法：**泛型 Context 买的是"多角色复用同一个 generate"，而我只想清楚
> 两件事——回复用户、解析工具调用。为两个用例引入一层泛型，收益小于它的
> 认知成本。如果我明天要加 ToolCallReviewer（第 3.5 节），我会先加角色对象，
> 再看要不要把 Context 提出来——抽象应该来自已经出现的重复，不是对未来的想象。**

---

### 3.4 ModelClient —— 为什么 `decide` 只收 messages

视频设计：`ModelClient.generate(context: Context[O], model_settings: ModelSettings) -> O`，
`ModelSettings` 里打包 `model_id` 和 `effort`（六档思考深度）。

这份代码，`ports.py:92`：

```python
class LLMClient(Protocol):
    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        """根据上下文返回最终回答或工具调用决策。"""
```

**没有 `ModelSettings`。** 模型名、超时、base_url 全在
`OpenAICompatibleConfig`（`openai_compatible.py:52`），构造时一次定死。

#### 这是真缺，不是简化

设计里 `ModelSettings` 的价值在于：**同一个 Agent 里，不同角色可以用不同的模型和
思考深度**——回复用户用强模型 + 高 effort，审核工具用便宜模型 + 低 effort。

这份代码做不到，因为整个运行只有一个 `LLMClient` 实例。

**如果面试官问"那你想让审核用便宜模型，怎么办？"** 别硬撑，直接答：

> 现在做不到，因为 `LLMClient` 是单例注入的。补起来很小：`decide(messages, settings)`
> 加一个参数，`LoopConfig` 里放两份 `ModelSettings`，`loop.py:229` 调用时选一份。
> 适配器里把 `effort` 翻译成各家字段——OpenAI 叫 `reasoning_effort`，Anthropic
> 叫 `output_config.effort`。**这个翻译动作正是适配层存在的理由**，我在
> `_encode_messages` 里已经做过一次同类翻译（TOOL→USER），加这个不破坏分层。

#### 换厂商要改几行？——这是分层的验收标准

**两处**：新增 `adapters/models/anthropic_client.py`（实现 `decide`），
然后在 `cli.py:217` 换一行构造。

```python
# cli.py:217
llm = OpenAICompatibleLLMClient(config)
```

这就是分层的全部意义：**换模型不改循环，换存储不改循环，换入口不改循环。**
你可以现场打开 `loop.py:101-74` 给他看 `__init__` 的 5 个参数全是 `Protocol`：

```python
def __init__(self, *, context_provider, llm_client, session_store,
             tool_executor, trace_recorder, config=None):
```

没有一个具体类名。这就是依赖倒置的**可验证证据**。

---

### 3.5 角色（Assistant / ToolCallReviewer）—— 我没有，这是最大的一处差距

#### 视频设计

```text
Assistant:        自己的指令 + 可用工具 → context(session) -> Context[AssistantMessage]
ToolCallReviewer: 审核指令，无工具       → context(session, call) -> Context[Decision]
Decision:         should_allow: bool, reason: str | None
```

工作流第 5 步：**模型说要调工具时，另调一次模型审核这次调用是否合理**，
不通过就返回拒绝结果给模型。

#### 这份代码：没有这一步

`grep -r "Reviewer\|should_allow" src/` → **零命中**。

`loop.py:317` 拿到 `decision.tool_calls` 之后，**直接进执行**：

```python
for call in decision.tool_calls:
    tool_started = perf_counter()
    ...
    tool_result = self._tool_executor.execute(request.session_id, call)   # loop.py:334
```

#### 那我用什么替代？——三条结构性约束

这是今晚必须讲清楚的一段，因为它决定了"没有审核"到底算不算缺陷。

**第一，参数校验在执行前**（`executor.py:78`）：

```python
validate(instance=dict(call.arguments), schema=dict(tool.definition.parameters))
```

非法参数根本进不到工具实现里，返回的是 `ToolArgumentsValidationError`
（`:82-87`），**作为可观察结果回给模型**，让模型自己改参数重试。

**第二，副作用在定义期就被标记**（`ports.py:36`），执行方式按标记分流
（`executor.py:142`）——有副作用的工具，要么同步执行（没有假超时），
要么带 `Deadline` 协作退出（`tools/deadline.py:39`）：

```python
def raise_if_expired(self) -> None:
    """在提交副作用之前调用；已过期则抛出 TimeoutError 放弃写入。"""
    if self.expired:
        raise TimeoutError("tool deadline exceeded before commit")
```

**第三，循环有硬上限**（`loop.py:166`），8 轮必停（`LoopConfig:45`），
超了返回 `MAX_ITERATIONS` 而不是继续烧钱。

#### 怎么讲这段（重要）

> 审核解决的是"**这次调用合不合理**"，是**语义判断**；
> 我这三条解决的是"**即使不合理，也不会留下不可解释的状态**"，是**结构约束**。
>
> 两者不是替代关系，是不同层次。我没有做语义审核，是因为在这个规模下，
> 审核会**把每次工具调用变成两次模型调用**——延迟翻倍、成本翻倍，
> 而收益（拦住"模型想执行 rm -rf"）可以由更便宜的方式拿到：
> 参数 schema、副作用标记、轮次上限。
>
> 但我要说清楚：**如果工具集里有真正危险的操作（比如 bash），光靠 schema 不够。**
> 那时候审核是必须的，而且应该做成"高风险工具才审"，而不是每次调用都审。

**如果面试官说"那你加一个"**，给他这个方案（约 40 行，不改现有结构）：

```python
# 1) ports.py 加一个端口
class ToolCallReviewer(Protocol):
    def review(self, session: Session, call: ToolCall) -> Decision: ...

# 2) domain/models.py 加结论对象
@dataclass(frozen=True, slots=True)
class Decision:
    should_allow: bool
    reason: str | None = None

# 3) loop.py:317，执行前插一步
for call in decision.tool_calls:
    verdict = self._reviewer.review(session, call)
    if not verdict.should_allow:
        tool_result = ToolResult(                 # 拒绝也是结果，不是异常
            call_id=call.call_id, tool_name=call.name,
            status=ToolResultStatus.ERROR,
            content=verdict.reason or "tool call rejected by reviewer",
            error_type="ToolCallRejected",
        )
    else:
        tool_result = self._tool_executor.execute(request.session_id, call)
    # 后面完全不变：照样 append_tool_interaction、照样进下一轮
```

**关键点**：拒绝**不能抛异常**，必须变成一个 `ToolResult`。否则模型永远不知道
自己为什么被拒，也就没法换方案——这和"工具失败照样写回 Session"是同一条原则。

---

### 3.6 AgentService —— 拆成了 `AgentLoop` + `SessionStore`

视频设计：`AgentService` 暴露 `new_session()` / `list_sessions()` / `chat(session_id, msg)`。

这份代码，编排全在 `AgentLoop.run()`（`loop.py:124`），但**用例方法散在两个地方**：

| 用例 | 在哪 |
| --- | --- |
| `new_session()` | `SessionStore.get_or_create()`（`loop.py:150` 调用） |
| `chat(session_id, msg)` | `AgentLoop.run(RunRequest(...))`（`cli.py:234`） |
| `list_sessions()` | **没有** |

`list_sessions()` 缺失是个真实缺口：`JsonFileSessionStore` 存的是
`{sha256(session_id)}.json`（`file.py:193`），**哈希之后没法反查原始 ID**。
想加的话，要么在文件里多存一份 `session_id`（已经存了，见 `file.py:203`），
扫目录读出来即可；要么维护一个索引文件。

> 面试答法：**我知道这个缺口。** 当时选择哈希文件名，是为了防 `../` 路径穿越
> （`file.py:189` 的注释写了原因）；代价就是丢了"列出所有会话"的能力。
> 补法是扫目录 + 读每个文件的 `session_id` 字段，30 行，但会变成 O(n) 次读盘，
> 所以真要做得配一个索引。

**为什么没有独立的 `AgentService` 类？** 因为 `AgentLoop` 已经只有 526 行，
职责单一（就是那个循环），再包一层 `AgentService` 只是转发。**但**如果明天要加
`WebApi`，那一层就有意义了——HTTP 层不该直接碰 `RunRequest`。

---

### 3.7 入口与组装根 —— `build_loop()` 就是 `main.py`

视频设计：`main.py` 是**唯一知道所有具体类**的地方。

这份代码在 `cli.py:153`：

```python
def build_loop(*, llm, trace, max_iterations) -> AgentLoop:
    store = JsonFileSessionStore(os.getenv("YJY_SESSION_DIR", ".yjy-sessions"))
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    return AgentLoop(
        context_provider=ContextBuilder(store, tool_definitions=registry.definitions()),
        llm_client=llm,
        session_store=store,
        tool_executor=RegistryToolExecutor(registry),
        trace_recorder=trace.recorder,
        config=LoopConfig(max_iterations=max_iterations),
    )
```

**这 20 行就是整个系统的装配图。** 三个具体类在这里被创建、被注入：

- `JsonFileSessionStore` → 换 SQLite 只改第 1 行
- `ContextBuilder` → 换压缩策略只改第 4 行
- `RegistryToolExecutor` → 换沙箱执行器只改第 6 行

`main()`（`:177`）负责另外三件事：参数校验（`:185`）、配置加载（`:205`）、
结果与退出码（`:418`）。

**和设计的偏差要主动交代**：

> 设计里组装根是独立文件，我这里和 CLI 合在一起了。好处是少一个文件、
> 依赖关系一眼看完；坏处是**加 `WebApi` 时会重复一遍装配代码**。
> 真要修，把 `build_loop` 抽到 `main.py`，两个入口都 import 它——10 分钟的事。

---

## 四、八步工作流 → 代码落点

视频第一节那 8 步，逐步对照（这张表建议直接背）：

| # | 工作流步骤 | 代码位置 | 一句话 |
| --- | --- | --- | --- |
| 1 | 开始/继续对话 | `loop.py:150` `get_or_create` | 会话连续性靠 `session_id` + 磁盘快照，不靠进程常驻 |
| 2 | 收到用户消息 | `loop.py:153` `append_message(USER)` | **先落盘再建 Context**，否则本轮问题不在输入里 |
| 3 | 组织上下文 | `builder.py:196` `build()` | 三层压缩，见第 5 节 |
| 4 | 调用模型 | `openai_compatible.py:137` `decide()` | 只发 HTTP + 解析 JSON，不做任何循环 |
| 5 | 审核工具调用 | **无** | 见 3.5，用结构约束替代 |
| 6 | 执行工具 / 拒绝 | `executor.py:66` `execute()` | 四道关：注册表 → schema → 副作用分流 → 结果校验 |
| 7 | 循环 | `loop.py:166` `for iteration in ...` | 三种出口，没有第四种 |
| 8 | 持久化 | `loop.py:151` / `:268` / `:359` | 用户消息、最终回答、工具交互各一次 |

**第 2 步那个顺序值得单独讲**，`loop.py:150` 的注释就是答案：

```python
# 1. 先落盘用户输入，再构建 Context。ContextBuilder 只从 Session 读，
#    所以必须先写；否则本轮问题不会出现在模型输入里。
self._session_store.get_or_create(request.session_id)
self._session_store.append_message(request.session_id, Message(MessageRole.USER, request.user_input))
```

反过来的写法（先建 Context 再存消息）会有一个很隐蔽的 bug：**第一轮永远答非所问**，
而且单元测试如果不检查"模型收到了什么"，根本发现不了。

---

## 五、主循环逐行：真正要背的 12 个行号

`agent/loop.py`，526 行。带读时按这个顺序停。

### 5.1 三种出口（`:166` 起）

```python
for iteration in range(1, self._config.max_iterations + 1):
```

**出口一：拿到最终回答**（`:255`）

```python
if isinstance(decision, FinalAnswerDecision):
    ...
    self._session_store.append_message(              # :268 最终回答也落盘
        request.session_id, Message(MessageRole.ASSISTANT, decision.answer))
    result = RunResult(..., termination_reason=TerminationReason.FINAL_ANSWER,
                       iterations=iteration, answer=decision.answer)
    self._record_termination(result)                 # :191
    return result
```

**出口二：依赖报错**（`:182` / `:233` / `:341`，统一走 `:406`）

```python
except Exception as error:
    return self._terminate_after_dependency_error(
        request=request, run_id=run_id, trace_id=trace_id, iteration=iteration,
        reason=TerminationReason.LLM_ERROR, stage="llm", error=error,
        duration_ms=self._duration_ms(llm_started),
    )
```

**出口三：达到上限**（`:391`）

```python
result = RunResult(..., termination_reason=TerminationReason.MAX_ITERATIONS,
                   iterations=self._config.max_iterations)
```

**为什么依赖错误不往上抛？** `loop.py:15` 的 docstring 写了：
CLI 要退出码 1、HTTP 要 503、测试要断言——**三种入口需要三种表达**。
所以循环返回**结构化事实**（`RunResult.termination_reason`），由入口翻译。

`RunResult` 还会反向校验这个约定（`models.py:293`）：

```python
if self.termination_reason is TerminationReason.FINAL_ANSWER and not self.answer:
    raise ValueError("final-answer result requires answer")
if self.termination_reason is not TerminationReason.FINAL_ANSWER and self.answer is not None:
    raise ValueError("non-final result must not contain answer")
```

**"有答案"和"final_answer"是绑死的**，构造不出"失败但带答案"这种模糊状态。

### 5.2 每轮重建 Context（`:101`）

```python
session_before = self._session_store.get(request.session_id)   # 只为 Trace
messages = self._context_provider.build(request, tuple(tool_results))  # :103
```

**为什么每轮都重建，不缓存？** 因为上一轮的工具结果刚写进 Session，
模型必须看到它才能决定下一步。缓存 Context 是这类系统最常见的错误来源。

`:101` 那个 `session_before` **不参与业务判断**，只用来在 Trace 里对比
"压缩前 vs 压缩后"的字符数（`:124-131`）——这是可观测性，不是逻辑。

### 5.3 工具执行与原子写入（`:218`）

```python
for call in decision.tool_calls:
    tool_result = self._tool_executor.execute(request.session_id, call)   # :231
    tool_results.append(tool_result)
    self._session_store.append_tool_interaction(                          # :250
        request.session_id, call, tool_result,
        Message(MessageRole.TOOL, tool_result.content, tool_result.call_id),
    )
```

`:247` 的注释是这一段的灵魂：

> 一次工具交互必须在同一次写入里落盘：调用、结果和 TOOL 消息三者是同一件事。
> 分成两次写，进程在中间崩溃就会留下"有结果没消息"的半条交互。

### 5.4 多工具是串行的（要主动承认）

`for call in decision.tool_calls` 是**顺序执行**，不是并发。三个工具各 3 秒，
就是 9 秒。设计里没提，但面试官容易问。

> 答：**故意的。** 并发执行会引入"部分成功"的中间态——一个成功一个超时，
> 落盘顺序和 `call_id` 顺序不一致，`invariants.py` 的校验会更复杂。
> 这个规模下，串行的可解释性比并发的时间收益值钱。要并发的话，前提是
> 先把 `append_tool_interaction` 改成批量原子写。

---

## 六、三层压缩：这是我最想被问的一段

`builder.py:196` 的 `build()`，是全场最复杂的一个函数（140 行）。核心概念只有一个：

```python
covered = original_covered        # builder.py:244
# messages[:covered] 已被摘要替代，不再进入模型输入
# messages[covered:] 是最近窗口，原样保留
```

**`covered` 是分界线，不是计数器。** 它只能单调递增（`:243` 的注释），
否则同一批消息会被重复摘要。

三层，从粗到细：

| 层 | 触发条件 | 代码 | 做什么 |
| --- | --- | --- | --- |
| 1 | 消息条数超 `max_history_messages`(20) | `:247-264` | 把移出窗口的旧消息**整批**摘要 |
| 2 | 总字符超 `max_context_chars`(24k) | `:274-292` | 按**完整消息组**继续往摘要里推 |
| 3 | 还是超 | `:296-316` | 只缩短摘要的**展示长度**，最近消息不动 |
| 兜底 | 只剩最后一组仍超 | `:321-324` | 抛 `ContextBudgetError` → `context_error` 终止 |

**第一层的取整**（`:251`）：

```python
if target_count > covered:
    target_count = max(covered, self._group_floor(session.messages, target_count))
```

`_group_floor()`（`:511`）把目标下标**向下取到消息组边界**——不能摘要半个工具交互。

**消息组怎么划**（`_message_groups`，`:472`）：

- 连续的 TOOL 消息 = 一组
- 紧邻 TOOL 之前的 ASSISTANT = 工具调用声明，与结果同组
- 其它各自成组

`:480` 的注释是理由：**如果只把 TOOL 结果摘要掉、却留下调用声明，
模型会看到"有调用没结果"的残缺历史。**

**第三层为什么存在？** 因为最近消息组不能拆，只能压摘要。但注意 `:296` 的写法：
它**不改 `covered`**，只改"这次发出去时摘要多长"。存储的摘要仍受
`max_summary_chars` 约束。**展示预算和存储预算是两个东西。**

**失败不推进覆盖范围**（`:326`）：

```python
# 只有成功组装后才落盘，失败时覆盖范围保持原值，下一轮可重试。
if summary_content is not None and (covered > original_covered or ...):
    self._session_store.set_summary(session.session_id,
        SessionSummary(summary_content, covered_message_count=covered))
```

摘要先在内存里累积，整个 Context 组装成功才写回。**摘要器抛错时不会留下
"覆盖范围前进了、摘要却没写完"的坏状态。**

> 这一段如果只能记一句：**`covered_message_count` 是压缩机制的唯一状态，
> 它必须单调、必须落在消息组边界上、必须和摘要同时提交。**

---

## 七、三个"比设计更深"的地方（主动抛出来）

面试里最容易被记住的，不是"你实现了什么"，而是"你想到了什么别人没想到的"。

### 7.1 失败不等于没发生：`timeout_semantics`

`models.py:183` 那个枚举，`ToolResult` 里多一个字段（`:212`）：

```text
SYNCHRONOUS     有副作用工具同步执行，根本没有超时
ABANDONED_SAFE  无副作用工具超时，线程被放弃但无害
COOPERATIVE     有副作用工具带 Deadline，写入前会自我放弃
```

**为什么必须显式写出来？** `status=error + error_type=TimeoutError` 是**歧义**的：

- 对 `calculator`：没算出来，什么都没发生
- 对 `todo add`：我告诉你失败了，但两秒后待办真写进去了

同一个 `ToolResult`，两种截然不同的外部事实。不写出来，调用方只能猜。

### 7.2 `join(timeout)` 停不掉线程

`executor.py:206`：

```python
worker.join(self._timeout_seconds)
if worker.is_alive():
    raise ToolExecutionTimeout(..., TimeoutSemantics.COOPERATIVE)
```

`join` 只是"不再等待"，**线程还在跑**。所以有副作用的工具分两条路（`:142`）：

- 能协作 → 给 `Deadline`，写入前 `raise_if_expired()`（`deadline.py:39`）
- 不能协作 → **同步执行，不设超时**（`executor.py:164`）

`:14` 的注释：**"宁可慢，也不谎报已取消。"** 这句话面试官会记住。

### 7.3 Trace 是关联的，不是一堆日志

`TraceEvent`（`models.py:65`）带 `session_id / run_id / trace_id / iteration`，
**四元组能关联一次运行里的每一次 LLM 调用和工具调用**。

`loop.py:136-137` 每次 `run()` 生成 `run_id` 和 `trace_id`；`_record_trace()`
（`:343`）统一落事件；`_summarize()`（`:375`）把结果截到 500 字符。

**不记思维链**（`models.py:66` 的 docstring 明说了）。只记
`decision_summary`（公开摘要）、参数、结果摘要、耗时、错误类型。

---

## 八、面试官会追问的 12 个问题（速查）

| 问题 | 一句话答案 | 指哪 |
| --- | --- | --- |
| 这不就是 `while True` 吗？ | 循环体只有 5 个阶段，但有 7 种受控终止、3 道原子性闸门、3 层预算 | `loop.py:166`、`models.py:250` |
| 换 Anthropic 要改几行？ | 1 个新 adapter + `cli.py:217` 一行 | `ports.py:92` |
| 怎么测循环不花钱？ | 5 个端口全是 Protocol，测试里全换 Stub | `tests/test_agent_loop.py:28` |
| 为什么不用原生 function calling？ | 控制流要在本地；解析、校验、循环全是自己的 | `openai_compatible.py:3-14` |
| 模型返回烂 JSON 怎么办？ | 4 类异常分开抛，循环转成 `LLM_ERROR`，不是崩 | `openai_compatible.py:157-183` |
| 工具报错为什么不停？ | 失败是可观察事实，回给模型让它自己改 | `executor.py:263` |
| 超时了但工具还在跑？ | 按副作用分流；有副作用要么同步要么协作退出 | `executor.py:142` |
| 上下文爆了怎么办？ | 三层压缩 + 消息组不拆 + 兜底明确失败 | `builder.py:274` |
| 压缩会不会丢历史？ | `covered` 单调 + 组边界 + 成功后提交 | `builder.py:243` |
| 两个会话会串吗？ | 所有读写显式带 `session_id`，循环自己不存状态 | `loop.py:13` |
| 文件名为什么哈希？ | 防 `../` 穿越，代价是丢了 list | `file.py:193` |
| 最不满意的地方？ | 没有 ToolCallReviewer，没有 ModelSettings，没有 `list_sessions` | 见第九节 |

---

## 九、诚实清单：缺什么、怎么补

**这一节今晚一定要讲，而且要主动讲。** 面试官对"知道边界"的评价，
远高于"什么都做了"。

| 缺口 | 现状 | 补的成本 | 会不会破坏分层 |
| --- | --- | --- | --- |
| `ToolCallReviewer` | 无 | ~40 行（第 3.5 节给了草案） | 不会，加一个端口 |
| `ModelSettings` / `effort` | 无，模型名写死在 config | ~15 行 + 各家字段翻译 | 不会，正是适配层的活 |
| `list_sessions()` | 无（哈希文件名丢了反查） | ~30 行，或加索引文件 | 不会 |
| `WebApi` | 无 | ~80 行，`AgentLoop` 完全不用改 | 不会 |
| 多厂商 adapter | 只有 OpenAI 兼容 | 一个文件/家 | 不会 |
| 工具并发 | 串行 | 需要批量原子写 | 会，`append_tool_interaction` 要改签名 |

**收尾那句**，可以直接说：

> 这套设计真正的价值不是"我实现了多少功能"，是**每个缺口都只需要在适配层
> 加一个文件**。分层不是为了让代码好看，是为了让"还没做的部分"有一个
> 明确的、不会污染核心的位置。

---

## 十、今晚的 25 分钟脚本

| 分钟 | 讲什么 | 打开哪里 |
| --- | --- | --- |
| 0-2 | 三个数字：3832 行 / 2 个运行时依赖 / 150 个测试 | 终端 `pytest -q` |
| 2-5 | 四层目录 + 依赖方向，指 `test_architecture.py:22` 那条测试 | `tests/test_architecture.py` |
| 5-8 | `Message` 一个类的三条规则，讲"为什么不是三个子类" | `models.py:90` |
| 8-11 | `Session` 不可变 + `covered` 上界校验 | `models.py:226` |
| 11-16 | 主循环三种出口，只讲 `:166` `:255` `:391` | `loop.py` |
| 16-20 | `append_tool_interaction` 的原子性，指 `invariants.py:37` | `session/invariants.py` |
| 20-23 | 三层压缩，只讲 `:244` 那个 `covered` 分界线 | `builder.py:196` |
| 23-25 | 主动交代三个缺口 + 一句"每个缺口都只加在适配层" | 第九节 |

**别讲的**：`config.py` 的 dotenv 解析、`trace/redaction.py` 的正则、
`calculator` 的四道闸门（除非他问安全）。这些是加分项，不是主线。

---

## 附：相关文档

- `docs/interview-code-walkthrough.md` —— 45 分钟完整带读（1,276 行，逐函数）
- `docs/architecture-layers.md` —— 分层与端口说明
- `docs/runtime-audit.md` —— 已知问题与验收证据
- `AGENTS.md` —— 工程红线（本文所有约束的上游）
