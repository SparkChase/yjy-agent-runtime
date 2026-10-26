"""真实 LLM 集成测试；默认不运行，避免普通测试依赖外部服务。"""

import os

import pytest

from yjy_agent_runtime.domain import Message, MessageRole
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)


@pytest.mark.integration
def test_configured_llm_can_return_a_decision() -> None:
    required = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
    if not all(os.getenv(name) for name in required):
        pytest.skip("real LLM configuration is not available")

    config = OpenAICompatibleConfig.from_environment()
    config = OpenAICompatibleConfig(
        base_url=config.base_url,
        model=config.model,
        api_key=config.api_key,
        timeout_seconds=max(config.timeout_seconds, 120.0),
    )
    client = OpenAICompatibleLLMClient(config)
    try:
        decision = client.decide((Message(MessageRole.USER, "只回复：ok"),))
    finally:
        client.close()

    assert decision is not None
