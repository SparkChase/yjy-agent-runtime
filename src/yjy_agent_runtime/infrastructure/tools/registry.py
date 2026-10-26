"""工具协议、工具定义和注册表。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """向模型公开的工具元数据和参数 Schema。"""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    has_side_effects: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("tool name must not be empty")
        if not self.description:
            raise ValueError("tool description must not be empty")
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )


class Tool(Protocol):
    """可由 Runtime 注册和调用的工具协议。"""

    @property
    def definition(self) -> ToolDefinition:
        """返回用于注册和模型暴露的工具定义。"""
        ...

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        """使用已经过校验的参数执行工具。"""
        ...


class ToolRegistry:
    """按唯一名称保存工具，并提供稳定的查找与定义视图。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；重复名称会被拒绝，避免静默覆盖。"""

        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        """按名称取得工具；未知名称保留明确错误语义。"""

        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """按注册顺序返回供 LLM 使用的只读工具定义。"""

        return tuple(tool.definition for tool in self._tools.values())
