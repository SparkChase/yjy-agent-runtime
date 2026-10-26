"""Agent Runtime 的核心领域对象。

这些类型只描述 Runtime 中稳定的事实和约束，不依赖模型 SDK、数据库或
命令行。将非法输入尽量阻挡在对象创建阶段，可以避免核心循环长期处理
含义不清的普通字典。

这里定义了整个 Agent Runtime 最基础的“事实对象”：
- Message（包含系统、用户、助手、工具四种角色）
- ToolCall（LLM 想调用的工具调用）
- ToolResult（工具执行后的结构化结果，无论成功失败）
- Session（完整 Session 快照，包含历史消息 + 摘要）
- 各种 Decision 类型（LLM 最终决定是直接回答还是调用工具）

所有这些都是不可变对象 + 后置校验，防止后面核心循环出 bug。

"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


def _contains_surrogate(value: str) -> bool:
    """判断字符串是否含 UTF-16 代理项（lone surrogate）。

    终端把非法 UTF-8 字节按 ``surrogateescape`` 解码时会产生 ``\\udcXX``。
    这种字符无法再编码成 UTF-8，写 Session 文件时会抛 UnicodeEncodeError。
    在领域对象构造期拒绝它，比等到落盘时再炸更早、更清楚。
    """

    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _ensure_no_surrogates(value: str, label: str) -> None:
    if _contains_surrogate(value):
        raise ValueError(f"{label} must not contain unicode surrogates")


class MessageRole(StrEnum):
    """模型上下文中允许出现的消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"  # TOOL 角色有特殊字段 tool_call_id，这是为了把工具调用和结果一一对应。


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
    """一条归属于当前 Session 的不可变消息。

    不可变（frozen）是刻意的：Session 快照会在多个 Store 之间传递，如果消息能被
    原地修改，历史就可能被某一轮循环悄悄改写，Trace 和实际输入会对不上。
    """

    role: MessageRole
    content: str
    # tool_call_id 只在 TOOL 角色时有意义，用于把工具结果对回某次调用。
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("message content must not be empty")
        _ensure_no_surrogates(self.content, "message content")
        if self.role is MessageRole.TOOL and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.role is not MessageRole.TOOL and self.tool_call_id is not None:
            raise ValueError("only tool message may contain tool_call_id")


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """历史压缩摘要及其覆盖范围元数据。"""

    content: str
    covered_message_count: int # 记录这个摘要覆盖到了第几条消息

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("session summary content must not be empty")
        _ensure_no_surrogates(self.content, "session summary content")
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
        _ensure_no_surrogates(self.answer, "final answer")


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


class TimeoutSemantics(StrEnum):
    """一次工具执行的超时语义。

    ``join(timeout)`` 只能停止等待，不能停止线程。把“等待超时”当成“执行已取消”
    会让调用方误以为外部状态不会再变。因此结果里必须写清楚这次执行属于哪种语义：

    - ``SYNCHRONOUS``：有副作用的工具在调用线程同步执行，根本没有超时，
      返回值就是真实结果，不存在“报告超时后又写入”。
    - ``ABANDONED_SAFE``：无副作用的工具等待超时，工作线程被放弃，但它按约定
      不改变外部状态，晚结束也不会造成可观察影响。
    - ``COOPERATIVE``：有副作用的工具实现了协作式截止时间，超时后会在写入前
      检查截止时间并放弃提交，因此“已报告超时”与“稍后仍然写入”不会同时发生。
    """

    SYNCHRONOUS = "synchronous"
    ABANDONED_SAFE = "abandoned_safe"
    COOPERATIVE = "cooperative"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的结构化结果。"""

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


@dataclass(frozen=True, slots=True)
class Session:
    """一个 Session 的完整、不可变状态快照。"""

    session_id: str
    messages: tuple[Message, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    todos: tuple[str, ...] = ()
    summary: SessionSummary | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session id must not be empty")
        # 摘要的覆盖范围不能超过真实消息数：这是“压缩元数据”和“历史”之间
        # 最重要的一致性约束，能挡住 covered 被错误推进导致的丢历史。
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
        _ensure_no_surrogates(self.user_input, "user input")


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
