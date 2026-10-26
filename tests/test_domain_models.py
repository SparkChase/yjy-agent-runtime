"""领域模型的规则测试。"""

import pytest

from yjy_agent_runtime.domain import (
    FinalAnswerDecision,
    Message,
    MessageRole,
    RunResult,
    TerminationReason,
    ToolCall,
    ToolCallDecision,
)


def test_tool_message_requires_call_id() -> None:
    with pytest.raises(ValueError, match="tool_call_id"):
        Message(role=MessageRole.TOOL, content="42")


def test_tool_call_copies_arguments_as_read_only_mapping() -> None:
    arguments = {"expression": "6 * 7"}
    call = ToolCall(call_id="call-1", name="calculator", arguments=arguments)

    arguments["expression"] = "changed"

    assert call.arguments["expression"] == "6 * 7"
    with pytest.raises(TypeError):
        call.arguments["expression"] = "invalid"  # type: ignore[index]


def test_tool_call_decision_rejects_duplicate_call_ids() -> None:
    call = ToolCall(call_id="same", name="calculator")

    with pytest.raises(ValueError, match="unique"):
        ToolCallDecision(tool_calls=(call, call))


def test_final_result_requires_answer() -> None:
    with pytest.raises(ValueError, match="requires answer"):
        RunResult(
            session_id="session-1",
            trace_id="trace-1",
            termination_reason=TerminationReason.FINAL_ANSWER,
            iterations=1,
        )


def test_final_answer_decision_accepts_public_summary() -> None:
    decision = FinalAnswerDecision(
        answer="结果是 42。",
        decision_summary="已有足够信息回答",
    )

    assert decision.answer == "结果是 42。"
