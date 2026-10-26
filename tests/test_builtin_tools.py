"""最小内置工具测试。"""

from yjy_agent_runtime.domain import ToolCall, ToolResultStatus
from yjy_agent_runtime.infrastructure.tools import (
    EchoTool,
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)


def test_echo_tool_definition_has_no_side_effects() -> None:
    definition = EchoTool().definition

    assert definition.name == "echo"
    assert definition.has_side_effects is False
    assert definition.parameters["additionalProperties"] is False


def test_echo_tool_runs_through_registry_executor() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == "hello"


def test_echo_tool_rejects_empty_text_before_invocation() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": ""}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "ToolArgumentsValidationError"


def test_register_builtin_tools_exposes_echo_schema() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)

    assert [definition.name for definition in registry.definitions()] == ["echo"]
