"""基于注册表的工具执行器测试。"""

from collections.abc import Mapping
from time import sleep
from typing import Any

import pytest

from yjy_agent_runtime.domain import ToolCall, ToolResultStatus
from yjy_agent_runtime.infrastructure.tools import (
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
