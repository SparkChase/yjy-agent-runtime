"""把同一条 Trace 事件广播给多个 Recorder。

CLI 需要两种能力：运行中在内存里保留事件用于即时诊断，运行结束后把完整链路落盘。
与其让某个 Recorder 同时承担两种职责，不如用一个扇出 Recorder 组合它们，各自保持
单一职责：`InMemoryTraceRecorder` 管内存，`JsonlTraceRecorder` 管文件。

广播按传入顺序执行；任何一个 Recorder 抛错都会向上传播，不吞掉审计失败。
"""

from __future__ import annotations

from collections.abc import Sequence

from yjy_agent_runtime.domain import TraceEvent


class FanOutTraceRecorder:
    """按顺序把事件转交给多个下游 Recorder。"""

    def __init__(self, recorders: Sequence[object]) -> None:
        if not recorders:
            raise ValueError("fan-out recorder requires at least one recorder")
        self._recorders = tuple(recorders)

    def record(self, event: TraceEvent) -> None:
        """把事件依次写入所有下游 Recorder。"""

        for recorder in self._recorders:
            recorder.record(event)  # type: ignore[attr-defined]

    def close(self) -> None:
        """关闭所有支持 close 的下游 Recorder，忽略不支持者。"""

        for recorder in self._recorders:
            close = getattr(recorder, "close", None)
            if callable(close):
                close()
