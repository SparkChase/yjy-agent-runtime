"""最小内置工具测试。"""

from time import perf_counter, sleep

import pytest

from yjy_agent_runtime.domain import (
    Session,
    TimeoutSemantics,
    ToolCall,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.tools import (
    EchoTool,
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)


class SlowTodoStore(InMemorySessionStore):
    """把写入拖慢，用来复现“执行器已经报超时、待办却稍后出现”的旧缺陷。"""

    def __init__(self) -> None:
        super().__init__()
        self.write_count = 0

    def append_todo(self, session_id: str, todo: str) -> Session:
        sleep(0.05)
        self.write_count += 1
        return super().append_todo(session_id, todo)


def test_echo_tool_definition_has_no_side_effects() -> None:
    definition = EchoTool().definition

    assert definition.name == "echo"
    assert definition.has_side_effects is False
    assert definition.parameters["additionalProperties"] is False


def test_echo_tool_runs_through_registry_executor() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": "hello"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == "hello"


def test_echo_tool_rejects_empty_text_before_invocation() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "echo", {"text": ""}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "ToolArgumentsValidationError"


def test_register_builtin_tools_exposes_required_tool_schemas() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)

    assert [definition.name for definition in registry.definitions()] == [
        "echo",
        "calculator",
        "search",
        "todo",
    ]


def test_calculator_rejects_code_execution_and_calculates_safely() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
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


def test_calculator_supports_normal_arithmetic() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    for index, (expression, expected) in enumerate(
        (("2**10", "1024"), ("1/4", "0.25"), ("7 % 4", "3"), ("-(3+4)", "-7")),
        start=1,
    ):
        result = executor.execute(
            "session-a",
            ToolCall(f"call-{index}", "calculator", {"expression": expression}),
        )
        assert result.status is ToolResultStatus.SUCCESS, expression
        assert result.content == expected, expression


def test_calculator_rejects_huge_exponent_without_computing_it() -> None:
    """P2-02 回归：``9**9**9`` 必须在算指数时就失败，而不是真的去求幂。"""

    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    started = perf_counter()
    result = executor.execute(
        "session-a",
        ToolCall("call-1", "calculator", {"expression": "9**9**9"}),
    )
    elapsed = perf_counter() - started

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "ValueError"
    assert "exponent" in result.content
    assert elapsed < 1.0


def test_calculator_rejects_overlong_expression() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)
    expression = "1+" * 100 + "1"  # 201 字符，超过 200 的上限

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "calculator", {"expression": expression}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_type == "ValueError"
    assert "at most 200 characters" in result.content


def test_calculator_rejects_deep_nesting_and_oversized_results() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    nested = executor.execute(
        "session-a",
        ToolCall("call-1", "calculator", {"expression": "1" + "+1" * 40}),
    )
    oversized = executor.execute(
        "session-a",
        ToolCall("call-2", "calculator", {"expression": "(10**64)**64"}),
    )

    assert nested.status is ToolResultStatus.ERROR
    assert "nesting" in nested.content
    assert oversized.status is ToolResultStatus.ERROR
    assert "bits" in oversized.content


def test_register_builtin_tools_requires_explicit_session_store() -> None:
    registry = ToolRegistry()

    with pytest.raises(TypeError):
        register_builtin_tools(registry)  # type: ignore[call-arg]


def test_mock_search_is_deterministic_and_has_no_network_dependency() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "search", {"query": "session"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert "Session" in result.content


def test_mock_search_matches_multiple_keywords() -> None:
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "search", {"query": "session 隔离"}),
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert "Session" in result.content
    assert "隔离" in result.content


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


def test_todo_cannot_write_after_the_executor_reported_a_timeout() -> None:
    """P1-03 回归：超时不能变成“先报失败、稍后照样写入”。

    ``todo`` 标记了 ``has_side_effects=True``，执行器必须在调用线程同步执行它：
    结果是真实成功，且恰好写一次。旧实现用 ``join(0.001)`` 返回 TimeoutError，
    工作线程随后仍然写入了待办。
    """

    store = SlowTodoStore()
    store.get_or_create("session-a")
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    executor = RegistryToolExecutor(registry, timeout_seconds=0.001)

    result = executor.execute(
        "session-a",
        ToolCall("call-1", "todo", {"action": "add", "text": "写周报"}),
    )
    # 旧实现会在这段时间里“偷偷”完成写入；新实现不允许出现第二次写。
    sleep(0.1)

    assert result.status is ToolResultStatus.SUCCESS
    assert result.timeout_semantics is TimeoutSemantics.SYNCHRONOUS
    assert store.list_todos("session-a") == ("写周报",)
    assert store.write_count == 1
