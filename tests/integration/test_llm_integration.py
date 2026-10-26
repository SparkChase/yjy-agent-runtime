"""真实 LLM 集成测试；默认不运行，避免普通测试依赖外部服务。

这些测试必须打真实付费模型，不能用 MockTransport 或预设决策替代。缺少配置时
明确 skip，并说明原因，不伪造"已联调"。
"""

import os
from collections.abc import Sequence

import pytest

from yjy_agent_runtime.agent import AgentLoop
from yjy_agent_runtime.domain import (
    AgentDecision,
    Message,
    MessageRole,
    RunRequest,
    ToolCall,
    ToolResult,
)
from yjy_agent_runtime.infrastructure.context import ContextBuilder, ContextConfig
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.trace import InMemoryTraceRecorder

EARLY_FACT = "ORANGE-42"


class UnusedTools:
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


@pytest.mark.integration
def test_configured_llm_can_return_a_decision() -> None:
    client = require_real_client()
    try:
        decision = client.decide((Message(MessageRole.USER, "只回复：ok"),))
    finally:
        client.close()

    assert decision is not None


@pytest.mark.integration
def test_real_llm_recalls_early_fact_after_bounded_compression() -> None:
    """真实压缩后追问：早期事实必须仍进入 Context，回答必须含代号。

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
    loop = AgentLoop(
        context_provider=ContextBuilder(
            store,
            config=ContextConfig(
                max_history_messages=20,
                max_summary_chars=1_500,
                max_context_chars=12_000,
            ),
        ),
        llm_client=recording,
        session_store=store,
        tool_executor=UnusedTools(),
        trace_recorder=trace,
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
