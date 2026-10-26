"""从当前 Session 构建有明确预算的模型输入。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from yjy_agent_runtime.agent.ports import HistorySummarizer, SessionStore
from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    RunRequest,
    Session,
    SessionSummary,
    ToolResult,
)
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


class ExtractiveHistorySummarizer:
    """以角色标记合并公开历史，作为无需外部模型的基础摘要器。"""

    _ROLE_LABELS: ClassVar[dict[MessageRole, str]] = {
        MessageRole.USER: "用户",
        MessageRole.ASSISTANT: "助手",
        MessageRole.TOOL: "工具结果",
        MessageRole.SYSTEM: "系统",
    }

    def summarize(
        self,
        previous_summary: str | None,
        messages: Sequence[Message],
    ) -> str:
        """保留公开事实，并将新增消息增量合并为分行摘要。"""

        lines = [previous_summary] if previous_summary else []
        lines.extend(
            f"{self._ROLE_LABELS[message.role]}：{message.content}"
            for message in messages
        )
        return "\n".join(lines)


class ContextBuilder:
    """构建当前 Session 的增量摘要和最近消息窗口。"""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        tool_definitions: Sequence[ToolDefinition] = (),
        summarizer: HistorySummarizer | None = None,
        config: ContextConfig | None = None,
    ) -> None:
        self._session_store = session_store
        self._tool_definitions = tuple(tool_definitions)
        self._summarizer = summarizer or ExtractiveHistorySummarizer()
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

        session = self._compress_history(session)
        history_start = max(
            session.summary.covered_message_count if session.summary else 0,
            len(session.messages) - self._config.max_history_messages,
        )
        history = session.messages[history_start:]
        selected = tuple(self._limit_tool_message(message) for message in history)
        summary_messages = (
            (
                Message(
                    MessageRole.SYSTEM,
                    f"较早历史摘要：\n{session.summary.content}",
                ),
            )
            if session.summary is not None
            else ()
        )
        return (
            Message(MessageRole.SYSTEM, self._system_content()),
            *summary_messages,
            *selected,
        )

    def _compress_history(self, session: Session) -> Session:
        """只摘要未覆盖且已移出最近窗口的连续消息前缀。"""

        target_count = max(
            0,
            len(session.messages) - self._config.max_history_messages,
        )
        covered_count = session.summary.covered_message_count if session.summary else 0
        if target_count <= covered_count:
            return session
        uncovered = session.messages[covered_count:target_count]
        content = self._summarizer.summarize(
            session.summary.content if session.summary else None,
            uncovered,
        )
        summary = SessionSummary(content, covered_message_count=target_count)
        return self._session_store.set_summary(session.session_id, summary)

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
