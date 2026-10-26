"""工具注册表实现。

工具"长什么样"这一契约（`ToolDefinition` 与 `Tool` 协议）定义在应用层
`yjy_agent_runtime.agent.ports`，因为 Context 和模型都要消费它。这里只保留
基建实现：如何按唯一名称保存工具、如何查找和导出定义视图。

为兼容既有导入路径，本模块继续 re-export `ToolDefinition` 与 `Tool`。
"""

from yjy_agent_runtime.agent.ports import Tool, ToolDefinition

__all__ = ["Tool", "ToolDefinition", "ToolRegistry"]


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
