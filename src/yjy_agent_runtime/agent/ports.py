"""Agent Loop 依赖的应用端口。

端口只描述核心循环需要哪些能力，不绑定模型厂商、存储方式或具体工具。
后续基础设施层通过实现这些协议接入 Agent Loop。
"""

from collections.abc import Sequence
from typing import Protocol

from yjy_agent_runtime.domain import (
    AgentDecision,
    Message,
    RunRequest,
    Session,
    SessionSummary,
    ToolCall,
    ToolResult,
)


class ContextProvider(Protocol):
    """为当前请求构建本轮模型上下文。"""

    def build(
        self,
        request: RunRequest,
        tool_results: Sequence[ToolResult],
    ) -> Sequence[Message]:
        """返回当前 Session 本轮需要发送给模型的消息。"""
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


class TraceRecorder(Protocol):
    """记录公开、结构化且可关联的运行事件。"""

    def record(self, event: str, **attributes: object) -> None:
        """追加一条运行事件。"""
        ...
