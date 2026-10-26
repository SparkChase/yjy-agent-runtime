"""真实 LLM 适配器。"""

from .openai_compatible import (
    LLMConfigurationError,
    LLMNetworkError,
    LLMResponseError,
    LLMServiceError,
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)

__all__ = [
    "LLMConfigurationError",
    "LLMNetworkError",
    "LLMResponseError",
    "LLMServiceError",
    "OpenAICompatibleConfig",
    "OpenAICompatibleLLMClient",
]
