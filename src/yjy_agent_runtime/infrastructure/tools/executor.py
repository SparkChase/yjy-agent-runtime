"""基于工具注册表的安全执行器。

执行器要回答两个不同的问题，不能混为一谈：

1. **参数合法吗**：调用前用 JSON Schema 校验，非法参数不进入工具实现。
2. **超时意味着什么**：``join(timeout)`` 只是"不再等待"，线程还在跑。对无副作用
   工具无所谓；对有写入副作用的工具，如果超时后线程继续执行，就会出现"已向模型
   报告失败、稍后却真的写入了"这种不可解释的状态。

因此按 ``ToolDefinition.has_side_effects`` 分流：

- 有副作用且实现了 ``invoke_with_session_deadline``：放进工作线程，同时给一个
  ``Deadline``；超时后工具会在写入前检查截止时间并放弃提交（``COOPERATIVE``）。
- 有副作用但没有协作能力：在调用线程同步执行，不设超时。宁可慢，也不谎报
  "已取消"（``SYNCHRONOUS``）。
- 无副作用：沿用线程超时；超时后线程被放弃，但按约定不会改变外部状态
  （``ABANDONED_SAFE``）。

每次执行得到的语义都会写进 ``ToolResult.timeout_semantics``，由 AgentLoop 落到
Trace，调用方不必靠猜。
"""

from queue import Empty, Queue
from threading import Thread
from typing import Any

from jsonschema import SchemaError, ValidationError, validate

from yjy_agent_runtime.domain import (
    TimeoutSemantics,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)

from .deadline import Deadline
from .registry import ToolRegistry


class ToolExecutionTimeout(TimeoutError):
    """执行器判定超时，并携带"是否可能仍有后续副作用"的语义。"""

    def __init__(self, message: str, semantics: TimeoutSemantics) -> None:
        super().__init__(message)
        self.semantics = semantics


