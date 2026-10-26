"""从当前 Session 构建有明确预算的模型输入。

职责边界：

1. 只按请求的 ``session_id`` 读取当前 Session，绝不跨 Session 共享历史或摘要。
2. 把已经移出最近窗口的连续历史增量摘要；摘要本身、单条工具结果和最终模型输入
   都有明确的字符上限。
3. 超预算时按完整消息组继续摘要旧消息，并同步推进
   ``summary.covered_message_count``，保证单调、不重复、不遗漏。

摘要先在内存中累积，只有整个 Context 成功组装后才写回 Session。这样摘要器抛错
时不会留下“覆盖范围已推进但摘要不完整”的状态，下一轮可以安全重试。
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from yjy_agent_runtime.agent.ports import (
    HistorySummarizer,
    SessionStore,
    ToolDefinition,
)
from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    RunRequest,
    Session,
    SessionSummary,
    ToolCall,
    ToolResult,
)

# 摘要被预算截断时的明确标记；同时保留英文关键词，便于日志检索。
SUMMARY_TRUNCATION_MARKER = "\n...[summary truncated / 摘要已截断]...\n"
TRUNCATION_MARKER = "...[truncated]"

# 工具结果信封中敏感参数名的脱敏占位符。
REDACTED = "[REDACTED]"
_SENSITIVE_ARGUMENT_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "cookie", "password", "secret", "token"}
)


class ContextBudgetError(RuntimeError):
    """系统提示与最近消息组本身已超过 Context 预算，无法再通过摘要缩小。"""


def _require_positive_int(name: str, value: int) -> None:
    """拒绝 0、负数、bool 与非整数，避免非法预算悄悄进入运行时。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def truncate_text(content: str, limit: int, marker: str = TRUNCATION_MARKER) -> str:
    """把文本截到 ``limit`` 个字符以内，并保证截断标记可见。"""

    if limit < 1:
        raise ValueError("text limit must be at least 1")
    if len(content) <= limit:
        return content
    if limit <= len(marker):
        return marker[:limit]
    return content[: limit - len(marker)] + marker


def truncate_summary(content: str, limit: int) -> str:
    """按行保留摘要首尾，并在中间写入明确的截断标记。

    首尾各占一半预算，因此最早写入的事实和最近摘要都能保留；当剩余预算不足以
    容纳下一整行时才在该行内截断。返回值长度严格不超过 ``limit``。
    """

    if limit < 1:
        raise ValueError("summary limit must be at least 1")
    if len(content) <= limit:
        return content
    marker = SUMMARY_TRUNCATION_MARKER
    if limit <= len(marker):
        return marker[:limit]

    remaining = limit - len(marker)
    head_budget = (remaining + 1) // 2
    tail_budget = remaining - head_budget
    head = _take_head_lines(content, head_budget).rstrip("\n")
    tail = _take_tail_lines(content, tail_budget).lstrip("\n")
    return f"{head}{marker}{tail}"[:limit]


def _take_head_lines(content: str, budget: int) -> str:
    """从开头按行取内容；单行超预算时只取该行前缀。"""

    if budget <= 0:
        return ""
    chunks: list[str] = []
    used = 0
    for line in content.splitlines(keepends=True):
        if used + len(line) <= budget:
            chunks.append(line)
            used += len(line)
            continue
        remaining = budget - used
        if remaining > 0:
            chunks.append(line[:remaining])
        break
    return "".join(chunks)


def _take_tail_lines(content: str, budget: int) -> str:
    """从结尾按行取内容；单行超预算时只取该行后缀。"""

    if budget <= 0:
        return ""
    chunks: list[str] = []
    used = 0
    for line in reversed(content.splitlines(keepends=True)):
        if used + len(line) <= budget:
            chunks.append(line)
            used += len(line)
            continue
        remaining = budget - used
        if remaining > 0:
            chunks.append(line[-remaining:])
        break
    return "".join(reversed(chunks))


@dataclass(frozen=True, slots=True)
class ContextConfig:
    """Context 的可解释安全边界。"""

    system_prompt: str = "你是一个可靠的 Agent Runtime 助手。"
    max_history_messages: int = 20
    max_tool_result_chars: int = 2_000
    max_summary_chars: int = 4_000
    max_context_chars: int = 24_000

    def __post_init__(self) -> None:
        if not self.system_prompt:
            raise ValueError("system prompt must not be empty")
        _require_positive_int("max_history_messages", self.max_history_messages)
        _require_positive_int("max_tool_result_chars", self.max_tool_result_chars)
        _require_positive_int("max_summary_chars", self.max_summary_chars)
        _require_positive_int("max_context_chars", self.max_context_chars)


