"""最小内置工具测试。"""

from yjy_agent_runtime.domain import ToolCall, ToolResultStatus
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
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


def test_register_builtin_tools_exposes_required_tool_schemas() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)

    assert [definition.name for definition in registry.definitions()] == [
        "echo",
        "calculator",
        "search",
        "todo",
    ]


def test_calculator_rejects_code_execution_and_calculates_safely() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    executor = RegistryToolExecutor(registry)

    success = executor.execute(
        "session-a",
        ToolCall("call-1", "calculator", {"expression": "(6 + 2) * 7"}),
    )
    failure = executor.execute(
        "session-a",
        ToolCall("call-2", "calculator", {"expression": "__import__('os')"}),
    )

    assert success.status is ToolResultStatus.SUCCESS
    assert success.content == "56"
    assert failure.status is ToolResultStatus.ERROR
    assert failure.error_type == "ValueError"


def test_mock_search_is_deterministic_and_has_no_network_dependency() -> None:
    registry = ToolRegistry()
    register_builtin_tools(registry)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "search", {"query": "session"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert "Session" in result.content


def test_todo_is_scoped_to_session_id() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.get_or_create("session-b")
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    added = executor.execute(
        "session-a",
        ToolCall("call-1", "todo", {"action": "add", "text": "写周报"}),
    )
    listed_a = executor.execute(
        "session-a",
        ToolCall("call-2", "todo", {"action": "list"}),
    )
    listed_b = executor.execute(
        "session-b",
        ToolCall("call-3", "todo", {"action": "list"}),
    )

    assert added.status is ToolResultStatus.SUCCESS
    assert "写周报" in listed_a.content
    assert listed_b.content == "当前没有待办"
