"""Agent Runtime 的核心领域对象。

这些类型只描述 Runtime 中稳定的事实和约束，不依赖模型 SDK、数据库或
命令行。将非法输入尽量阻挡在对象创建阶段，可以避免核心循环长期处理
含义不清的普通字典。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class MessageRole(StrEnum):
    """模型上下文中允许出现的消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TraceEventType(StrEnum):
    """Runtime 可公开记录的结构化事件类型。"""

    RUN_STARTED = "run_started"
    ITERATION_STARTED = "iteration_started"
    LLM_STARTED = "llm_started"
    LLM_COMPLETED = "llm_completed"
    DECISION_MADE = "decision_made"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    DEPENDENCY_FAILED = "dependency_failed"
    RUN_TERMINATED = "run_terminated"


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """一次可关联、可序列化且不包含私有思维链的 Trace 事件。"""

    event_type: TraceEventType
    session_id: str
    run_id: str
    trace_id: str
    iteration: int
    timestamp: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id or not self.run_id or not self.trace_id:
            raise ValueError("trace event requires session, run and trace ids")
        if self.iteration < 0:
            raise ValueError("trace event iteration must not be negative")
        if not self.timestamp:
            raise ValueError("trace event timestamp must not be empty")
        object.__setattr__(
            self,
            "attributes",
            MappingProxyType(dict(self.attributes)),
        )


@dataclass(frozen=True, slots=True)
class Message:
    """一条归属于当前 Session 的不可变消息。"""

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


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """历史压缩摘要及其覆盖范围元数据。"""

    content: str
    covered_message_count: int

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("session summary content must not be empty")
        if self.covered_message_count < 1:
            raise ValueError("covered message count must be at least 1")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """LLM 请求 Runtime 执行的一次工具调用。"""

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("tool call id must not be empty")
        if not self.name:
            raise ValueError("tool name must not be empty")
        # 复制后再包成只读映射，防止调用方在运行期间悄悄修改参数。
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class FinalAnswerDecision:
    """LLM 已具备足够信息，可以结束循环并返回答案。"""

    answer: str
    decision_summary: str = ""

    def __post_init__(self) -> None:
        if not self.answer:
            raise ValueError("final answer must not be empty")


@dataclass(frozen=True, slots=True)
class ToolCallDecision:
    """LLM 决定执行一个或多个工具后继续下一轮。"""

    tool_calls: tuple[ToolCall, ...]
    decision_summary: str = ""

    def __post_init__(self) -> None:
        if not self.tool_calls:
            raise ValueError("tool call decision requires at least one call")
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("tool call ids must be unique within one decision")


type AgentDecision = FinalAnswerDecision | ToolCallDecision


class ToolResultStatus(StrEnum):
    """工具执行结果状态。失败结果仍会保留给 LLM 用于后续判断。"""

    SUCCESS = "success"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的结构化结果。"""

    call_id: str
    tool_name: str
    status: ToolResultStatus
    content: str
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ValueError("tool result requires call id and tool name")
        if not self.content:
            raise ValueError("tool result content must not be empty")
        if self.status is ToolResultStatus.ERROR and not self.error_type:
            raise ValueError("error tool result requires error_type")
        if self.status is ToolResultStatus.SUCCESS and self.error_type is not None:
            raise ValueError("successful tool result must not contain error_type")


@dataclass(frozen=True, slots=True)
class Session:
    """一个 Session 的完整、不可变状态快照。"""

    session_id: str
    messages: tuple[Message, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
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


class TerminationReason(StrEnum):
    """Agent Loop 所有受控终止出口。"""

    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    LLM_ERROR = "llm_error"
    CONTEXT_ERROR = "context_error"
    TOOL_ERROR = "tool_error"
    INVALID_DECISION = "invalid_decision"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunRequest:
    """发起一次 Agent 运行所需的最小输入。"""

    session_id: str
    user_input: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session id must not be empty")
        if not self.user_input:
            raise ValueError("user input must not be empty")


@dataclass(frozen=True, slots=True)
class RunResult:
    """一次运行的可审计结果，不使用模糊字符串表示终止状态。"""

    session_id: str
    run_id: str
    trace_id: str
    termination_reason: TerminationReason
    iterations: int
    answer: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id or not self.run_id or not self.trace_id:
            raise ValueError("run result requires session, run and trace ids")
        if self.iterations < 0:
            raise ValueError("iterations must not be negative")
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
