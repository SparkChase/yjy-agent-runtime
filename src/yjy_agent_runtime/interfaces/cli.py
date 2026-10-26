"""YJY Agent Runtime 的最小命令行入口。"""

import argparse
import sys

from yjy_agent_runtime.agent import AgentLoop
from yjy_agent_runtime.domain import RunRequest
from yjy_agent_runtime.infrastructure.context import ContextBuilder
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)
from yjy_agent_runtime.infrastructure.session import InMemorySessionStore
from yjy_agent_runtime.infrastructure.tools import (
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)
from yjy_agent_runtime.infrastructure.trace import InMemoryTraceRecorder


def build_parser() -> argparse.ArgumentParser:
    """构建可测试的命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="运行一次 YJY Agent Runtime 对话")
    parser.add_argument("message", nargs="?", help="用户输入；省略时从 stdin 读取")
    parser.add_argument("--session-id", default="default", help="Session 标识")
    parser.add_argument("--max-iterations", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    """读取一次用户输入，调用真实 LLM 并输出最终结果。"""

    args = build_parser().parse_args(argv)
    user_input = args.message or sys.stdin.readline().strip()
    if not user_input:
        print("请输入非空消息。", file=sys.stderr)
        return 2

    try:
        config = OpenAICompatibleConfig.from_environment()
        llm = OpenAICompatibleLLMClient(config)
        try:
            store = InMemorySessionStore()
            registry = ToolRegistry()
            register_builtin_tools(registry)
            loop = AgentLoop(
                context_provider=ContextBuilder(
                    store,
                    tool_definitions=registry.definitions(),
                ),
                llm_client=llm,
                session_store=store,
                tool_executor=RegistryToolExecutor(registry),
                trace_recorder=InMemoryTraceRecorder(),
            )
            result = loop.run(RunRequest(args.session_id, user_input))
        finally:
            llm.close()
    except Exception as error:  # noqa: BLE001
        print(f"运行失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if result.answer is not None:
        print(result.answer)
        return 0
    print(
        f"运行未返回最终答案，终止原因：{result.termination_reason.value}",
        file=sys.stderr,
    )
    return 1
