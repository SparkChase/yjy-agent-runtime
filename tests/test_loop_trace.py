"""AgentLoop 写入 Trace 的内容测试。

关注"审计能回答什么"：每轮输入的字符量、工具调用的参数、结果的摘要，
以及超长文本是否被截断。脱敏本身由 Recorder 负责，这里只验证 Trace 是否
携带了足够且不膨胀的信息。
"""

from collections import deque
from collections.abc import Sequence

from yjy_agent_runtime.agent import AgentLoop, LoopConfig
from yjy_agent_runtime.domain import (
    AgentDecision,
    FinalAnswerDecision,
    Message,
    MessageRole,
    RunRequest,
    ToolCall,
    ToolCallDecision,
    ToolResult,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.trace import InMemoryTraceRecorder


class FakeLLM:
    """按预设顺序返回决策，避免真实网络。"""

    def __init__(self, decisions: Sequence[AgentDecision]) -> None:
        self._decisions = deque(decisions)

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        del messages
        return self._decisions.popleft()


class Context:
    """生成最小 Context，并保证至少一条消息有内容。"""

    def build(
        self,
        request: RunRequest,
        tool_results: Sequence[ToolResult],
    ) -> Sequence[Message]:
        del tool_results
        return (Message(MessageRole.USER, request.user_input),)


class FixedExecutor:
    """返回固定内容的工具执行器，便于断言结果摘要。"""

    def __init__(self, content: str) -> None:
        self._content = content

    def execute(self, session_id: str, call: ToolCall) -> ToolResult:
        del session_id
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.SUCCESS,
            content=self._content,
        )


def run_once(
    *,
    decisions: Sequence[AgentDecision],
    tool_content: str = "42",
    max_trace_value_chars: int = 500,
) -> tuple[InMemoryTraceRecorder, object]:
    trace = InMemoryTraceRecorder()
    loop = AgentLoop(
        context_provider=Context(),
        llm_client=FakeLLM(decisions),
        session_store=InMemorySessionStore(),
        tool_executor=FixedExecutor(tool_content),
        trace_recorder=trace,
        config=LoopConfig(max_trace_value_chars=max_trace_value_chars),
    )
    result = loop.run(RunRequest("session-a", "计算 6 * 7"))
    return trace, result


def find_events(trace: InMemoryTraceRecorder, event_type: str):
    return [
        event for event in trace.events if event.event_type.value == event_type
    ]


def test_iteration_records_prompt_size() -> None:
    trace, _ = run_once(decisions=[FinalAnswerDecision("直接回答")])

    iteration = find_events(trace, "iteration_started")[0]

    assert iteration.attributes["message_count"] == 1
    assert iteration.attributes["prompt_chars"] == len("计算 6 * 7")


def test_tool_events_record_arguments_and_result_summary() -> None:
    trace, _ = run_once(
        decisions=[
            ToolCallDecision(
                (ToolCall("call-1", "calculator", {"expression": "6 * 7"}),)
            ),
            FinalAnswerDecision("结果是 42。"),
        ],
        tool_content="42",
    )

    started = find_events(trace, "tool_started")[0]
    completed = find_events(trace, "tool_completed")[0]

    assert started.attributes["tool_name"] == "calculator"
    assert started.attributes["arguments"] == {"expression": "6 * 7"}
    assert completed.attributes["status"] == "success"
    assert completed.attributes["result_summary"] == "42"


def test_long_tool_result_is_truncated_in_trace() -> None:
    trace, _ = run_once(
        decisions=[
            ToolCallDecision((ToolCall("call-1", "echo", {"text": "x"}),)),
            FinalAnswerDecision("完成"),
        ],
        tool_content="x" * 5000,
        max_trace_value_chars=100,
    )

    summary = find_events(trace, "tool_completed")[0].attributes["result_summary"]

    assert len(summary) == 100
    assert summary.endswith("...[truncated]")


def test_trace_value_limit_must_be_positive() -> None:
    try:
        LoopConfig(max_trace_value_chars=0)
    except ValueError as error:
        assert "max_trace_value_chars" in str(error)
    else:
        raise AssertionError("LoopConfig should reject zero trace value limit")
