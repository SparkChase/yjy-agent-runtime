"""无外部网络依赖的最小内置工具集。"""

import ast
import operator
from collections.abc import Mapping
from math import isfinite
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
    """使用受限 AST 计算基本算术，不执行任意 Python 代码。

    除了"只允许算术"，还必须限制**资源消耗**：``9**9**9`` 在语法上完全合法，
    但会让解释器算到天荒地老，一次工具调用就能拖垮整个进程。因此这里设四道
    闸门，任何一道超限都立即失败，绝不"先算完再说"：

    - 表达式长度：挡住超长输入；
    - 嵌套深度：挡住 ``((((1))))`` 式的深递归；
    - 指数绝对值：挡住天文数字级别的幂运算（指数本身也是先算出来的，
      所以 ``9**9**9`` 的指数 387420489 会在这里被拒绝）；
    - 结果规模：整数位数与浮点量级，挡住中间结果的爆炸式增长。
    """

    _definition = ToolDefinition(
        name="calculator",
        description="计算只包含数字和基本算术运算符的表达式（长度、嵌套与幂次均受限）",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "minLength": 1}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    )
    # 资源上限：数值取自"够用且明显安全"，不是性能调优的结果。
    max_expression_chars: ClassVar[int] = 200
    max_depth: ClassVar[int] = 32
    max_exponent: ClassVar[int] = 64
    max_result_bits: ClassVar[int] = 1_024
    max_result_float: ClassVar[float] = 1e100
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
        if len(expression) > self.max_expression_chars:
            raise ValueError(
                f"expression must be at most {self.max_expression_chars} characters"
            )
        try:
            tree = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError, RecursionError) as error:
            raise ValueError("expression is not a safe arithmetic expression") from error
        try:
            value = self._evaluate(tree.body, depth=1)
        except (ArithmeticError, RecursionError) as error:
            # 除零、浮点溢出等仍按"不是安全算术表达式"对外，不泄露内部细节。
            raise ValueError("expression is not a safe arithmetic expression") from error
        return str(value)

    def _evaluate(self, node: ast.AST, *, depth: int) -> int | float:
        if depth > self.max_depth:
            raise ValueError(
                f"expression nesting must be at most {self.max_depth} levels"
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                raise TypeError("only numeric arithmetic is allowed")
            return self._check_magnitude(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in self._binary_operators:
            left = self._evaluate(node.left, depth=depth + 1)
            right = self._evaluate(node.right, depth=depth + 1)
            if isinstance(node.op, ast.Pow):
                self._check_exponent(right)
            return self._check_magnitude(
                self._binary_operators[type(node.op)](left, right)
            )
        if isinstance(node, ast.UnaryOp) and type(node.op) in self._unary_operators:
            return self._check_magnitude(
                self._unary_operators[type(node.op)](
                    self._evaluate(node.operand, depth=depth + 1)
                )
            )
        raise ValueError("only numeric arithmetic is allowed")

    def _check_exponent(self, exponent: float) -> None:
        """指数必须在允许范围内，否则幂运算可能直接耗尽 CPU。"""

        if not isinstance(exponent, (int, float)) or abs(exponent) > self.max_exponent:
            raise ValueError(
                f"exponent must be within ±{self.max_exponent}"
            )

    def _check_magnitude(self, value: float) -> int | float:
        """限制结果规模，避免中间结果把内存撑爆。"""

        if isinstance(value, int):
            if value.bit_length() > self.max_result_bits:
                raise ValueError(
                    f"result must fit within {self.max_result_bits} bits"
                )
            return value
        if isinstance(value, float):
            if not isfinite(value) or abs(value) > self.max_result_float:
                raise ValueError("result must be a finite number within 1e100")
            return value
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
        query = str(arguments["query"]).casefold().strip()
        # 按空格切词，任一关键词命中即可。这样 "session 隔离" 能命中
        # 同时包含 session 和 隔离 的文档，而不是要求整串连续出现。
        tokens = [token for token in query.split() if token] or [query]
        matches = [
            f"{title}: {content}"
            for title, content in self._documents
            if any(
                token in title.casefold() or token in content.casefold()
                for token in tokens
            )
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


def register_builtin_tools(registry: ToolRegistry, session_store: Any) -> None:
    """注册题目要求的三个工具及无副作用的 echo 工具。"""

    registry.register(EchoTool())
    registry.register(CalculatorTool())
    registry.register(MockSearchTool())
    registry.register(TodoTool(session_store))