class ExtractiveHistorySummarizer:
    """以角色标记合并公开历史，作为无需外部模型的基础摘要器。"""

    _ROLE_LABELS: ClassVar[dict[MessageRole, str]] = {
        MessageRole.USER: "用户",
        MessageRole.ASSISTANT: "助手",
        MessageRole.TOOL: "工具结果",
        MessageRole.SYSTEM: "系统",
    }

    def __init__(self, *, max_summary_chars: int = 4_000) -> None:
        _require_positive_int("max_summary_chars", max_summary_chars)
        self._max_summary_chars = max_summary_chars

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
        return truncate_summary("\n".join(lines), self._max_summary_chars)


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
        self._config = config or ContextConfig()
        self._summarizer = summarizer or ExtractiveHistorySummarizer(
            max_summary_chars=self._config.max_summary_chars
        )

    def build(
        self,
        request: RunRequest,
        tool_results: tuple[ToolResult, ...],
    ) -> tuple[Message, ...]:
        """构建系统约束加当前 Session 最近历史；本轮结果已由循环持久化。

        最终送给模型的消息顺序（从上到下）：

            [system] 角色约束 + 工具名称/描述/参数 Schema
            [system] 较早历史摘要              <- 可选，受 max_summary_chars
            [最近消息 ...]                     <- 受 max_history_messages
            [TOOL 信封 ...]                    <- 受 max_tool_result_chars

        压缩分三层，从粗到细，任何一层都不拆开“工具调用 + 工具结果”：

            1) 条数预算：超出最近窗口的旧消息，整批摘要；
            2) 总字符预算：仍超 max_context_chars，就按消息组继续往摘要里推；
            3) 摘要展示预算：最后才缩短摘要文本本身，最近消息保持原样。

        摘要先在内存里累积，整个 Context 成功组装后才写回 Session。这样摘要器
        抛错时不会出现“覆盖范围前进了、摘要却没写完”的坏状态。
        """

        session = self._session_store.get(request.session_id)
        if session is None:
            raise LookupError(f"session not found: {request.session_id}")

        system_content = self._system_content()
        if len(system_content) > self._config.max_context_chars:
            raise ContextBudgetError(
                "system prompt alone exceeds max_context_chars"
            )

        envelopes = self._tool_envelopes(session, tool_results)
        original_summary = session.summary
        original_covered = (
            original_summary.covered_message_count if original_summary else 0
        )
        summary_content = (
            truncate_summary(original_summary.content, self._config.max_summary_chars)
            if original_summary is not None
            else None
        )
        # covered 是“摘要与最近窗口的分界线”：
        #   messages[:covered] 已被摘要替代，不再进入模型输入；
        #   messages[covered:] 是最近窗口，原样保留。
        # 它只能单调递增，否则同一批消息会被重复摘要。
        covered = original_covered

        # 第一层：消息条数预算。目标是“移出最近窗口”的旧消息前缀。
        target_count = min(
            len(session.messages),
            max(0, len(session.messages) - self._config.max_history_messages),
        )
        if target_count > covered:
            target_count = max(
                covered,
                self._group_floor(session.messages, target_count),
            )
            if target_count > covered:
                summary_content = self._summarize_batch(
                    session,
                    envelopes,
                    summary_content,
                    covered,
                    target_count,
                )
                covered = target_count

        context = self._assemble(
            system_content,
            summary_content,
            session.messages[covered:],
            envelopes,
        )

        # 第二层：总字符预算。仍超预算时按完整消息组继续摘要旧消息。
        while self._context_chars(context) > self._config.max_context_chars:
            next_covered = self._next_group_boundary(session.messages, covered)
            last_group_start = self._last_group_start(session.messages)
            if next_covered <= covered or next_covered > last_group_start:
                break
            summary_content = self._summarize_batch(
                session,
                envelopes,
                summary_content,
                covered,
                next_covered,
            )
            covered = next_covered
            context = self._assemble(
                system_content,
                summary_content,
                session.messages[covered:],
                envelopes,
            )

        # 第三层：最近消息组不能拆；若仍超预算，只缩短摘要的“展示长度”。
        # 存储摘要仍受 max_summary_chars 约束，覆盖范围不变。
        if self._context_chars(context) > self._config.max_context_chars and (
            summary_content is not None
        ):
            selected = tuple(
                self._render_message(message, envelopes)
                for message in session.messages[covered:]
            )
            prefix = "较早历史摘要：\n"
            fixed_chars = len(system_content) + sum(
                len(message.content) for message in selected
            )
            allowed_summary_chars = (
                self._config.max_context_chars - fixed_chars - len(prefix)
            )
            if allowed_summary_chars >= 1:
                context = self._assemble(
                    system_content,
                    truncate_summary(summary_content, allowed_summary_chars),
                    session.messages[covered:],
                    envelopes,
                )

        # 走到这里说明“只剩最后一组消息”仍然超预算。本版本不拆分单条消息，
        # 所以单条超长输入（例如一次粘贴几万字）会以 context_error 终止，而不是
        # 被静默截断。这是有意的取舍：宁可明确失败，也不把用户输入改得面目全非。
        if self._context_chars(context) > self._config.max_context_chars:
            raise ContextBudgetError(
                "context cannot fit within max_context_chars without splitting a message"
            )

        # 只有成功组装后才落盘，失败时覆盖范围保持原值，下一轮可重试。
        if summary_content is not None and (
            covered > original_covered
            or (
                original_summary is not None
                and summary_content != original_summary.content
                and covered == original_covered
            )
        ):
            self._session_store.set_summary(
                session.session_id,
                SessionSummary(summary_content, covered_message_count=covered),
            )
        return context

    def _summarize_batch(
        self,
        session: Session,
        envelopes: Mapping[str, tuple[ToolCall | None, ToolResult]],
        previous_summary: str | None,
        start: int,
        end: int,
    ) -> str:
        """摘要 ``[start, end)`` 的连续消息；摘要前先限制单条工具结果。"""

        messages = tuple(
            self._render_message(message, envelopes)
            for message in session.messages[start:end]
        )
        content = self._summarizer.summarize(previous_summary, messages)
        content = truncate_summary(content, self._config.max_summary_chars)
        if not content:
            raise ValueError("summary must not be empty")
        return content

    def _assemble(
        self,
        system_content: str,
        summary_content: str | None,
        history: Sequence[Message],
        envelopes: Mapping[str, tuple[ToolCall | None, ToolResult]],
    ) -> tuple[Message, ...]:
        """按固定顺序组装系统约束、摘要、最近历史和工具结果信封。"""

        summary_messages = (
            (
                Message(
                    MessageRole.SYSTEM,
                    f"较早历史摘要：\n{summary_content}",
                ),
            )
            if summary_content is not None
            else ()
        )
        return (
            Message(MessageRole.SYSTEM, system_content),
            *summary_messages,
            *(
                self._render_message(message, envelopes)
                for message in history
            ),
        )

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

    def _render_message(
        self,
        message: Message,
        envelopes: Mapping[str, tuple[ToolCall | None, ToolResult]],
    ) -> Message:
        """工具消息优先渲染成结构化信封，其它角色原样返回。"""

        if message.role is not MessageRole.TOOL:
            return message
        call_id = message.tool_call_id or ""
        pair = envelopes.get(call_id)
        if pair is not None:
            call, result = pair
            content = render_tool_envelope(
                call,
                result,
                max_result_chars=self._config.max_tool_result_chars,
                max_arguments_chars=self._config.max_tool_result_chars,
            )
            return Message(MessageRole.TOOL, content, call_id)
        return self._limit_tool_message(message)

    def _limit_tool_message(self, message: Message) -> Message:
        """没有对应结构化结果时，至少保证纯文本工具消息不超预算。"""

        if message.role is not MessageRole.TOOL:
            return message
        content = truncate_text(
            message.content,
            self._config.max_tool_result_chars,
        )
        return Message(MessageRole.TOOL, content, message.tool_call_id)

    def _tool_envelopes(
        self,
        session: Session,
        tool_results: Sequence[ToolResult],
    ) -> dict[str, tuple[ToolCall | None, ToolResult]]:
        """按 ``call_id`` 关联当前 Session 的工具调用与结果。

        ``tool_results`` 是本轮循环传入的结果；若 Session 已经持久化同一结果，
        以 Session 快照为准，避免调用方传入的临时结果覆盖已确认状态。只有结果、
        没有对应调用时仍保留结果元数据，避免错误类型在 Context 中丢失。
        """

        calls = {call.call_id: call for call in session.tool_calls}
        persisted = {result.call_id: result for result in session.tool_results}
        results = dict(persisted)
        for result in tool_results:
            call = calls.get(result.call_id)
            # 只接受当前 Session 已记录的调用所对应的结果，防止调用方把其它
            # Session 的工具结果通过 build 参数带进来。
            if call is not None and call.name == result.tool_name:
                results.setdefault(result.call_id, result)

        envelopes: dict[str, tuple[ToolCall | None, ToolResult]] = {}
        for call_id, call in calls.items():
            result = results.get(call_id)
            if result is not None and result.tool_name == call.name:
                envelopes[call_id] = (call, result)
        for call_id, result in persisted.items():
            envelopes.setdefault(call_id, (None, result))
        return envelopes

    @staticmethod
    def _context_chars(messages: Sequence[Message]) -> int:
        return sum(len(message.content) for message in messages)

    @staticmethod
    def _message_groups(messages: Sequence[Message]) -> list[tuple[int, int]]:
        """把必须同进同出的消息划为一组。

        - 连续的 TOOL 消息视为同一批工具结果，不能只保留其中一条。
        - 紧邻 TOOL 消息之前的 ASSISTANT 消息视为工具调用声明，与结果同组。
        - 其它消息各自成组。

        为什么必须分组：如果只把 TOOL 结果摘要掉、却留下它的调用声明（或反过来），
        模型会看到“有结果没调用”或“有调用没结果”的残缺历史，容易误判下一步。
        分组保证一次工具交互要么完整保留，要么完整进入摘要。
        """

        groups: list[tuple[int, int]] = []
        index = 0
        while index < len(messages):
            start = index
            if messages[index].role is MessageRole.TOOL:
                while (
                    index < len(messages)
                    and messages[index].role is MessageRole.TOOL
                ):
                    index += 1
            elif (
                messages[index].role is MessageRole.ASSISTANT
                and index + 1 < len(messages)
                and messages[index + 1].role is MessageRole.TOOL
            ):
                index += 1
                while (
                    index < len(messages)
                    and messages[index].role is MessageRole.TOOL
                ):
                    index += 1
            else:
                index += 1
            groups.append((start, index))
        return groups

    @classmethod
    def _group_floor(cls, messages: Sequence[Message], target: int) -> int:
        """把目标下标向下取到消息组边界，避免摘要半个工具交互。"""

        if target <= 0:
            return 0
        if target >= len(messages):
            return len(messages)
        for start, end in cls._message_groups(messages):
            if target < end:
                return min(target, start)
        return len(messages)

    @classmethod
    def _next_group_boundary(cls, messages: Sequence[Message], covered: int) -> int:
        """返回覆盖 ``covered`` 之后下一组的结束下标。"""

        if covered >= len(messages):
            return len(messages)
        for _start, end in cls._message_groups(messages):
            if covered < end:
                return end
        return len(messages)

    @classmethod
    def _last_group_start(cls, messages: Sequence[Message]) -> int:
        groups = cls._message_groups(messages)
        return groups[-1][0] if groups else 0


