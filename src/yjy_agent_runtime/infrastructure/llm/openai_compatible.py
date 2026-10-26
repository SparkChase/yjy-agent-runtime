"""OpenAI Chat Completions 兼容接口的最小 LLM 适配器。"""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from yjy_agent_runtime.domain import (
    AgentDecision,
    FinalAnswerDecision,
    Message,
    MessageRole,
    ToolCall,
    ToolCallDecision,
)


class LLMConfigurationError(ValueError):
    """真实 LLM 配置缺失或非法。"""


class LLMNetworkError(RuntimeError):
    """LLM 请求未能完成网络传输。"""


class LLMServiceError(RuntimeError):
    """LLM 服务返回非成功状态码。"""


class LLMResponseError(ValueError):
    """LLM 响应格式不符合 Runtime 的决策协议。"""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """连接 OpenAI Chat Completions 兼容服务所需配置。"""

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise LLMConfigurationError("LLM_BASE_URL must not be empty")
        if not self.model.strip():
            raise LLMConfigurationError("LLM_MODEL must not be empty")
        if not self.api_key.strip():
            raise LLMConfigurationError("LLM_API_KEY must not be empty")
        if self.timeout_seconds <= 0:
            raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be positive")

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleConfig":
        """从环境变量读取配置，密钥不进入错误或 Trace 文本。"""

        try:
            timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
        except ValueError as error:
            raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be numeric") from error
        return cls(
            base_url=os.getenv("LLM_BASE_URL", ""),
            model=os.getenv("LLM_MODEL", ""),
            api_key=os.getenv("LLM_API_KEY", ""),
            timeout_seconds=timeout,
        )


class OpenAICompatibleLLMClient:
    """调用兼容接口并把 JSON 决策解析成内部领域对象。"""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        """关闭适配器自行创建的 HTTP 客户端。"""

        if self._owns_client:
            self._client.close()

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        """请求 JSON 决策，并区分网络、服务和响应解析错误。"""

        payload = {
            "model": self._config.model,
            "messages": self._encode_messages(messages),
            "response_format": {"type": "json_object"},
        }
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        try:
            response = self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise LLMNetworkError("LLM request timed out") from error
        except httpx.HTTPError as error:
            raise LLMNetworkError(f"LLM network error: {type(error).__name__}") from error

        if response.is_error:
            raise LLMServiceError(
                f"LLM service returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            decision_data = json.loads(content)
            return self._decode_decision(decision_data)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as error:
            if isinstance(error, LLMResponseError):
                raise
            raise LLMResponseError("LLM response did not contain a valid decision") from error

    @staticmethod
    def _encode_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
        encoded = [
            {"role": message.role.value, "content": message.content}
            for message in messages
        ]
        if encoded and encoded[0]["role"] == MessageRole.SYSTEM.value:
            encoded[0]["content"] += (
                "\n请只返回 JSON 对象。最终回答格式："
                + '{"type":"final_answer","answer":"...","decision_summary":"公开摘要"}。工具调用格式：'
                + '{"type":"tool_calls","tool_calls":[{"name":"...","arguments":{...}}],"decision_summary":"公开摘要"}。'
                + "decision_summary 只能是简短公开摘要，不得输出私有思维链；不得输出 JSON 以外的文本。"
            )
        return encoded

    @staticmethod
    def _decode_decision(data: Any) -> AgentDecision:
        if not isinstance(data, Mapping):
            raise LLMResponseError("decision must be a JSON object")
        decision_type = data.get("type")
        if decision_type == "final_answer":
            answer = data.get("answer")
            if not isinstance(answer, str) or not answer:
                raise LLMResponseError("final_answer requires a non-empty answer")
            summary = data.get("decision_summary", "")
            if not isinstance(summary, str):
                raise LLMResponseError("decision_summary must be a string")
            return FinalAnswerDecision(answer, decision_summary=summary)
        if decision_type == "tool_calls":
            calls = data.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                raise LLMResponseError("tool_calls requires a non-empty array")
            decoded: list[ToolCall] = []
            for index, call_data in enumerate(calls, start=1):
                if not isinstance(call_data, Mapping):
                    raise LLMResponseError("each tool call must be an object")
                name = call_data.get("name")
                arguments = call_data.get("arguments", {})
                if not isinstance(name, str) or not name:
                    raise LLMResponseError("tool call requires a non-empty name")
                if not isinstance(arguments, Mapping):
                    raise LLMResponseError("tool call arguments must be an object")
                decoded.append(
                    ToolCall(
                        call_id=f"call-{index}-{os.urandom(4).hex()}",
                        name=name,
                        arguments=dict(arguments),
                    )
                )
            summary = data.get("decision_summary", "")
            if not isinstance(summary, str):
                raise LLMResponseError("decision_summary must be a string")
            return ToolCallDecision(tuple(decoded), decision_summary=summary)
        raise LLMResponseError("decision type must be final_answer or tool_calls")
