"""基于注册表的工具执行器测试。"""

from collections.abc import Mapping
from time import perf_counter, sleep
from typing import Any

import pytest

from yjy_agent_runtime.domain import (
    TimeoutSemantics,
    ToolCall,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.tools import (
    Deadline,
    RegistryToolExecutor,
    ToolDefinition,
    ToolRegistry,
)


class RecordingTool:
    """记录收到的参数，验证非法参数不会进入工具实现。"""

    def __init__(
        self,
        *,
        result: object = "ok",
        schema: Mapping[str, Any] | None = None,
        has_side_effects: bool = False,
    ) -> None:
        self._definition = ToolDefinition(
            name="echo",
            description="返回输入",
            parameters=schema
            or {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            has_side_effects=has_side_effects,
        )
        self.result = result
        self.calls: list[Mapping[str, Any]] = []

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        self.calls.append(arguments)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result  # type: ignore[return-value]


def make_executor(
    tool: RecordingTool,
    *,
    max_result_chars: int = 10_000,
    timeout_seconds: float = 30.0,
) -> RegistryToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return RegistryToolExecutor(
        registry,
        max_result_chars=max_result_chars,
        timeout_seconds=timeout_seconds,
    )


def test_executor_validates_arguments_before_invoking_tool() -> None:
    tool = RecordingTool()
    executor = make_executor(tool)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": 42}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "ToolArgumentsValidationError"
    assert tool.calls == []


def test_executor_invokes_registered_tool_with_valid_arguments() -> None:
    tool = RecordingTool(result="hello")
    executor = make_executor(tool)
    arguments = {"text": "hello"}

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", arguments),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == "hello"
    assert dict(tool.calls[0]) == arguments


def test_executor_returns_structured_unknown_tool_error() -> None:
    executor = RegistryToolExecutor(ToolRegistry())

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "missing"),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "UnknownToolError"
    assert "unknown tool: missing" in result.content


def test_executor_preserves_tool_exception_type_and_message() -> None:
    executor = make_executor(
        RecordingTool(result=TimeoutError("tool timed out"))
    )

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "TimeoutError"
    assert result.content == "tool timed out"


def test_executor_rejects_invalid_tool_schema() -> None:
    tool = RecordingTool(schema={"type": "not-a-real-type"})
    executor = make_executor(tool)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "InvalidToolSchemaError"
    assert tool.calls == []


@pytest.mark.parametrize("result", ["", 42])
def test_executor_rejects_invalid_tool_results(result: object) -> None:
    executor = make_executor(RecordingTool(result=result))

    tool_result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert tool_result.status is ToolResultStatus.ERROR
    assert tool_result.error_type == "InvalidToolResultError"


def test_executor_limits_success_and_error_result_size() -> None:
    success = make_executor(
        RecordingTool(result="abcdefghij"),
        max_result_chars=8,
    ).execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )
    failure = make_executor(
        RecordingTool(result=RuntimeError("abcdefghij")),
        max_result_chars=8,
    ).execute(
        "session-a",
        ToolCall("call-2", "echo", {"text": "hello"}),
    )

    assert success.content == "...[trun"
    assert failure.content == "...[trun"
    assert len(success.content) == 8
    assert len(failure.content) == 8


def test_executor_rejects_empty_session_id_and_invalid_limit() -> None:
    with pytest.raises(ValueError, match="max_result_chars"):
        RegistryToolExecutor(ToolRegistry(), max_result_chars=0)

    executor = RegistryToolExecutor(ToolRegistry())
    with pytest.raises(ValueError, match="session id"):
        executor.execute("", ToolCall("call-1", "missing"))


def test_deadline_requires_a_positive_timeout() -> None:
    with pytest.raises(ValueError, match="deadline timeout"):
        Deadline(0)


def test_executor_returns_timeout_error_when_tool_exceeds_budget() -> None:
    class SlowTool(RecordingTool):
        def invoke(self, arguments: Mapping[str, Any]) -> str:
            sleep(0.05)
            return super().invoke(arguments)

    tool = SlowTool()
    executor = make_executor(tool, timeout_seconds=0.001)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "TimeoutError"
    # 无副作用工具超时后线程仍可能活着，但按约定不会改变外部状态。
    assert result.timeout_semantics is TimeoutSemantics.ABANDONED_SAFE


def test_side_effect_tool_runs_inline_instead_of_faking_a_cancellation() -> None:
    """有副作用工具不能在"报告超时"之后继续写。

    执行器读到 ``has_side_effects=True`` 后改为调用线程同步执行：慢就是慢，
    结果是真实的成功，而不是"先报 TimeoutError、稍后待办里又出现一条"。
    """

    writes: list[str] = []

    class SlowWriter(RecordingTool):
        def invoke(self, arguments: Mapping[str, Any]) -> str:
            sleep(0.05)
            writes.append(str(arguments["text"]))
            return "written"

    executor = make_executor(
        SlowWriter(has_side_effects=True),
        timeout_seconds=0.001,
    )

    started = perf_counter()
    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "late write"}),
    )
    elapsed = perf_counter() - started

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == "written"
    assert result.timeout_semantics is TimeoutSemantics.SYNCHRONOUS
    assert elapsed >= 0.05
    assert writes == ["late write"]


def test_cooperative_side_effect_tool_cancels_before_commit() -> None:
    """实现协作截止时间的副作用工具，超时后不得提交写入。"""

    writes: list[str] = []

    class CooperativeWriter(RecordingTool):
        def invoke(self, arguments: Mapping[str, Any]) -> str:
            raise AssertionError("cooperative tool must use the deadline entrypoint")

        def invoke_with_session_deadline(
            self,
            session_id: str,
            arguments: Mapping[str, Any],
            deadline: Deadline,
        ) -> str:
            del session_id
            sleep(0.05)
            deadline.raise_if_expired()
            writes.append(str(arguments["text"]))
            return "written"

    executor = make_executor(
        CooperativeWriter(has_side_effects=True),
        timeout_seconds=0.001,
    )

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "late write"}),
    )
    # 等待后台线程真正跑到截止时间检查点，确认它没有偷偷写入。
    sleep(0.1)

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "TimeoutError"
    assert result.timeout_semantics is TimeoutSemantics.COOPERATIVE
    assert writes == []


def test_cooperative_side_effect_tool_reports_success_when_fast_enough() -> None:
    writes: list[str] = []

    class FastWriter(RecordingTool):
        def invoke_with_session_deadline(
            self,
            session_id: str,
            arguments: Mapping[str, Any],
            deadline: Deadline,
        ) -> str:
            del session_id
            deadline.raise_if_expired()
            writes.append(str(arguments["text"]))
            return "written"

    executor = make_executor(FastWriter(has_side_effects=True))

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "ok"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.timeout_semantics is TimeoutSemantics.COOPERATIVE
    assert writes == ["ok"]
