"""YJY Agent Runtime 的最小命令行入口。

一次 CLI 进程只处理一条用户消息，内部可能循环多轮工具调用，最后输出最终回答。
入口层只做三件事：解析参数、装配具体实现、打印结果；Agent 的执行逻辑全部在
`agent.AgentLoop` 里。
"""

import argparse
import os
import sys
from pathlib import Path

from yjy_agent_runtime.agent import AgentLoop, LoopConfig
from yjy_agent_runtime.domain import RunRequest, RunResult
from yjy_agent_runtime.infrastructure.context import ContextBuilder
from yjy_agent_runtime.infrastructure.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleLLMClient,
)
from yjy_agent_runtime.infrastructure.session import JsonFileSessionStore
from yjy_agent_runtime.infrastructure.tools import (
    RegistryToolExecutor,
    ToolRegistry,
    register_builtin_tools,
)
from yjy_agent_runtime.infrastructure.trace import (
    FanOutTraceRecorder,
    InMemoryTraceRecorder,
    JsonlTraceRecorder,
)

from .config import EnvFileError, prepare_environment

# 未显式指定 --trace-file 时，Trace 落到该目录下按 Session 命名。
DEFAULT_TRACE_DIR = Path("logs")


def build_parser() -> argparse.ArgumentParser:
    """构建可测试的命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="运行一次 YJY Agent Runtime 对话")
    parser.add_argument("message", nargs="?", help="用户输入；省略时从 stdin 读取")
    parser.add_argument("--session-id", default="default", help="Session 标识")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument(
        "--env-file",
        default=None,
        help="显式指定配置文件名；省略时自动读取当前目录 .env",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="不读取任何 .env 文件，只使用当前进程环境变量",
    )
    parser.add_argument(
        "--trace-file",
        default=None,
        help="Trace JSONL 落盘路径；省略时写入 YJY_TRACE_DIR（默认 logs/）",
    )
    parser.add_argument(
        "--no-trace",
        action="store_true",
        help="不写 Trace 文件，仅在进程内保留事件",
    )
    return parser


def resolve_trace_path(args: argparse.Namespace) -> Path | None:
    """决定本次运行的 Trace 文件路径；`--no-trace` 时返回 None。"""

    if args.no_trace:
        return None
    if args.trace_file:
        return Path(args.trace_file)
    trace_dir = Path(os.getenv("YJY_TRACE_DIR", str(DEFAULT_TRACE_DIR)))
    return trace_dir / f"{args.session_id}.jsonl"


class TraceSink:
    """本次运行使用的 Trace 输出：内存始终保留，文件可选。

    内存副本用于运行失败时立即打印最近错误；文件副本用于事后审计。
    """

    def __init__(self, path: Path | None) -> None:
        self.memory = InMemoryTraceRecorder()
        self._file: JsonlTraceRecorder | None = None
        self.path = path
        if path is not None:
            self._file = JsonlTraceRecorder(path)
            self.recorder = FanOutTraceRecorder((self.memory, self._file))
        else:
            self.recorder = self.memory

    def close(self) -> None:
        """关闭文件 Recorder；不吞掉写入/关闭错误。"""

        if self._file is not None:
            self._file.close()

    def latest_dependency_error(self):
        """返回最近一次依赖失败事件，供失败诊断使用。"""

        return next(
            (
                event
                for event in reversed(self.memory.events)
                if event.event_type.value == "dependency_failed"
            ),
            None,
        )


def build_loop(
    *,
    llm: OpenAICompatibleLLMClient,
    trace: TraceSink,
    max_iterations: int,
) -> AgentLoop:
    """装配一次运行需要的全部实现，所有具体选择都收敛在入口层。"""

    store = JsonFileSessionStore(os.getenv("YJY_SESSION_DIR", ".yjy-sessions"))
    registry = ToolRegistry()
    register_builtin_tools(registry, store)
    return AgentLoop(
        context_provider=ContextBuilder(
            store,
            tool_definitions=registry.definitions(),
        ),
        llm_client=llm,
        session_store=store,
        tool_executor=RegistryToolExecutor(registry),
        trace_recorder=trace.recorder,
        config=LoopConfig(max_iterations=max_iterations),
    )


def main(argv: list[str] | None = None) -> int:
    """读取一次用户输入，调用真实 LLM 并输出最终结果。"""

    args = build_parser().parse_args(argv)
    user_input = args.message or sys.stdin.readline().strip()
    if not user_input:
        print("请输入非空消息。", file=sys.stderr)
        return 2

    try:
        prepare_environment(
            env_file=args.env_file,
            use_env_file=not args.no_env_file,
        )
    except EnvFileError as error:
        print(f"配置失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    trace = TraceSink(resolve_trace_path(args))
    try:
        config = OpenAICompatibleConfig.from_environment()
        llm = OpenAICompatibleLLMClient(config)
        try:
            loop = build_loop(
                llm=llm,
                trace=trace,
                max_iterations=args.max_iterations,
            )
            result = loop.run(RunRequest(args.session_id, user_input))
        finally:
            llm.close()
            trace.close()
    except Exception as error:  # noqa: BLE001
        _report_trace_path(trace.path)
        print(f"运行失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    _report_trace_path(trace.path)
    return _report_result(result, trace)


def _report_trace_path(path: Path | None) -> None:
    """把 Trace 位置写到 stderr，成功与失败都要告知用户。"""

    if path is not None:
        print(f"trace: {path}", file=sys.stderr)


def _report_result(result: RunResult, trace: TraceSink) -> int:
    """按运行结果决定退出码，并在失败时附带最近依赖错误。"""

    if result.answer is not None:
        print(result.answer)
        return 0
    print(
        f"运行未返回最终答案，终止原因：{result.termination_reason.value}",
        file=sys.stderr,
    )
    failure = trace.latest_dependency_error()
    if failure is not None:
        attributes = failure.attributes
        print(
            "依赖错误："
            f"iteration={failure.iteration} "
            f"stage={attributes.get('stage')} "
            f"type={attributes.get('error_type')} "
            f"message={attributes.get('error_message')}",
            file=sys.stderr,
        )
    return 1
