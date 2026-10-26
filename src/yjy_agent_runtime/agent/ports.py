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


class TraceRecorder(Protocol):
    """记录公开、结构化且可关联的运行事件。"""

    def record(self, event: str, **attributes: object) -> None:
        """追加一条运行事件。"""
        ...
