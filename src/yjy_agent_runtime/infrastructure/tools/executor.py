"""基于工具注册表的安全执行器。"""

from queue import Empty, Queue
from threading import Thread
from typing import Any

from jsonschema import SchemaError, ValidationError, validate

from yjy_agent_runtime.domain import ToolCall, ToolResult, ToolResultStatus

from .registry import ToolRegistry


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
        """在指定 Session 中执行工具，并保留明确的失败类型。"""

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
            if hasattr(tool, "invoke_with_session"):
                content = self._invoke_with_timeout(
                    tool,
                    (session_id, call.arguments),
                    session_aware=True,
                )
            else:
                content = self._invoke_with_timeout(tool, call.arguments)
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
            )
        if not content:
            return self._error_result(
                call,
                "InvalidToolResultError",
                "tool result must not be empty",
            )

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.SUCCESS,
            content=self._limit_content(content),
        )

    def _invoke_with_timeout(
        self,
        tool: object,
        arguments: object,
        *,
        session_aware: bool = False,
    ) -> Any:
        """在守护线程中执行工具，超时后返回结构化错误。

        注意语义：``join(timeout)`` 只是“不再等待”，并不会杀死工作线程。
        超时返回后，工具可能仍在后台继续执行。对 ``calculator``/``search`` 这类
        无副作用工具没有影响；对 ``todo`` 这类有写入副作用的工具，则存在
        “已向模型报告超时、稍后却真的写入了”的风险。这是已知边界，修复方案见
        docs/runtime-remediation-plan.md 的 P1-03。
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
            raise TimeoutError(
                f"tool execution exceeded {self._timeout_seconds:g} seconds"
            )
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
    ) -> ToolResult:
        """构造可交给模型继续判断的错误结果。"""

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.name,
            status=ToolResultStatus.ERROR,
            content=self._limit_content(message or error_type),
            error_type=error_type,
        )

    def _limit_content(self, content: str) -> str:
        """限制工具结果大小，避免后续 Context 无界增长。"""

        if len(content) <= self._max_result_chars:
            return content
        marker = "...[truncated]"
        if self._max_result_chars <= len(marker):
            return marker[: self._max_result_chars]
        return content[: self._max_result_chars - len(marker)] + marker
