"""进程内 TraceRecorder：把事件留在内存里，供测试与本地调试查看。"""

from yjy_agent_runtime.domain import TraceEvent

from .redaction import TraceRedactionConfig, redact_event


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

        self._events.append(redact_event(event, self._config))
