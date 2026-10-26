"""结构化 Trace 基础设施实现。

本子包回答的是"这次运行到底发生了什么"：一次会话从 `run_started` 到
`run_terminated` 的完整链路，包括每轮的 LLM 决策、工具调用、异常和耗时。

文件职责：
- `redaction.py`：敏感字段脱敏规则，内存版与文件版共用。
- `in_memory.py`：进程内 Recorder，事件留在内存，供测试与调试。
- `jsonl.py`：按行落盘的 Recorder，供真实 CLI 运行后审计。
"""

from .fanout import FanOutTraceRecorder
from .in_memory import InMemoryTraceRecorder
from .jsonl import JsonlTraceRecorder, TraceWriteError
from .redaction import TraceRedactionConfig

__all__ = [
    "FanOutTraceRecorder",
    "InMemoryTraceRecorder",
    "JsonlTraceRecorder",
    "TraceRedactionConfig",
    "TraceWriteError",
]
