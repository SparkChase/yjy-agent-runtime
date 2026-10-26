"""JSONL TraceRecorder、共享脱敏与扇出 Recorder 的测试。

覆盖：逐行落盘、按 run_id 关联、递归脱敏、截断与关闭语义、写入失败显式报错。
"""

import json
from pathlib import Path

import pytest

from yjy_agent_runtime.domain import TraceEvent, TraceEventType
from yjy_agent_runtime.infrastructure.trace import (
    FanOutTraceRecorder,
    InMemoryTraceRecorder,
    JsonlTraceRecorder,
    TraceWriteError,
)


def make_event(
    attributes: dict[str, object],
    *,
    event_type: TraceEventType = TraceEventType.TOOL_STARTED,
    run_id: str = "run-a",
) -> TraceEvent:
    return TraceEvent(
        event_type=event_type,
        session_id="session-a",
        run_id=run_id,
        trace_id="trace-a",
        iteration=1,
        timestamp="2026-10-26T05:31:00+00:00",
        attributes=attributes,
    )


def read_lines(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_jsonl_recorder_persists_one_event_per_line(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    with JsonlTraceRecorder(path) as recorder:
        recorder.record(make_event({"tool_name": "echo"}))
        recorder.record(
            make_event(
                {"reason": "final_answer"},
                event_type=TraceEventType.RUN_TERMINATED,
            )
        )

    lines = read_lines(path)

    assert [line["event_type"] for line in lines] == [
        "tool_started",
        "run_terminated",
    ]
    # 每条事件都携带可关联的三元组与轮次。
    assert all(line["run_id"] == "run-a" for line in lines)
    assert all(line["session_id"] == "session-a" for line in lines)
    assert all(line["trace_id"] == "trace-a" for line in lines)
    assert all(line["iteration"] == 1 for line in lines)


def test_jsonl_recorder_redacts_nested_sensitive_fields(tmp_path: Path) -> None:
    path = tmp_path / "redacted.jsonl"
    with JsonlTraceRecorder(path) as recorder:
        recorder.record(
            make_event(
                {
                    "arguments": {
                        "text": "hello",
                        "api_key": "should-not-leak",
                        "nested": [{"authorization": "Bearer should-not-leak"}],
                    }
                }
            )
        )

    raw = path.read_text(encoding="utf-8")
    attributes = read_lines(path)[0]["attributes"]

    assert "should-not-leak" not in raw
    assert attributes["arguments"]["text"] == "hello"
    assert attributes["arguments"]["api_key"] == "[REDACTED]"
    assert attributes["arguments"]["nested"] == [{"authorization": "[REDACTED]"}]


def test_jsonl_recorder_serializes_tuples_and_mapping_proxies(tmp_path: Path) -> None:
    """tuple / MappingProxyType 必须能落盘，否则真实运行会在写 Trace 时崩溃。"""

    path = tmp_path / "containers.jsonl"
    with JsonlTraceRecorder(path) as recorder:
        recorder.record(make_event({"items": ("a", "b"), "meta": {"k": ("v",)}}))

    attributes = read_lines(path)[0]["attributes"]

    assert attributes["items"] == ["a", "b"]
    assert attributes["meta"] == {"k": ["v"]}


def test_jsonl_recorder_appends_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "append.jsonl"
    with JsonlTraceRecorder(path) as first:
        first.record(make_event({"step": 1}))
    with JsonlTraceRecorder(path) as second:
        second.record(make_event({"step": 2}))

    assert [line["attributes"]["step"] for line in read_lines(path)] == [1, 2]


def test_jsonl_recorder_reports_write_after_close(tmp_path: Path) -> None:
    recorder = JsonlTraceRecorder(tmp_path / "closed.jsonl")
    recorder.close()

    with pytest.raises(TraceWriteError, match="closed"):
        recorder.record(make_event({"step": 1}))


def test_jsonl_recorder_reports_unopenable_path(tmp_path: Path) -> None:
    """目标是目录时无法打开文件，必须保留明确错误，不静默丢弃审计。"""

    with pytest.raises(TraceWriteError, match="unable to open"):
        JsonlTraceRecorder(tmp_path)


def test_fanout_recorder_writes_to_all_downstream(tmp_path: Path) -> None:
    path = tmp_path / "fanout.jsonl"
    memory = InMemoryTraceRecorder()
    with JsonlTraceRecorder(path) as file:
        recorder = FanOutTraceRecorder((memory, file))
        recorder.record(make_event({"tool_name": "calculator"}))

    assert len(memory.events) == 1
    assert read_lines(path)[0]["attributes"]["tool_name"] == "calculator"


def test_fanout_recorder_requires_downstream() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FanOutTraceRecorder(())


def test_fanout_recorder_propagates_downstream_failure(tmp_path: Path) -> None:
    """下游写入失败必须向上传播，不能假报 Trace 已保存。"""

    closed = JsonlTraceRecorder(tmp_path / "closed.jsonl")
    closed.close()
    recorder = FanOutTraceRecorder((InMemoryTraceRecorder(), closed))

    with pytest.raises(TraceWriteError):
        recorder.record(make_event({"step": 1}))
