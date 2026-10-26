"""协作式截止时间：让有副作用的工具在提交写入前有一次明确的检查点。

Python 无法从外部安全地杀死一个正在执行写操作的线程。因此"给副作用工具加超时"
不能靠 ``join(timeout)``，只能靠工具自己配合：执行器把 ``Deadline`` 交给工具，
工具在真正写入前调用 ``raise_if_expired()``。这样"执行器已经向模型报告超时"
和"工具稍后又写了一次"就不会同时发生。

约定（工具实现方）：

1. 耗时准备阶段可以随时检查 ``remaining_seconds()``，提前收工。
2. 真正产生外部副作用之前必须调用 ``raise_if_expired()``，且检查与写入之间
   不应再插入长时间等待。
"""

from time import monotonic


class Deadline:
    """一次工具调用的截止时间，只能判断是否过期，不能强制中断执行。"""

    __slots__ = ("_expires_at",)

    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("deadline timeout must be positive")
        self._expires_at = monotonic() + timeout_seconds

    @property
    def expired(self) -> bool:
        """截止时间是否已过。"""

        return monotonic() >= self._expires_at

    def remaining_seconds(self) -> float:
        """返回剩余秒数，已过期时为 0，便于工具做分阶段判断。"""

        return max(0.0, self._expires_at - monotonic())

    def raise_if_expired(self) -> None:
        """在提交副作用之前调用；已过期则抛出 ``TimeoutError`` 放弃写入。"""

        if self.expired:
            raise TimeoutError("tool deadline exceeded before commit")
