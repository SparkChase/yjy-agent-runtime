"""不依赖外网的完整 Runtime 闭环测试。"""

import json

import httpx

from yjy_agent_runtime.agent import AgentLoop
from yjy_agent_runtime.domain import (
    RunRequest,
    TerminationReason,
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
    register_builtin_tools(registry)
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
