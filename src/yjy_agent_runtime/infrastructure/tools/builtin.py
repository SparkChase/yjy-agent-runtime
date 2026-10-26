"""无外部副作用的最小内置工具。"""

from collections.abc import Mapping
from typing import Any

from .registry import ToolDefinition, ToolRegistry


class EchoTool:
    """原样返回经过 Schema 校验的文本。"""

    _definition = ToolDefinition(
        name="echo",
        description="原样返回给定文本，用于验证工具调用链路",
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "需要原样返回的文本",
                }
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    @property
    def definition(self) -> ToolDefinition:
        """返回供注册表和模型使用的工具定义。"""

        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        """返回执行器已经校验过的文本参数。"""

        return str(arguments["text"])


def register_builtin_tools(registry: ToolRegistry) -> None:
    """向指定注册表注册当前版本提供的全部内置工具。"""

    registry.register(EchoTool())
