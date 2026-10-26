"""真实 LLM 集成测试；默认不运行，避免普通测试依赖外部服务。

这些测试必须打真实付费模型，不能用 MockTransport 或预设决策替代。缺少配置时
明确 skip，并说明原因，不伪造"已联调"。

这里分两层：

1. ``test_configured_llm_can_return_a_decision``：最小连通性检查，只证明配置有效。
2. 端到端场景：真实 LLM + 真实 ``ToolRegistry`` + 真实 ``AgentLoop`` +
   临时 Session 目录，验证"模型自己决定调工具 -> 工具执行 -> 结果写回 Session
   -> 下一轮 Context -> 最终回答"的完整闭环，以及同 Session 追问、跨 Session
   隔离和压缩后追问。

只断言事实（计算结果、工具名、状态、终止原因、Session 内容），不对模型措辞做
精确匹配。任一场景失败时保留原始错误与 Trace，不用 Mock 或重试伪造成功。
"""

import os
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest

from yjy_agent_runtime.agent import AgentLoop, LoopConfig
from yjy_agent_runtime.domain import (
    AgentDecision,
    Message,
    MessageRole,
    RunRequest,
    Session,
    TerminationReason,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.context import ContextBuilder, ContextConfig
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)
from yjy_agent_runtime.infrastructure.session import (
    InMemorySessionStore,
    JsonFileSessionStore,
)
from yjy_agent_runtime.infrastructure.tools import (
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)
from yjy_agent_runtime.infrastructure.trace import (
    InMemoryTraceRecorder,
    JsonlTraceRecorder,
)

EARLY_FACT = "ORANGE-42"
CALCULATION = "137 * 29"
EXPECTED_RESULT = "3973"


class NoToolExecutor:
    """压缩验收不涉及工具调用；被调用即说明场景偏离预期。"""

    def execute(self, session_id: str, call: ToolCall) -> ToolResult:
        raise AssertionError(f"unexpected tool call: {session_id} {call.name}")


class RecordingClient:
    """记录真实客户端收到的 Context，用于断言压缩后仍保留早期事实。"""

    def __init__(self, client: OpenAICompatibleLLMClient) -> None:
        self._client = client
        self.contexts: list[tuple[Message, ...]] = []

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        self.contexts.append(tuple(messages))
        return self._client.decide(messages)


def require_real_client() -> OpenAICompatibleLLMClient:
    """读取真实配置；缺失时明确 skip，绝不回退到 Mock。"""

    required = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
    if not all(os.getenv(name) for name in required):
        pytest.skip("real LLM configuration is not available")
    config = OpenAICompatibleConfig.from_environment()
    return OpenAICompatibleLLMClient(
        OpenAICompatibleConfig(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            timeout_seconds=max(config.timeout_seconds, 120.0),
        )
    )


def build_real_runtime(
    session_store: Any,
    trace_recorder: Any,
    *,
    context_config: ContextConfig | None = None,
    llm_client: Any | None = None,
) -> tuple[AgentLoop, Any]:
    """装配真实注册表、真实执行器和真实 AgentLoop。

    ``llm_client`` 允许传入包装过的真实客户端（例如记录 Context 的 ``RecordingClient``），
    默认则按环境变量新建一个真实客户端，由调用方负责关闭。
    """

    client = llm_client if llm_client is not None else require_real_client()
    registry = ToolRegistry()
    register_builtin_tools(registry, session_store)
    loop = AgentLoop(
        context_provider=ContextBuilder(
            session_store,
            tool_definitions=registry.definitions(),
            config=context_config,
        ),
        llm_client=client,
        session_store=session_store,
        tool_executor=RegistryToolExecutor(registry),
        trace_recorder=trace_recorder,
        config=LoopConfig(max_iterations=6),
    )
    return loop, client


def tool_evidence(session: Session) -> dict[str, object]:
    """打印可复现的工具证据，不包含任何密钥。"""

    return {
        "model": os.getenv("LLM_MODEL", ""),
        "tool_calls": [call.name for call in session.tool_calls],
        "tool_status": [result.status.value for result in session.tool_results],
        "todos": list(session.todos),
    }


@pytest.mark.integration
def test_configured_llm_can_return_a_decision() -> None:
    client = require_real_client()
    try:
        decision = client.decide((Message(MessageRole.USER, "只回复：ok"),))
    finally:
        client.close()

    assert decision is not None


@pytest.mark.integration
def test_real_llm_drives_calculator_and_todo_through_the_full_loop(
    tmp_path: Path,
) -> None:
    """场景一：真实模型自己决定调 ``calculator`` 和 ``todo``，Loop 跑完整闭环。"""

    store = JsonFileSessionStore(tmp_path / "sessions")
    trace_path = tmp_path / "trace.jsonl"
    started = perf_counter()
    with JsonlTraceRecorder(trace_path) as trace:
        loop, client = build_real_runtime(store, trace)
        try:
            result = loop.run(
                RunRequest(
                    "real-tool-loop",
                    f"请使用 calculator 工具计算 {CALCULATION}，"
                    "再使用 todo 工具把计算结果记入待办，最后用一句话回答结果。",
                )
            )
        finally:
            client.close()
    elapsed = round(perf_counter() - started, 1)

    session = store.get_or_create("real-tool-loop")
    print(
        "real tool loop evidence:",
        {
            **tool_evidence(session),
            "iterations": result.iterations,
            "termination_reason": result.termination_reason.value,
            "answer": result.answer,
            "elapsed_seconds": elapsed,
            "trace_file": str(trace_path),
        },
    )

    used_tools = [call.name for call in session.tool_calls]
    assert result.termination_reason is TerminationReason.FINAL_ANSWER
    assert result.answer is not None
    assert EXPECTED_RESULT in result.answer
    # 至少发生一次真实工具调用，且确实是模型按 Schema 自主发起的。
    assert "calculator" in used_tools
    assert "todo" in used_tools
    assert all(
        result.status is ToolResultStatus.SUCCESS for result in session.tool_results
    )
    # 工具结果真的写进了目标 Session：计算结果进了待办。
    assert any(EXPECTED_RESULT in todo for todo in session.todos)
    # Trace 落盘，可用于事后审计。
    assert trace_path.exists()
    assert trace_path.read_text(encoding="utf-8").strip()


