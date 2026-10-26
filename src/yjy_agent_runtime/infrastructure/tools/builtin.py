"""无外部网络依赖的最小内置工具集。"""

import ast
import operator
from collections.abc import Mapping
from typing import Any, ClassVar

from .registry import ToolDefinition, ToolRegistry


class EchoTool:
    """原样返回经过 Schema 校验的文本。"""

    _definition = ToolDefinition(
        name="echo",
        description="原样返回给定文本，用于验证工具调用链路",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        return str(arguments["text"])


class CalculatorTool:
    """使用受限 AST 计算基本算术，不执行任意 Python 代码。"""

    _definition = ToolDefinition(
        name="calculator",
        description="计算只包含数字和基本算术运算符的表达式",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "minLength": 1}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    )
    _binary_operators: ClassVar[dict[type[ast.operator], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    _unary_operators: ClassVar[dict[type[ast.unaryop], Any]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        expression = str(arguments["expression"])
        try:
            tree = ast.parse(expression, mode="eval")
            value = self._evaluate(tree.body)
        except (ArithmeticError, SyntaxError, ValueError) as error:
            raise ValueError("expression is not a safe arithmetic expression") from error
        return str(value)

    def _evaluate(self, node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary_operators:
            return self._binary_operators[type(node.op)](
                self._evaluate(node.left),
                self._evaluate(node.right),
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary_operators:
            return self._unary_operators[type(node.op)](self._evaluate(node.operand))
        raise ValueError("only numeric arithmetic is allowed")


class MockSearchTool:
    """可预测的本地搜索替身，明确不访问网络。"""

    _definition = ToolDefinition(
        name="search",
        description="在内置 mock 文档中按关键词检索，不访问网络",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    _documents = (
        ("agent-loop", "Agent Loop 通过 Context、LLM 决策和工具结果推进。"),
        ("session", "Session 必须显式携带 session_id，并保持不同窗口隔离。"),
        ("context", "Context 应保留近期历史，并对较早公开历史做摘要。"),
    )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        query = str(arguments["query"]).casefold()
        matches = [
            f"{title}: {content}"
            for title, content in self._documents
            if query in title.casefold() or query in content.casefold()
        ]
        return "\n".join(matches) if matches else "mock search 未找到匹配内容"


class TodoTool:
    """在当前 Session 中新增或列出待办。"""

    _definition = ToolDefinition(
        name="todo",
        description="在当前 Session 中新增或列出待办事项",
        parameters={
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"const": "add"},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["action", "text"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"action": {"const": "list"}},
                    "required": ["action"],
                    "additionalProperties": False,
                },
            ],
        },
        has_side_effects=True,
    )

    def __init__(self, session_store: Any) -> None:
        self._session_store = session_store

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any]) -> str:
        raise RuntimeError("todo tool requires the current session_id")

    def invoke_with_session(
        self,
        session_id: str,
        arguments: Mapping[str, Any],
    ) -> str:
        action = arguments["action"]
        if action == "add":
            self._session_store.append_todo(session_id, str(arguments["text"]))
            return f"已添加待办：{arguments['text']}"
        todos = self._session_store.list_todos(session_id)
        return (
            "\n".join(f"{index}. {item}" for index, item in enumerate(todos, 1))
            or "当前没有待办"
        )


def register_builtin_tools(registry: ToolRegistry, session_store: Any | None = None) -> None:
    """注册题目要求的三个工具及无副作用的 echo 工具。"""

    registry.register(EchoTool())
    registry.register(CalculatorTool())
    registry.register(MockSearchTool())
    if session_store is None:
        from yjy_agent_runtime.infrastructure.session import InMemorySessionStore

        session_store = InMemorySessionStore()
    registry.register(TodoTool(session_store))