class RegistryToolExecutor:
    """校验工具调用并把可恢复失败转换为结构化结果。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_result_chars: int = 10_000,
        timeout_seconds: float = 30.0,
    ) -> None:
        if max_result_chars < 1:
            raise ValueError("max_result_chars must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._registry = registry
        self._max_result_chars = max_result_chars
        self._timeout_seconds = timeout_seconds

    def execute(self, session_id: str, call: ToolCall) -> ToolResult:
        """在指定 Session 中执行工具，并保留明确的失败类型与超时语义。"""

        if not session_id:
            raise ValueError("session id must not be empty")

        try:
            tool = self._registry.get(call.name)
        except KeyError as error:
            return self._error_result(call, "UnknownToolError", str(error))

        try:
            validate(
                instance=dict(call.arguments),
                schema=dict(tool.definition.parameters),
            )
        except ValidationError as error:
            return self._error_result(
                call,
                "ToolArgumentsValidationError",
                error.message,
            )
        except SchemaError as error:
            return self._error_result(
                call,
                "InvalidToolSchemaError",
                error.message,
            )

        try:
            content, semantics = self._dispatch(tool, session_id, call.arguments)
        except ToolExecutionTimeout as error:
            return self._error_result(
                call,
                "TimeoutError",
                str(error),
                semantics=error.semantics,
            )
        except Exception as error:  # noqa: BLE001
            return self._error_result(
                call,
                type(error).__name__,
                str(error) or "tool execution failed",
            )

        if not isinstance(content, str):
            return self._error_result(
                call,
                "InvalidToolResultError",
                "tool result must be a string",
                semantics=semantics,
            )
        if not content:
            return self._error_result(
                call,
                "InvalidToolResultError",
                "tool result must not be empty",
                semantics=semantics,
            )

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.SUCCESS,
            content=self._limit_content(content),
            timeout_semantics=semantics,
        )

    def _dispatch(
        self,
        tool: object,
        session_id: str,
        arguments: object,
    ) -> tuple[Any, TimeoutSemantics]:
        """按副作用标记选择执行方式，并返回对应的超时语义。"""

        if tool.definition.has_side_effects:  # type: ignore[attr-defined]
            if hasattr(tool, "invoke_with_session_deadline"):
                return self._invoke_cooperatively(tool, session_id, arguments)
            return (
                self._invoke_inline(tool, session_id, arguments),
                TimeoutSemantics.SYNCHRONOUS,
            )

        if hasattr(tool, "invoke_with_session"):
            return (
                self._invoke_with_timeout(
                    tool,
                    (session_id, arguments),
                    session_aware=True,
                ),
                TimeoutSemantics.ABANDONED_SAFE,
            )
        return (
            self._invoke_with_timeout(tool, arguments),
            TimeoutSemantics.ABANDONED_SAFE,
        )

    def _invoke_inline(
        self,
        tool: object,
        session_id: str,
        arguments: object,
    ) -> Any:
        """在调用线程同步执行副作用工具，不存在"超时"这种模糊状态。"""

        if hasattr(tool, "invoke_with_session"):
            return tool.invoke_with_session(session_id, arguments)  # type: ignore[attr-defined]
        return tool.invoke(arguments)  # type: ignore[attr-defined]

    def _invoke_cooperatively(
        self,
        tool: object,
        session_id: str,
        arguments: object,
    ) -> tuple[Any, TimeoutSemantics]:
        """带协作式截止时间地执行副作用工具。

        执行器仍然只在 ``timeout_seconds`` 内等待，但这里的"超时"是可信的：
        工具在写入前会检查 ``Deadline``，过期即放弃提交。
        """

        deadline = Deadline(self._timeout_seconds)
        outcome: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                result = tool.invoke_with_session_deadline(  # type: ignore[attr-defined]
                    session_id,
                    arguments,
                    deadline,
                )
            except Exception as error:  # noqa: BLE001
                outcome.put((False, error))
            else:
                outcome.put((True, result))

        worker = Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(self._timeout_seconds)
        if worker.is_alive():
            raise ToolExecutionTimeout(
                f"tool execution exceeded {self._timeout_seconds:g} seconds "
                "and was cancelled at its deadline",
                TimeoutSemantics.COOPERATIVE,
            )
        return self._take_outcome(outcome), TimeoutSemantics.COOPERATIVE

    def _invoke_with_timeout(
        self,
        tool: object,
        arguments: object,
        *,
        session_aware: bool = False,
    ) -> Any:
        """在守护线程中执行无副作用工具，超时后返回结构化错误。

        超时只是"不再等待"：工作线程可能仍在运行。对 ``calculator``/``search``
        这类无副作用工具没有影响，因此语义记为 ``ABANDONED_SAFE``；有副作用的
        工具不会走到这里（见 ``_dispatch``）。
        """

        outcome: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                if session_aware:
                    result = tool.invoke_with_session(*arguments)  # type: ignore[attr-defined]
                else:
                    result = tool.invoke(arguments)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                outcome.put((False, error))
            else:
                outcome.put((True, result))

        worker = Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(self._timeout_seconds)
        if worker.is_alive():
            raise ToolExecutionTimeout(
                f"tool execution exceeded {self._timeout_seconds:g} seconds",
                TimeoutSemantics.ABANDONED_SAFE,
            )
        return self._take_outcome(outcome)

    @staticmethod
    def _take_outcome(outcome: Queue[tuple[bool, Any]]) -> Any:
        """取出工作线程的结果；异常按原类型重新抛出。"""

        try:
            succeeded, value = outcome.get_nowait()
        except Empty as error:
            raise RuntimeError("tool execution returned no result") from error
        if not succeeded:
            raise value
        return value

    def _error_result(
        self,
        call: ToolCall,
        error_type: str,
        message: str,
        *,
        semantics: TimeoutSemantics | None = None,
    ) -> ToolResult:
        """构造可交给模型继续判断的错误结果。"""

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.ERROR,
            content=self._limit_content(message or error_type),
            error_type=error_type,
            timeout_semantics=semantics,
        )

    def _limit_content(self, content: str) -> str:
        """限制工具结果大小，避免后续 Context 无界增长。"""

        if len(content) <= self._max_result_chars:
            return content
        marker = "...[truncated]"
        if self._max_result_chars <= len(marker):
            return marker[: self._max_result_chars]
        return content[: self._max_result_chars - len(marker)] + marker