def _redact_arguments(value: Any) -> Any:
    """递归脱敏工具参数中的敏感字段，不修改原始 Mapping。"""

    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if str(key).casefold() in _SENSITIVE_ARGUMENT_KEYS
                else _redact_arguments(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_arguments(item) for item in value]
    return value


def render_tool_envelope(
    call: ToolCall | None,
    result: ToolResult,
    *,
    max_result_chars: int,
    max_arguments_chars: int,
) -> str:
    """把一次工具交互渲染成模型可读的结构化信封。

    参数与结果分别受字符预算限制，``call_id`` 用于与 Session 中保存的调用一一
    对应。只有结果、没有调用记录时 ``arguments`` 为 null，避免伪造参数。
    这里只呈现公开的调用事实，不包含模型私有思维链。
    """

    arguments: Any = None
    if call is not None:
        redacted = _redact_arguments(dict(call.arguments))
        encoded_arguments = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        arguments = (
            redacted
            if len(encoded_arguments) <= max_arguments_chars
            else {
                "_truncated_json": truncate_text(
                    encoded_arguments,
                    max_arguments_chars,
                )
            }
        )
    payload: dict[str, Any] = {
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "arguments": arguments,
        "status": result.status.value,
        "result": _render_tool_result(result.content, max_result_chars),
        "error_type": result.error_type,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _render_tool_result(content: str, limit: int) -> str:
    """限制工具结果长度；若结果是 JSON 对象/数组，则递归脱敏敏感字段。

    先解析完整 JSON 再脱敏、再截断，避免长 JSON 在敏感字段之前被截断后，
    前缀里的密钥仍然进入模型 Context。
    """

    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return truncate_text(content, limit)
    if not isinstance(decoded, (Mapping, list)):
        return truncate_text(content, limit)
    redacted = _redact_arguments(decoded)
    return truncate_text(
        json.dumps(redacted, ensure_ascii=False, sort_keys=True),
        limit,
    )
