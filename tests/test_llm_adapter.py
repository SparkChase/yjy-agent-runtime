"""OpenAI-compatible LLM 适配器测试。"""

import json

import httpx
import pytest

from yjy_agent_runtime.domain import (
    FinalAnswerDecision,
    Message,
    MessageRole,
    ToolCallDecision,
)
from yjy_agent_runtime.infrastructure.llm import (
    LLMConfigurationError,
    LLMNetworkError,
    LLMResponseError,
    LLMServiceError,
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)


def make_config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        base_url="https://llm.example/v1",
        model="test-model",
        api_key="test-key",
    )


def make_client(handler) -> OpenAICompatibleLLMClient:
    return OpenAICompatibleLLMClient(
        make_config(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def response_for(content: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"choices": [{"message": {"content": content}}]},
    )


def test_client_decodes_final_answer_and_sends_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["response_format"] == {"type": "json_object"}
        return response_for('{"type":"final_answer","answer":"完成"}')

    client = make_client(handler)

    decision = client.decide((Message(MessageRole.USER, "你好"),))

    assert decision == FinalAnswerDecision("完成")


def test_client_decodes_tool_calls() -> None:
    client = make_client(
        lambda request: response_for(
            '{"type":"tool_calls","tool_calls":['
            '{"name":"echo","arguments":{"text":"hello"}}]}'
        )
    )

    decision = client.decide((Message(MessageRole.USER, "回显"),))

    assert isinstance(decision, ToolCallDecision)
    assert decision.tool_calls[0].name == "echo"
    assert decision.tool_calls[0].arguments == {"text": "hello"}


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(503), LLMServiceError),
        (response_for("not-json"), LLMResponseError),
        (response_for('{"type":"unknown"}'), LLMResponseError),
    ],
)
def test_client_preserves_service_and_parse_error_categories(
    response: httpx.Response,
    error_type: type[Exception],
) -> None:
    client = make_client(lambda request: response)

    with pytest.raises(error_type):
        client.decide((Message(MessageRole.USER, "触发错误"),))


def test_client_maps_network_error_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)

    with pytest.raises(LLMNetworkError, match="network error") as error:
        client.decide((Message(MessageRole.USER, "触发网络错误"),))

    assert "test-key" not in str(error.value)


def test_config_reads_environment_and_rejects_missing_secret(monkeypatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12")

    config = OpenAICompatibleConfig.from_environment()

    assert config.base_url == "https://env.example/v1"
    assert config.timeout_seconds == 12

    monkeypatch.delenv("LLM_API_KEY")
    with pytest.raises(LLMConfigurationError, match="LLM_API_KEY"):
        OpenAICompatibleConfig.from_environment()
