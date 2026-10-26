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

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
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

    @property
    def definition(self) -> ToolDefinition:
        """返回用于注册和模型暴露的工具定义。"""
        ...

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        """使用已经过校验的参数执行工具。"""
        ...


class ContextProvider(Protocol):
    """为当前请求构建本轮模型上下文。"""

    def build(
        self,
        request: RunRequest,
        tool_results: Sequence[ToolResult],
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
        """向已存在的 Session 追加消息并返回新快照。"""
        ...

    def append_tool_interaction(
        self,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
    ) -> Session:
        """原子追加相互关联的工具调用和结果。"""
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
