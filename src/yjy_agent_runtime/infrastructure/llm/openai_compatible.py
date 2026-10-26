"""OpenAI Chat Completions 兼容接口的最小 LLM 适配器。

这里刻意**不使用**供应商的原生 function calling / tool_calls 字段，而是要求模型
直接返回一段 JSON：

    {"type": "final_answer", "answer": "..."}
    {"type": "tool_calls", "tool_calls": [{"name": "...", "arguments": {...}}]}

原因：题目要求“自行实现 LLM 输出解析”。把决策解析放在本地，模型只负责产出
JSON，Runtime 负责校验、执行和循环，整个控制流才完全掌握在项目自己手里。

适配器只做三件事：发 HTTP、取 choices[0].message.content、把 JSON 解析成
领域对象。网络错误、HTTP 错误、解析错误分别抛不同异常，交由 AgentLoop 转成
结构化的 termination_reason。
"""

import json
import os
import re
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
    """连接 OpenAI Chat Completions 兼容服务所需配置。

    `timeout_seconds` 是"等待模型读完并返回"的读取预算，而不是所有阶段共用的
    总超时。真实网关在多轮工具调用中经常超过 30 秒，因此默认值取 120 秒；
    连接阶段另有独立的较短预算，避免地址不可达时长时间挂起。
    """

    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise LLMConfigurationError("LLM_BASE_URL must not be empty")
        if not self.model.strip():
            raise LLMConfigurationError("LLM_MODEL must not be empty")
        if not self.api_key.strip():
            raise LLMConfigurationError("LLM_API_KEY must not be empty")
        if self.timeout_seconds <= 0:
            raise LLMConfigurationError("LLM_TIMEOUT_SECONDS must be positive")
        if self.connect_timeout_seconds <= 0:
            raise LLMConfigurationError(
                "LLM_CONNECT_TIMEOUT_SECONDS must be positive"
            )

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleConfig":
        """从环境变量读取配置，密钥不进入错误或 Trace 文本。"""

        return cls(
            base_url=os.getenv("LLM_BASE_URL", ""),
            model=os.getenv("LLM_MODEL", ""),
            api_key=os.getenv("LLM_API_KEY", ""),
            timeout_seconds=_float_from_env("LLM_TIMEOUT_SECONDS", 120.0),
            connect_timeout_seconds=_float_from_env(
                "LLM_CONNECT_TIMEOUT_SECONDS", 10.0
            ),
        )

    def http_timeout(self) -> httpx.Timeout:
        """构造分阶段超时：连接要快失败，读取要给模型足够时间。"""

        return httpx.Timeout(
            self.timeout_seconds,
            connect=self.connect_timeout_seconds,
        )


def _float_from_env(name: str, default: float) -> float:
    """读取浮点环境变量，非法值保留明确错误语义。"""

    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise LLMConfigurationError(f"{name} must be numeric") from error


class OpenAICompatibleLLMClient:
    """调用兼容接口并把 JSON 决策解析成内部领域对象。"""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        # 只有自建客户端才应用分阶段超时；注入的客户端（例如 MockTransport）
        # 保留调用方自己的超时策略。
        self._client = client or httpx.Client(timeout=config.http_timeout())
        self._owns_client = client is None

    def close(self) -> None:
        """关闭适配器自行创建的 HTTP 客户端。"""

        if self._owns_client:
            self._client.close()

    def decide(self, messages: Sequence[Message]) -> AgentDecision:
        """请求 JSON 决策，并区分网络、服务和响应解析错误。"""

        # response_format 只是“请求模型输出 JSON”的提示；部分兼容网关会忽略它，
        # 所以下面的解析仍然按“content 里必须能 json.loads”来校验，不假设一定生效。
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
            detail = response.text[:500].strip()
            if detail:
                detail = detail.replace(self._config.api_key, "[REDACTED]")
                detail = re.sub(
                    r"(?i)(bearer\s+)[^\s\"']+",
                    r"\1[REDACTED]",
                    detail,
                )
            raise LLMServiceError(
                f"LLM service returned HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
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
        """把内部消息编码成 Chat Completions 的 role/content 结构。

        关键取舍：内部 TOOL 消息在这里被改写成 USER 消息。因为本适配器没有使用
        供应商原生的 tool 角色（那需要 assistant.tool_calls 严格配对），而是把
        “工具结果”当成一段普通上下文交给模型。调用 ID 保留在文本里，模型仍能
        把结果和某次调用对上。
        """

        encoded = []
        for message in messages:
            if message.role is MessageRole.TOOL:
                content = f"工具执行结果（调用 {message.tool_call_id}）：{message.content}"
                encoded.append({"role": MessageRole.USER.value, "content": content})
            else:
                encoded.append(
                    {"role": message.role.value, "content": message.content}
                )
        protocol = (
            "Runtime 决策协议：只返回 JSON 对象。最终回答格式："
            + '{"type":"final_answer","answer":"...","decision_summary":"公开摘要"}。工具调用格式：'
            + '{"type":"tool_calls","tool_calls":[{"name":"...","arguments":{...}}],"decision_summary":"公开摘要"}。'
            + "decision_summary 只能是简短公开摘要，不得输出私有思维链；不得输出 JSON 以外的文本。"
        )
        if encoded and encoded[0]["role"] == MessageRole.SYSTEM.value:
            encoded[0]["content"] += f"\n{protocol}"
        else:
            encoded.insert(
                0,
                {"role": MessageRole.SYSTEM.value, "content": protocol},
            )
        return encoded

    @staticmethod
    def _decode_decision(data: Any) -> AgentDecision:
        if not isinstance(data, Mapping):
            raise LLMResponseError("decision must be a JSON object")
        decision_type = data.get("type")
        if decision_type is None and isinstance(data.get("reply"), str):
            # 兼容部分 OpenAI-compatible 网关的简单 JSON 回复包装。
            return FinalAnswerDecision(data["reply"])
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
