"""从当前 Session 构建有明确预算的模型输入。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass

from yjy_agent_runtime.agent.ports import SessionStore
from yjy_agent_runtime.domain import Message, MessageRole, RunRequest, ToolResult
from yjy_agent_runtime.infrastructure.tools import ToolDefinition


@dataclass(frozen=True, slots=True)
class ContextConfig:
    """Context 的可解释安全边界。"""

    system_prompt: str = "你是一个可靠的 Agent Runtime 助手。"
    max_history_messages: int = 20
    max_tool_result_chars: int = 2_000

    def __post_init__(self) -> None:
        if not self.system_prompt:
            raise ValueError("system prompt must not be empty")
        if self.max_history_messages < 1:
            raise ValueError("max_history_messages must be at least 1")
        if self.max_tool_result_chars < 1:
            raise ValueError("max_tool_result_chars must be at least 1")


class ContextBuilder:
    """只读取请求指定的 Session，并选择最近的历史消息。"""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        tool_definitions: Sequence[ToolDefinition] = (),
        config: ContextConfig | None = None,
    ) -> None:
        self._session_store = session_store
        self._tool_definitions = tuple(tool_definitions)
        self._config = config or ContextConfig()

    def build(
        self,
        request: RunRequest,
        tool_results: tuple[ToolResult, ...],
    ) -> tuple[Message, ...]:
        """构建系统约束加当前 Session 最近历史；本轮结果已由循环持久化。"""

        del tool_results
        session = self._session_store.get(request.session_id)
        if session is None:
            raise LookupError(f"session not found: {request.session_id}")

        history = session.messages[-self._config.max_history_messages :]
        selected = tuple(self._limit_tool_message(message) for message in history)
        return (Message(MessageRole.SYSTEM, self._system_content()), *selected)

    def _system_content(self) -> str:
        if not self._tool_definitions:
            return self._config.system_prompt
        definitions = [
            {
                "name": definition.name,
                "description": definition.description,
                "parameters": dict(definition.parameters),
                "has_side_effects": definition.has_side_effects,
            }
            for definition in self._tool_definitions
        ]
        encoded = json.dumps(definitions, ensure_ascii=False, sort_keys=True)
        return f"{self._config.system_prompt}\n可用工具定义：{encoded}"

    def _limit_tool_message(self, message: Message) -> Message:
        if (
            message.role is not MessageRole.TOOL
            or len(message.content) <= self._config.max_tool_result_chars
        ):
            return message
        marker = "...[truncated]"
        limit = self._config.max_tool_result_chars
        content = (
            marker[:limit]
            if limit <= len(marker)
            else message.content[: limit - len(marker)] + marker
        )
        return Message(MessageRole.TOOL, content, message.tool_call_id)
