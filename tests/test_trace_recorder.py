"""进程内 TraceRecorder 的结构化存储与脱敏测试。"""

import pytest

from yjy_agent_runtime.domain import TraceEvent, TraceEventType
from yjy_agent_runtime.infrastructure.trace import (
    InMemoryTraceRecorder,
    TraceRedactionConfig,
)


def make_event(attributes: dict[str, object]) -> TraceEvent:
    return TraceEvent(
        event_type=TraceEventType.TOOL_STARTED,
        session_id="session-a",
        run_id="run-a",
        trace_id="trace-a",
        iteration=1,
        timestamp="2026-10-26T05:31:00+00:00",
        attributes=attributes,
    )


def test_recorder_keeps_correlated_event_snapshot() -> None:
    recorder = InMemoryTraceRecorder()
    event = make_event({"tool_name": "echo", "call_id": "call-1"})

    recorder.record(event)

    assert recorder.events == (event,)
    assert recorder.events[0].session_id == "session-a"
    assert recorder.events[0].run_id == "run-a"
    assert recorder.events[0].trace_id == "trace-a"


def test_recorder_recursively_redacts_sensitive_fields() -> None:
    recorder = InMemoryTraceRecorder()
    event = make_event(
        {
            "arguments": {
                "user": "connor",
                "api_key": "should-not-leak",
                "nested": [{"Authorization": "Bearer should-not-leak"}],
            },
            "password": "should-not-leak",
        }
    )

    recorder.record(event)

    attributes = recorder.events[0].attributes
    assert attributes["password"] == "[REDACTED]"
    assert attributes["arguments"] == {
        "user": "connor",
        "api_key": "[REDACTED]",
        "nested": [{"Authorization": "[REDACTED]"}],
    }
    assert event.attributes["password"] == "should-not-leak"


def test_recorder_returns_immutable_event_collection_snapshot() -> None:
    recorder = InMemoryTraceRecorder()
    recorder.record(make_event({"stage": "llm"}))

    snapshot = recorder.events

    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1


def test_redaction_config_rejects_empty_rules_and_replacement() -> None:
    with pytest.raises(ValueError, match="field names must not be empty"):
        TraceRedactionConfig(sensitive_field_names=frozenset())

    with pytest.raises(ValueError, match="replacement must not be empty"):
        TraceRedactionConfig(replacement="")
