"""基于工具注册表的安全执行器。"""

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
    ) -> None:
        if max_result_chars < 1:
            raise ValueError("max_result_chars must be at least 1")
        self._registry = registry
        self._max_result_chars = max_result_chars

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
            content = tool.invoke(call.arguments)
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
