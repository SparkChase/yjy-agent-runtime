"""Agent Loop 依赖的应用端口。

端口只描述核心循环需要哪些能力，不绑定模型厂商、存储方式或具体工具。
后续基础设施层通过实现这些协议接入 Agent Loop。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from yjy_agent_runtime.domain import (
    AgentDecision,
    Message,
    RunRequest,
    Session,
    SessionSummary,
    ToolCall,
    ToolResult,
    TraceEvent,
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """工具对外的稳定契约：名称、描述、参数 Schema 和副作用标记。

    这是"应用层契约"而不是基建实现：Context 需要把它注入系统提示，
    注册表需要按它做参数校验，模型需要它来生成调用。把它放在端口层，
    基建的 Context 与 Tool 实现就都只依赖抽象，不会互相绑定。
    """

    name: str # 注册表（唯一键）、模型（调用时填）
    description: str # 这是模型判断"该不该用这个工具"的唯一依据
    parameters: Mapping[str, Any] = field(default_factory=dict) # 模型（生成参数）、执行器（校验）
    has_side_effects: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must not be empty")
        if not self.description:
            raise ValueError("tool description must not be empty")
        # 复制后包成只读映射，防止调用方在运行期间修改 Schema。
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )


class Tool(Protocol):
    """可由 Runtime 注册和调用的工具协议。

    只做无副作用的输入序列化的工具实现 `invoke`；需要读写 Session 状态的
    工具额外实现 `invoke_with_session`，由执行器显式传入 `session_id`。
    """

    @property # 大多数工具的 definition 是常量，用 property 可以让类属性 `_definition` 只构造一次，而不是每个实例一份。
    def definition(self) -> ToolDefinition:
        """返回用于注册和模型暴露的工具定义。"""
        ...

    def invoke(self, arguments: Mapping[str, Any]) -> str: # **为什么 `invoke` 只收 `arguments`，不收 `session_id`？**因为大多数工具不需要会话。只有todo 需要——它要读写当前会话的待办。所以`todo` 额外实现了 `invoke_with_session(session_id, arguments)`，执行器用`hasattr` 探测（`executor.py:150`）。
        """使用已经过校验的参数执行工具。"""
        ...


class ContextProvider(Protocol):
    """为当前请求构建本轮模型上下文。"""

    def build(
        self,
        request: RunRequest,
        tool_results: Sequence[ToolResult],# 因为本轮刚执行完的工具结果还没被下一轮读到——虽然循环已经把它写进 Session 了，但传一份显式的可以让 Context 实现不必重新扫全量历史。下游（`LLMClient.decide`）只接受 `Sequence[Message]`。中间多一个对象，就要多一次转换。
    ) -> Sequence[Message]:
        """返回当前 Session 本轮需要发送给模型的消息。"""
        ...


class HistorySummarizer(Protocol):
    """将尚未覆盖的连续历史增量合并为公开摘要。"""

    def summarize(
        self,
        previous_summary: str | None,
        messages: Sequence[Message],
    ) -> str:
        """返回覆盖旧摘要和新增消息的完整替代摘要。"""
        ...


class LLMClient(Protocol):
    """将模型调用封装为与厂商无关的结构化决策。"""

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        """根据上下文返回最终回答或工具调用决策。"""
        ...


class ToolExecutor(Protocol):
    """校验并执行一次工具调用。"""

    def execute(self, session_id: str, call: ToolCall) -> ToolResult:
        """在指定 Session 中执行工具并返回结构化结果。"""
        ...


class SessionStore(Protocol):
    """按显式 session_id 读取和更新 Session 状态。"""

    def get(self, session_id: str) -> Session | None:
        """读取 Session 快照；不存在时返回 None。"""
        ...

    def get_or_create(self, session_id: str) -> Session:
        """读取 Session，不存在时创建空 Session。"""
        ...

    def append_message(self, session_id: str, message: Message) -> Session:
        """向已存在的 Session 追加非工具消息并返回新快照。

        TOOL 消息不允许单独追加：它必须和对应的调用、结果一起通过
        ``append_tool_interaction`` 写入，否则历史里会出现半条交互。
        """
        ...

    def append_tool_interaction( # 这个地方也只是一个接口 去实现 具体在session下面的 file文件 90行
        self,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
        message: Message,
    ) -> Session:
        """一次写入完整工具交互：调用、结果和对应的 TOOL 消息。

        实现必须保证这次更新是原子的：要么三者全部可见，要么全部不可见。
        """
        ...

    def set_summary(
        self,
        session_id: str,
        summary: SessionSummary | None,
    ) -> Session:
        """设置或清除 Session 摘要元数据。"""
        ...

    def append_todo(self, session_id: str, todo: str) -> Session:
        """向当前 Session 追加待办。"""
        ...

    def list_todos(self, session_id: str) -> tuple[str, ...]:
        """读取当前 Session 的待办快照。"""
        ...


class TraceRecorder(Protocol):
    """记录公开、结构化且可关联的运行事件。"""

    def record(self, event: TraceEvent) -> None:
        """追加一条运行事件。"""
        ...
