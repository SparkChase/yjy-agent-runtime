"""ContextBuilder 的 Session 隔离和预算测试。"""

from yjy_agent_runtime.domain import Message, MessageRole, RunRequest
from yjy_agent_runtime.infrastructure.context import ContextBuilder, ContextConfig
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.tools import ToolDefinition


def test_builder_reads_only_requested_session() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.get_or_create("session-b")
    store.append_message("session-a", Message(MessageRole.USER, "A 的历史"))
    store.append_message("session-b", Message(MessageRole.USER, "B 的历史"))

    messages = ContextBuilder(store).build(RunRequest("session-a", "当前问题"), ())

    assert [message.content for message in messages] == [
        "你是一个可靠的 Agent Runtime 助手。",
        "A 的历史",
    ]


def test_builder_keeps_only_recent_history_with_explicit_budget() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    for index in range(4):
        store.append_message(
            "session-a",
            Message(MessageRole.USER, f"消息 {index}"),
        )
    builder = ContextBuilder(store, config=ContextConfig(max_history_messages=2))

    messages = builder.build(RunRequest("session-a", "继续"), ())

    assert [message.content for message in messages[1:]] == ["消息 2", "消息 3"]


def test_builder_injects_tool_definitions_into_system_constraint() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    definition = ToolDefinition(
        name="echo",
        description="原样返回文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        has_side_effects=False,
    )
    builder = ContextBuilder(store, tool_definitions=(definition,))

    messages = builder.build(RunRequest("session-a", "回显内容"), ())

    assert messages[0].role is MessageRole.SYSTEM
    assert '"name": "echo"' in messages[0].content
    assert '"has_side_effects": false' in messages[0].content


def test_builder_limits_stored_tool_result_for_model_context() -> None:
    store = InMemorySessionStore()
    store.get_or_create("session-a")
    store.append_message(
        "session-a",
        Message(MessageRole.TOOL, "x" * 100, "call-1"),
    )
    builder = ContextBuilder(
        store,
        config=ContextConfig(max_tool_result_chars=20),
    )

    messages = builder.build(RunRequest("session-a", "继续"), ())

    assert len(messages[-1].content) == 20
    assert messages[-1].content.endswith("...[truncated]")
    assert messages[-1].tool_call_id == "call-1"


def test_loop_persists_history_across_runs() -> None:
    from collections import deque
    from collections.abc import Sequence

    from yjy_agent_runtime.agent import AgentLoop
    from yjy_agent_runtime.domain import (
        AgentDecision,
        FinalAnswerDecision,
        ToolCall,
        ToolResult,
    )

    class FakeLLM:
        def __init__(self) -> None:
            self.decisions = deque(
                [FinalAnswerDecision("第一答"), FinalAnswerDecision("第二答")]
            )
            self.contexts: list[tuple[Message, ...]] = []

        def decide(self, messages: Sequence[Message]) -> AgentDecision:
            self.contexts.append(tuple(messages))
            return self.decisions.popleft()

    class UnusedTools:
        def execute(self, session_id: str, call: ToolCall) -> ToolResult:
            raise AssertionError("tool should not be called")

    class Trace:
        def record(self, event: str, **attributes: object) -> None:
            del event, attributes

    store = InMemorySessionStore()
    llm = FakeLLM()
    loop = AgentLoop(
        context_provider=ContextBuilder(store),
        llm_client=llm,
        session_store=store,
        tool_executor=UnusedTools(),
        trace_recorder=Trace(),
    )

    loop.run(RunRequest("session-a", "第一问"))
    loop.run(RunRequest("session-a", "第二问"))

    assert [message.content for message in llm.contexts[1][1:]] == [
        "第一问",
        "第一答",
        "第二问",
    ]
