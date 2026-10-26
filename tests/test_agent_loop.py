"""线性 Agent Loop 的应用层测试。"""

from collections import deque
from collections.abc import Sequence

from yjy_agent_runtime.agent import AgentLoop, LoopConfig
from yjy_agent_runtime.domain import (
    AgentDecision,
    FinalAnswerDecision,
    Message,
    MessageRole,
    RunRequest,
    TerminationReason,
    ToolCall,
    ToolCallDecision,
    ToolResult,
    ToolResultStatus,
)


class StubContextProvider:
    """记录每轮收到的工具结果，并生成最小测试 Context。"""

    def __init__(self) -> None:
        self.seen_results: list[tuple[ToolResult, ...]] = []

    def build(
        self,
        request: RunRequest,
        tool_results: Sequence[ToolResult],
    ) -> Sequence:
        self.seen_results.append(tuple(tool_results))
        messages = [Message(MessageRole.USER, request.user_input)]
        messages.extend(
            Message(MessageRole.TOOL, result.content, result.call_id)
            for result in tool_results
        )
        return messages


class FakeLLMClient:
    """按照预设顺序返回决策，避免单元测试依赖真实模型。"""

    def __init__(self, decisions: Sequence[AgentDecision]) -> None:
        self._decisions = deque(decisions)

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        del messages
        return self._decisions.popleft()


class StubToolExecutor:
    """记录调用信息并返回固定成功结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ToolCall]] = []

    def execute(self, session_id: str, call: ToolCall) -> ToolResult:
        self.calls.append((session_id, call))
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.SUCCESS,
            content="42",
        )


class InMemoryTraceRecorder:
    """保存测试期间产生的结构化事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, **attributes: object) -> None:
        self.events.append((event, attributes))


def make_loop(
    decisions: Sequence[AgentDecision],
    *,
    max_iterations: int = 8,
) -> tuple[AgentLoop, StubContextProvider, StubToolExecutor, InMemoryTraceRecorder]:
    context = StubContextProvider()
    tools = StubToolExecutor()
    trace = InMemoryTraceRecorder()
    loop = AgentLoop(
        context_provider=context,
        llm_client=FakeLLMClient(decisions),
        tool_executor=tools,
        trace_recorder=trace,
        config=LoopConfig(max_iterations=max_iterations),
    )
    return loop, context, tools, trace


def test_loop_returns_direct_answer_in_one_iteration() -> None:
    loop, _context, tools, trace = make_loop(
        [FinalAnswerDecision(answer="你好，我可以帮你。")]
    )

    result = loop.run(RunRequest("session-a", "你好"))

    assert result.answer == "你好，我可以帮你。"
    assert result.iterations == 1
    assert result.termination_reason is TerminationReason.FINAL_ANSWER
    assert tools.calls == []
    assert trace.events[-1][1]["reason"] == "final_answer"


def test_loop_executes_tool_and_returns_result_to_next_context() -> None:
    call = ToolCall("call-1", "calculator", {"expression": "6 * 7"})
    loop, context, tools, _trace = make_loop(
        [
            ToolCallDecision((call,)),
            FinalAnswerDecision(answer="结果是 42。"),
        ]
    )

    result = loop.run(RunRequest("session-a", "计算 6 * 7"))

    assert result.iterations == 2
    assert tools.calls == [("session-a", call)]
    assert context.seen_results[0] == ()
    assert context.seen_results[1][0].content == "42"


def test_loop_supports_multiple_tool_calls_in_one_decision() -> None:
    first = ToolCall("call-1", "calculator", {"expression": "6 * 7"})
    second = ToolCall("call-2", "calculator", {"expression": "8 * 8"})
    loop, context, tools, _trace = make_loop(
        [
            ToolCallDecision((first, second)),
            FinalAnswerDecision(answer="结果分别是 42 和 64。"),
        ]
    )

    result = loop.run(RunRequest("session-a", "计算两个表达式"))

    assert result.iterations == 2
    assert [call.call_id for _, call in tools.calls] == ["call-1", "call-2"]
    assert [item.content for item in context.seen_results[1]] == ["42", "42"]


def test_loop_stops_at_max_iterations() -> None:
    first = ToolCall("call-1", "calculator")
    second = ToolCall("call-2", "calculator")
    loop, _context, tools, trace = make_loop(
        [
            ToolCallDecision((first,)),
            ToolCallDecision((second,)),
        ],
        max_iterations=2,
    )

    result = loop.run(RunRequest("session-a", "继续调用工具"))

    assert result.answer is None
    assert result.iterations == 2
    assert result.termination_reason is TerminationReason.MAX_ITERATIONS
    assert [call.call_id for _, call in tools.calls] == ["call-1", "call-2"]
    assert trace.events[-1][1]["reason"] == "max_iterations"


def test_tool_execution_keeps_session_id_explicit() -> None:
    call = ToolCall("call-1", "todo", {"text": "学习 Agent Loop"})
    loop, _context, tools, _trace = make_loop(
        [
            ToolCallDecision((call,)),
            FinalAnswerDecision(answer="已记录。"),
        ]
    )

    loop.run(RunRequest("isolated-session", "记录待办"))

    assert tools.calls[0][0] == "isolated-session"


def test_loop_config_rejects_non_positive_limit() -> None:
    try:
        LoopConfig(max_iterations=0)
    except ValueError as error:
        assert "at least 1" in str(error)
    else:
        raise AssertionError("LoopConfig should reject zero max_iterations")
