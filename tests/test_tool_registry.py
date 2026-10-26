"""工具定义与注册表测试。"""

from collections.abc import Mapping
from typing import Any

import pytest

from yjy_agent_runtime.infrastructure.tools import (
    ToolDefinition,
    ToolRegistry,
)


class EchoTool:
    """用于验证注册行为的最小测试工具。"""

    def __init__(self, name: str = "echo") -> None:
        self._definition = ToolDefinition(
            name=name,
            description="原样返回输入文本",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        return str(arguments["text"])


def test_tool_definition_rejects_empty_name_and_description() -> None:
    with pytest.raises(ValueError, match="name"):
        ToolDefinition(name="", description="描述")

    with pytest.raises(ValueError, match="description"):
        ToolDefinition(name="echo", description="")


def test_tool_definition_copies_parameters_as_read_only_mapping() -> None:
    parameters = {"type": "object"}
    definition = ToolDefinition(
        name="echo",
        description="原样返回输入文本",
        parameters=parameters,
        has_side_effects=True,
    )

    parameters["type"] = "changed"

    assert definition.parameters["type"] == "object"
    assert definition.has_side_effects is True
    with pytest.raises(TypeError):
        definition.parameters["type"] = "invalid"  # type: ignore[index]


def test_registry_registers_and_resolves_tool() -> None:
    registry = ToolRegistry()
    tool = EchoTool()

    registry.register(tool)

    assert registry.get("echo") is tool
    assert registry.definitions() == (tool.definition,)


def test_registry_rejects_duplicate_name() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())


def test_registry_reports_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="unknown tool: missing"):
        registry.get("missing")
