"""进程内 SessionStore 的隔离与恢复规则测试。"""

import pytest

from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    SessionSummary,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.session import (
    InMemorySessionStore,
    SessionNotFoundError,
)


def test_get_or_create_returns_same_session_history_for_continuous_turns() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")

    store.append_message("session-a", Message(MessageRole.USER, "第一问"))
    store.append_message("session-a", Message(MessageRole.ASSISTANT, "第一答"))
    restored = store.get_or_create("session-a")

    assert [message.content for message in restored.messages] == ["第一问", "第一答"]


def test_different_sessions_keep_messages_and_tool_data_isolated() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.get_or_create("session-b")
    call = ToolCall("call-a", "echo", {"text": "仅属于 A"})
    result = ToolResult(
        call_id="call-a",
        tool_name="echo",
        status=ToolResultStatus.SUCCESS,
        content="仅属于 A",
    )

    store.append_message("session-a", Message(MessageRole.USER, "A 的消息"))
    store.append_tool_interaction(
        "session-a",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )
    store.append_message("session-b", Message(MessageRole.USER, "B 的消息"))

    session_a = store.get_or_create("session-a")
    session_b = store.get_or_create("session-b")
    assert [item.content for item in session_a.messages] == ["A 的消息", "仅属于 A"]
    assert session_a.tool_calls == (call,)
    assert session_a.tool_results == (result,)
    assert [item.content for item in session_b.messages] == ["B 的消息"]
    assert session_b.tool_calls == ()
    assert session_b.tool_results == ()


def test_get_does_not_create_missing_session() -> None:
    store = InMemorySessionStore()

    assert store.get("missing") is None
    assert store.get("missing") is None


def test_updates_require_an_existing_session() -> None:
    store = InMemorySessionStore()

    with pytest.raises(SessionNotFoundError, match="missing"):
        store.append_message(
            "missing",
            Message(MessageRole.USER, "不会被保存"),
        )


def test_summary_metadata_is_scoped_to_its_session() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.get_or_create("session-b")
    store.append_message(
        "session-a",
        Message(MessageRole.USER, "已被摘要的消息"),
    )
    summary = SessionSummary(
        "用户已经完成首次提问。",
        covered_message_count=1,
    )

    store.set_summary("session-a", summary)

    assert store.get_or_create("session-a").summary == summary
    assert store.get_or_create("session-b").summary is None


def test_tool_call_and_result_must_match() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall("call-1", "echo")
    wrong_result = ToolResult(
        call_id="call-2",
        tool_name="echo",
        status=ToolResultStatus.SUCCESS,
        content="unexpected",
    )

    with pytest.raises(ValueError, match="same interaction"):
        store.append_tool_interaction(
            "session-a",
            call,
            wrong_result,
            Message(MessageRole.TOOL, "unexpected", "call-1"),
        )

    assert store.get_or_create("session-a").tool_calls == ()


def test_tool_interaction_writes_call_result_and_message_together() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall("call-1", "echo", {"text": "hi"})
    result = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "hi")

    updated = store.append_tool_interaction(
        "session-a",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )

    assert updated.tool_calls == (call,)
    assert updated.tool_results == (result,)
    assert updated.messages[-1].tool_call_id == "call-1"


def test_standalone_tool_message_is_rejected() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")

    with pytest.raises(ValueError, match="together with their tool interaction"):
        store.append_message(
            "session-a",
            Message(MessageRole.TOOL, "半条交互", "call-1"),
        )

    assert store.get_or_create("session-a").messages == ()


def test_tool_interaction_message_must_describe_the_same_call() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall("call-1", "echo")
    result = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "hi")

    with pytest.raises(ValueError, match="same call id"):
        store.append_tool_interaction(
            "session-a",
            call,
            result,
            Message(MessageRole.TOOL, result.content, "call-2"),
        )
    with pytest.raises(ValueError, match="must match the tool result"):
        store.append_tool_interaction(
            "session-a",
            call,
            result,
            Message(MessageRole.TOOL, "别的结果", "call-1"),
        )

    assert store.get_or_create("session-a").tool_calls == ()


def test_duplicate_tool_call_id_is_rejected() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall("call-1", "echo")
    result = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "hi")
    store.append_tool_interaction(
        "session-a",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )

    with pytest.raises(ValueError, match="already exists"):
        store.append_tool_interaction(
            "session-a",
            call,
            result,
            Message(MessageRole.TOOL, result.content, call.call_id),
        )

    session = store.get_or_create("session-a")
    assert len(session.tool_calls) == 1
    assert len(session.messages) == 1