@pytest.mark.integration
def test_real_llm_follow_up_and_cross_session_isolation(tmp_path: Path) -> None:
    """场景二、三：同 Session 追问能读到待办；另一个 Session 看不到。"""

    store = JsonFileSessionStore(tmp_path / "sessions")
    trace_path = tmp_path / "trace.jsonl"
    with JsonlTraceRecorder(trace_path) as trace:
        loop, client = build_real_runtime(store, trace)
        try:
            first = loop.run(
                RunRequest(
                    "real-session-a",
                    f"请使用 calculator 计算 {CALCULATION}，并使用 todo 把结果记入待办。",
                )
            )
            follow_up = loop.run(
                RunRequest("real-session-a", "列出我的待办。只回答待办内容。")
            )
            other = loop.run(
                RunRequest("real-session-b", "列出我的待办。")
            )
        finally:
            client.close()

    session_a = store.get_or_create("real-session-a")
    session_b = store.get_or_create("real-session-b")
    print(
        "real follow-up evidence:",
        {
            **tool_evidence(session_a),
            "session_b_tool_calls": [call.name for call in session_b.tool_calls],
            "first_answer": first.answer,
            "follow_up_answer": follow_up.answer,
            "other_session_answer": other.answer,
            "trace_file": str(trace_path),
        },
    )

    assert first.termination_reason is TerminationReason.FINAL_ANSWER
    assert EXPECTED_RESULT in (first.answer or "")
    assert follow_up.termination_reason is TerminationReason.FINAL_ANSWER
    # 追问必须基于 Session 里真实保存的待办，而不是模型猜出来的。
    assert "todo" in [call.name for call in session_a.tool_calls]
    assert EXPECTED_RESULT in (follow_up.answer or "")
    assert any(EXPECTED_RESULT in todo for todo in session_a.todos)

    # 隔离：另一个 Session 既没有待办数据，回答里也不能出现前一个 Session 的内容。
    assert session_b.todos == ()
    assert session_b.tool_calls == () or all(
        call.name == "todo" for call in session_b.tool_calls
    )
    assert EXPECTED_RESULT not in (other.answer or "")


@pytest.mark.integration
def test_real_llm_recalls_early_fact_after_bounded_compression() -> None:
    """场景四：压缩后追问，早期事实必须仍进入 Context，回答必须含代号。

    离线测试只能证明字符预算；这里用真实模型验证"压缩后仍可追问"。预置 60 条、
    每条约 800 字符的历史，早期写入验收代号，配置刻意较小的预算以确保发生压缩。
    """

    store = InMemorySessionStore()
    session_id = "real-compression"
    store.get_or_create(session_id)
    store.append_message(
        session_id,
        Message(MessageRole.USER, f"请记住：验收代号是 {EARLY_FACT}。"),
    )
    for index in range(60):
        store.append_message(
            session_id,
            Message(
                MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                f"第 {index:02d} 条历史：" + ("背景资料" * 200),
            ),
        )
    messages_before = len(store.get_or_create(session_id).messages)

    client = require_real_client()
    recording = RecordingClient(client)
    trace = InMemoryTraceRecorder()
    loop, _client = build_real_runtime(
        store,
        trace,
        context_config=ContextConfig(
            max_history_messages=20,
            max_summary_chars=1_500,
            max_context_chars=12_000,
        ),
        llm_client=recording,
    )
    try:
        result = loop.run(
            RunRequest(session_id, "我最开始告诉你的验收代号是什么？只回答代号。")
        )
    finally:
        client.close()

    session = store.get_or_create(session_id)
    summary = session.summary
    iteration = next(
        event for event in trace.events if event.event_type.value == "iteration_started"
    )
    prompt_chars = iteration.attributes["prompt_chars"]
    context_text = "\n".join(message.content for message in recording.contexts[0])
    print(
        "real compression evidence:",
        {
            "model": os.getenv("LLM_MODEL", ""),
            "messages_before": messages_before,
            "messages_after": len(session.messages),
            "summary_covered_message_count": (
                summary.covered_message_count if summary is not None else 0
            ),
            "summary_chars": len(summary.content) if summary is not None else 0,
            "prompt_chars": prompt_chars,
            "context_messages": len(recording.contexts[0]),
            "max_context_chars": 12_000,
            "answer": result.answer,
        },
    )

    assert summary is not None
    assert len(summary.content) <= 1_500
    assert 0 < summary.covered_message_count < len(session.messages)
    assert prompt_chars <= 12_000
    # 早期事实必须经摘要保留并真实进入模型输入，而不是靠模型猜测。
    assert EARLY_FACT in context_text
    assert result.answer is not None
    assert EARLY_FACT in result.answer
