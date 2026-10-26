"""Trace 敏感字段脱敏规则。

脱敏是写入 Trace 前的最后一道安全边界：任何被标记为敏感的字段名（例如
`api_key`、`authorization`、`password`）都会被替换为固定占位符，并且对嵌套的
Mapping、tuple、list 递归生效。内存版与 JSONL 版 Recorder 共用这里的规则，
避免两处实现出现不一致而导致某一处泄密。

这里只做"字段名匹配"，不做内容识别：Trace 不应包含密钥，也不应依赖正则去
猜测哪些字符串像密钥。字段命名由本文件配合各调用点共同保证。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
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


def redact_attributes(
    attributes: Mapping[str, Any],
    config: TraceRedactionConfig,
) -> dict[str, Any]:
    """递归脱敏事件属性，返回新的普通字典，不修改入参。"""

    return {
        key: (
            config.replacement
            if isinstance(key, str) and key.casefold() in config.sensitive_field_names
            else redact_value(value, config)
        )
        for key, value in attributes.items()
    }


def redact_value(value: Any, config: TraceRedactionConfig) -> Any:
    """递归处理嵌套容器；字符串等标量原样返回。"""

    if isinstance(value, Mapping):
        return redact_attributes(value, config)
    if isinstance(value, (list, tuple)):
        return [redact_value(item, config) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item, config) for item in value]
    return value


def redact_event(event: TraceEvent, config: TraceRedactionConfig) -> TraceEvent:
    """返回属性已脱敏的事件副本。"""

    return TraceEvent(
        event_type=event.event_type,
        session_id=event.session_id,
        run_id=event.run_id,
        trace_id=event.trace_id,
        iteration=event.iteration,
        timestamp=event.timestamp,
        attributes=MappingProxyType(redact_attributes(event.attributes, config)),
    )
