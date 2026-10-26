"""Context 有界压缩与工具结果信封的离线验收测试。

这些测试只验证本地确定性行为：不访问网络、不调用真实模型。真实 LLM 的压缩后
追问验收放在 ``tests/integration/``，必须显式用 ``-m integration`` 运行。
"""

import json

import pytest

from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    RunRequest,
    SessionSummary,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.context import (
    ContextBuilder,
    ContextConfig,
    ExtractiveHistorySummarizer,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore

EARLY_FACT = "验收代号是 ORANGE-42"


def context_chars(messages: tuple[Message, ...]) -> int:
    """模型输入总字符数，与 AgentLoop 的 prompt_chars 统计口径一致。"""

    return sum(len(message.content) for message in messages)


def make_long_history(
    store: InMemorySessionStore,
    session_id: str,
    *,
    count: int = 100,
    chars: int = 1_000,
) -> None:
    """写入一条早期事实，再追加足够长的历史。"""

    store.append_message(
        session_id,
        Message(MessageRole.USER, f"请记住：{EARLY_FACT}，后面会追问。"),
    )
    for index in range(count):
        body = f"第{index:03d}条历史：" + ("历史内容" * (chars // 4))
        store.append_message(
            session_id,
            Message(
                MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                body[:chars],
            ),
        )


def test_context_budget_bounds_input_and_summary() -> None:
    store = InMemorySessionStore()
    store.get_or_create("long-session")
    make_long_history(store, "long-session")
    builder = ContextBuilder(
        store,
        config=ContextConfig(
            max_history_messages=20,
            max_summary_chars=1_200,
            max_context_chars=8_000,
            max_tool_result_chars=200,
        ),
    )

    messages = builder.build(RunRequest("long-session", "代号是什么？"), ())
    session = store.get_or_create("long-session")

    assert context_chars(messages) <= 8_000
    assert session.summary is not None
    assert len(session.summary.content) <= 1_200
    assert "摘要已截断" in session.summary.content
    assert any(EARLY_FACT in message.content for message in messages)


def test_context_budget_keeps_covered_count_monotonic_and_complete() -> None:
    store = InMemorySessionStore()
    store.get_or_create("long-session")
    make_long_history(store, "long-session")
    builder = ContextBuilder(
        store,
        config=ContextConfig(
            max_history_messages=20,
            max_summary_chars=1_200,
            max_context_chars=8_000,
        ),
    )

    first = builder.build(RunRequest("long-session", "第一次追问"), ())
    covered_after_first = store.get_or_create("long-session").summary
    second = builder.build(RunRequest("long-session", "第二次追问"), ())
    covered_after_second = store.get_or_create("long-session").summary

    assert covered_after_first is not None
    assert covered_after_second is not None
    assert covered_after_second.covered_message_count >= (
        covered_after_first.covered_message_count
    )
    assert covered_after_second.covered_message_count < len(
        store.get_or_create("long-session").messages
    )
    # 未被摘要覆盖的消息必须仍然可见；已覆盖的不再重复出现在最近历史。
    assert first[-1].content == second[-1].content
    assert context_chars(second) <= 8_000


def test_context_budget_preserves_early_fact_after_repeated_growth() -> None:
    store = InMemorySessionStore()
    store.get_or_create("long-session")
    make_long_history(store, "long-session")
    builder = ContextBuilder(
        store,
        config=ContextConfig(
            max_history_messages=10,
            max_summary_chars=600,
            max_context_chars=4_000,
        ),
    )

    for index in range(5):
        messages = builder.build(RunRequest("long-session", f"追问 {index}"), ())
        assert any(EARLY_FACT in message.content for message in messages)
        assert context_chars(messages) <= 4_000
        store.append_message(
            "long-session",
            Message(MessageRole.USER, f"新增历史 {index}：" + "y" * 500),
        )


def test_summary_failure_does_not_advance_coverage() -> None:
    class FailingSummarizer:
        def summarize(self, previous_summary, messages):
            del previous_summary, messages
            raise RuntimeError("summary unavailable")

    store = InMemorySessionStore()
    store.get_or_create("session-a")
    make_long_history(store, "session-a", count=10, chars=1_000)
    store.set_summary("session-a", SessionSummary("旧摘要", 2))
    builder = ContextBuilder(
        store,
        summarizer=FailingSummarizer(),
        config=ContextConfig(max_history_messages=2, max_context_chars=2_000),
    )

    with pytest.raises(RuntimeError, match="summary unavailable"):
        builder.build(RunRequest("session-a", "继续"), ())

    summary = store.get_or_create("session-a").summary
    assert summary is not None
    assert summary.covered_message_count == 2
    assert summary.content == "旧摘要"


def test_tool_result_cannot_bypass_budget_through_summary() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall("call-1", "echo", {"text": "长结果"})
    result = ToolResult(
        "call-1",
        "echo",
        ToolResultStatus.SUCCESS,
        "x" * 5_000,
    )
    store.append_tool_interaction(
        "session-a",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )
    for index in range(6):
        store.append_message(
            "session-a",
            Message(MessageRole.USER, f"后续消息 {index}"),
        )
    builder = ContextBuilder(
        store,
        config=ContextConfig(
            max_history_messages=1,
            max_tool_result_chars=120,
            max_summary_chars=1_000,
            max_context_chars=4_000,
        ),
    )

    messages = builder.build(RunRequest("session-a", "继续"), ())
    summary = store.get_or_create("session-a").summary

    assert summary is not None
    assert len(summary.content) <= 1_000
    assert "x" * 200 not in summary.content
    assert all("x" * 200 not in message.content for message in messages)


def test_tool_message_renders_structured_envelope_with_redaction() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    call = ToolCall(
        "call-1",
        "echo",
        {"text": "hello", "api_key": "should-not-leak"},
    )
    result = ToolResult(
        "call-1",
        "echo",
        ToolResultStatus.SUCCESS,
        json.dumps({"ok": True, "token": "should-not-leak"}),
    )
    store.append_tool_interaction(
        "session-a",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )
    builder = ContextBuilder(store)

    messages = builder.build(RunRequest("session-a", "继续"), ())
    envelope = json.loads(messages[-1].content)

    assert envelope["call_id"] == "call-1"
    assert envelope["tool_name"] == "echo"
    assert envelope["arguments"]["text"] == "hello"
    assert envelope["arguments"]["api_key"] == "[REDACTED]"
    assert envelope["status"] == "success"
    assert envelope["error_type"] is None
    assert "should-not-leak" not in messages[-1].content
    assert messages[-1].tool_call_id == "call-1"


def test_tool_envelope_preserves_error_type_and_multiple_calls() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    ok_call = ToolCall("call-1", "calculator", {"expression": "6 * 7"})
    ok_result = ToolResult(
        "call-1",
        "calculator",
        ToolResultStatus.SUCCESS,
        "42",
    )
    failed_call = ToolCall("call-2", "unknown", {"x": 1})
    failed_result = ToolResult(
        "call-2",
        "unknown",
        ToolResultStatus.ERROR,
        "unknown tool: unknown",
        error_type="UnknownToolError",
    )
    for call, result in ((ok_call, ok_result), (failed_call, failed_result)):
        store.append_tool_interaction(
            "session-a",
            call,
            result,
            Message(MessageRole.TOOL, result.content, result.call_id),
        )
    builder = ContextBuilder(store)

    messages = builder.build(RunRequest("session-a", "继续"), ())
    first, second = (json.loads(message.content) for message in messages[-2:])

    assert first["call_id"] == "call-1"
    assert first["status"] == "success"
    assert second["call_id"] == "call-2"
    assert second["status"] == "error"
    assert second["error_type"] == "UnknownToolError"
    assert second["tool_name"] == "unknown"


def test_tool_envelope_does_not_import_other_session_results() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.get_or_create("session-b")
    call = ToolCall("call-1", "echo", {"text": "本地"})
    local = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "本地结果")
    store.append_tool_interaction(
        "session-a",
        call,
        local,
        Message(MessageRole.TOOL, local.content, call.call_id),
    )
    foreign = ToolResult(
        "call-1",
        "echo",
        ToolResultStatus.SUCCESS,
        "来自其它 Session 的结果",
    )

    messages = ContextBuilder(store).build(
        RunRequest("session-a", "继续"),
        (foreign,),
    )

    assert "来自其它 Session 的结果" not in messages[-1].content
    assert "本地结果" in messages[-1].content


def test_context_config_rejects_non_positive_budgets() -> None:
    for field in (
        "max_history_messages",
        "max_tool_result_chars",
        "max_summary_chars",
        "max_context_chars",
    ):
        with pytest.raises(ValueError, match="positive integer"):
            ContextConfig(**{field: 0})
        with pytest.raises(ValueError, match="positive integer"):
            ContextConfig(**{field: -1})
        with pytest.raises(ValueError, match="positive integer"):
            ContextConfig(**{field: True})


def test_extractive_summarizer_honours_its_own_budget() -> None:
    summarizer = ExtractiveHistorySummarizer(max_summary_chars=120)
    messages = tuple(
        Message(MessageRole.USER, f"事实 {index}：" + "z" * 80)
        for index in range(10)
    )

    summary = summarizer.summarize(None, messages)

    assert len(summary) <= 120
    assert "摘要已截断" in summary


def test_context_rejects_budget_that_cannot_hold_latest_group() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.append_message("session-a", Message(MessageRole.USER, "x" * 500))
    builder = ContextBuilder(
        store,
        config=ContextConfig(max_context_chars=100),
    )

    with pytest.raises(Exception, match="max_context_chars"):
        builder.build(RunRequest("session-a", "继续"), ())
