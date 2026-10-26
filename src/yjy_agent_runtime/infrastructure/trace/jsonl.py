"""把运行链路按行落盘的 TraceRecorder。

一个 Trace 就是"这一次运行（run）完整发生了什么"：从 `run_started` 到
`run_terminated`，逐轮记录 LLM 决策、工具调用、异常与耗时。进程结束后内存版
Recorder 的事件会消失，真实 CLI 需要可审计的落盘版本，因此这里把每条事件写成
一行 JSON（JSONL），便于 `jq`、`grep` 或脚本按 `run_id` 关联。

设计取舍：
- 每行一个 JSON 对象，追加写入；一行写完立即 flush，进程崩溃也能保留已发生的事件。
- 写入前统一走 `redaction.py` 脱敏；密钥不会落盘。
- 写入失败抛出 `TraceWriteError`，不静默吞掉——审计缺失必须显式暴露。
- 不做多进程加锁：CLI 每次运行写自己的文件，跨进程共享同一文件不在当前范围内。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self, TextIO

from yjy_agent_runtime.domain import TraceEvent

from .redaction import TraceRedactionConfig, redact_event


class TraceWriteError(RuntimeError):
    """Trace 事件无法写入目标文件。"""


class JsonlTraceRecorder:
    """把脱敏后的 TraceEvent 逐行写入文件的 Recorder。"""

    def __init__(
        self,
        path: str | Path,
        *,
        config: TraceRedactionConfig | None = None,
    ) -> None:
        self._path = Path(path)
        self._config = config or TraceRedactionConfig()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 以追加模式打开：同一次运行的多次 record 顺序落盘。
            self._handle: TextIO = self._path.open("a", encoding="utf-8")
        except OSError as error:
            raise TraceWriteError(f"unable to open trace file: {self._path}") from error
        self._closed = False

    @property
    def path(self) -> Path:
        """返回落盘路径，供 CLI 打印告知用户去哪里查看。"""

        return self._path

    def record(self, event: TraceEvent) -> None:
        """脱敏、序列化并追加一行；失败时抛出明确异常。"""

        if self._closed:
            raise TraceWriteError("trace recorder is already closed")
        line = json.dumps(
            self._encode(redact_event(event, self._config)),
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            self._handle.write(line + "\n")
            self._handle.flush()
        except OSError as error:
            raise TraceWriteError(
                f"unable to write trace event: {self._path}"
            ) from error

    def close(self) -> None:
        """关闭文件句柄；可重复调用。"""

        if self._closed:
            return
        try:
            self._handle.close()
        except OSError as error:
            raise TraceWriteError(
                f"unable to close trace file: {self._path}"
            ) from error
        finally:
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _encode(event: TraceEvent) -> dict[str, Any]:
        """把事件转成可 JSON 序列化的稳定结构。"""

        return {
            "event_type": event.event_type.value,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "trace_id": event.trace_id,
            "iteration": event.iteration,
            "timestamp": event.timestamp,
            "attributes": _jsonable(event.attributes),
        }


def _jsonable(value: Any) -> Any:
    """把 MappingProxyType、tuple 等容器转成 JSON 可序列化形式。"""

    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
