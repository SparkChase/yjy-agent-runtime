"""不依赖外网的完整 Runtime 闭环测试。"""

import json
from collections import deque
from collections.abc import Sequence

import httpx

from yjy_agent_runtime.agent import AgentLoop
from yjy_agent_runtime.domain import (
    AgentDecision,
    FinalAnswerDecision,
    Message,
    RunRequest,
    TerminationReason,
    ToolCall,
    ToolCallDecision,
    TraceEventType,
)
from yjy_agent_runtime.infrastructure.context import ContextBuilder
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.tools import (
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)
from yjy_agent_runtime.infrastructure.trace import InMemoryTraceRecorder


class FakeDecisionLLM:
    """使用可预测决策验证真实工具注册器接入 AgentLoop。"""

    def __init__(self, decisions: Sequence[AgentDecision]) -> None:
        self._decisions = deque(decisions)

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        del messages
        return self._decisions.popleft()


def make_local_loop(decisions: Sequence[AgentDecision]):
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    trace = InMemoryTraceRecorder()
    loop = AgentLoop(
        context_provider=ContextBuilder(
            store,
            tool_definitions=registry.definitions(),
        ),
        llm_client=FakeDecisionLLM(decisions),
        session_store=store,
        tool_executor=RegistryToolExecutor(registry),
        trace_recorder=trace,
    )
    return loop, store


def test_openai_compatible_tool_loop_runs_to_final_answer() -> None:
    responses = iter(
        [
            (
                '{"type":"tool_calls","tool_calls":['
                '{"name":"echo","arguments":{"text":"来自工具"}}]}'
            ),
            '{"type":"final_answer","answer":"闭环完成"}',
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["messages"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": next(responses)}}]},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAICompatibleLLMClient(
        OpenAICompatibleConfig("https://llm.example/v1", "test-model", "test-key"),
        client=http_client,
    )
    store = InMemorySessionStore()
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    trace = InMemoryTraceRecorder()
    loop = AgentLoop(
        context_provider=ContextBuilder(
            store,
            tool_definitions=registry.definitions(),
        ),
        llm_client=llm,
        session_store=store,
        tool_executor=RegistryToolExecutor(registry),
        trace_recorder=trace,
    )

    result = loop.run(RunRequest("e2e-session", "请回显并总结"))

    assert result.answer == "闭环完成"
    assert result.termination_reason is TerminationReason.FINAL_ANSWER
    assert result.iterations == 2
    assert trace.events[0].event_type is TraceEventType.RUN_STARTED
    assert trace.events[-1].event_type is TraceEventType.RUN_TERMINATED
    assert all(event.session_id == "e2e-session" for event in trace.events)
    assert all(event.run_id == result.run_id for event in trace.events)
    assert store.get("e2e-session").messages[-1].content == "闭环完成"


def test_calculator_tool_drives_agent_loop() -> None:
    loop, _store = make_local_loop(
        [
            ToolCallDecision(
                (ToolCall("call-1", "calculator", {"expression": "6 * 7"}),),
                decision_summary="需要计算",
            ),
            FinalAnswerDecision("结果是 42。"),
        ]
    )

    result = loop.run(RunRequest("calculator-session", "计算 6 * 7"))

    assert result.answer == "结果是 42。"
    assert result.iterations == 2


def test_search_tool_drives_agent_loop() -> None:
    loop, _store = make_local_loop(
        [
            ToolCallDecision((ToolCall("call-1", "search", {"query": "session"}),)),
            FinalAnswerDecision("找到了 Session 说明。"),
        ]
    )

    result = loop.run(RunRequest("search-session", "搜索 session"))

    assert result.answer == "找到了 Session 说明。"
    assert result.iterations == 2


def test_todo_tool_drives_agent_loop_and_keeps_session_state() -> None:
    loop, store = make_local_loop(
        [
            ToolCallDecision(
                (ToolCall("call-1", "todo", {"action": "add", "text": "写周报"}),)
            ),
            ToolCallDecision((ToolCall("call-2", "todo", {"action": "list"}),)),
            FinalAnswerDecision("待办中有写周报。"),
        ]
    )

    result = loop.run(RunRequest("todo-session", "记一个待办并确认"))

    assert result.answer == "待办中有写周报。"
    assert store.get("todo-session").todos == ("写周报",)
