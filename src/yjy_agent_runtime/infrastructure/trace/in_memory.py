"""具有敏感字段脱敏能力的进程内 TraceRecorder。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from yjy_agent_runtime.domain import TraceEvent


@dataclass(frozen=True, slots=True)
class TraceRedactionConfig:
    """Trace 写入前使用的敏感字段规则。"""

    sensitive_field_names: frozenset[str] = frozenset(
        {
            "api_key",
            "apikey",
            "authorization",
            "cookie",
            "password",
            "secret",
            "token",
        }
    )
    replacement: str = "[REDACTED]"

    def __post_init__(self) -> None:
        if not self.sensitive_field_names:
            raise ValueError("sensitive field names must not be empty")
        if not self.replacement:
            raise ValueError("redaction replacement must not be empty")


class InMemoryTraceRecorder:
    """保存脱敏后的不可变 TraceEvent，供测试和本地调试使用。"""

    def __init__(self, config: TraceRedactionConfig | None = None) -> None:
        self._config = config or TraceRedactionConfig()
        self._events: list[TraceEvent] = []

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """返回事件快照，避免调用方修改内部列表。"""

        return tuple(self._events)

    def record(self, event: TraceEvent) -> None:
        """递归脱敏事件属性后保存，不修改调用方传入的事件。"""

        sanitized = self._redact_mapping(event.attributes)
        self._events.append(
            TraceEvent(
                event_type=event.event_type,
                session_id=event.session_id,
                run_id=event.run_id,
                trace_id=event.trace_id,
                iteration=event.iteration,
                timestamp=event.timestamp,
                attributes=sanitized,
            )
        )

    def _redact_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: (
                self._config.replacement
                if key.casefold() in self._config.sensitive_field_names
                else self._redact_value(item)
            )
            for key, item in value.items()
        }

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self._redact_mapping(value)
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return [self._redact_value(item) for item in value]
        return value
