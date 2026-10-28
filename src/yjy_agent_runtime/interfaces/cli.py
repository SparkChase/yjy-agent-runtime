"""YJY Agent Runtime 的最小命令行入口。

默认一次 CLI 进程只处理一条用户消息，内部可能循环多轮工具调用，最后输出最终回答。
加上 ``--interactive`` 后，进程不退出，循环读取多轮输入，并复用同一个 Session。
入口层只做三件事：解析参数、装配具体实现、打印结果；Agent 的执行逻辑全部在
`agent.AgentLoop` 里。

会话连续性靠 ``--session-id`` + ``YJY_SESSION_DIR``，不靠进程常驻：
同一个 ID 的多次 CLI 调用会读写同一个 JSON 快照，不同 ID 互不可见。

退出码约定（脚本和 CI 依赖它）：
    0  拿到最终回答
    1  运行了但没拿到答案（llm_error / context_error / tool_error /
       max_iterations / invalid_decision），stderr 会带最近一次依赖错误
    2  参数或配置有问题，根本没有发起运行

``--interactive`` 沿用同一套约定：只要有一轮没拿到最终答案就返回 1，
不因为“进程最后是正常退出的”而报成功。
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

from yjy_agent_runtime.agent import AgentLoop, LoopConfig
from yjy_agent_runtime.domain import RunRequest, RunResult, TraceEvent
from yjy_agent_runtime.infrastructure.context import ContextBuilder
from yjy_agent_runtime.infrastructure.llm import (
    LLMConfigurationError,
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

# 终端把非法 UTF-8 字节按 surrogateescape 解码时会产生 \udcXX。
# 这种字符无法再编码成 UTF-8，必须在进入 Runtime 前拦住。
SURROGATE_INPUT_MESSAGE = (
    "输入包含无法解码的字符（终端编码可能不是 UTF-8）。"
    "请检查终端/输入法编码后重试。"
)


def _contains_surrogate(text: str) -> bool:
    """判断输入是否含 lone surrogate（\ud800-\udfff）。"""

    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def build_parser() -> argparse.ArgumentParser:
    """构建可测试的命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="运行一次 YJY Agent Runtime 对话")
    parser.add_argument("message", nargs="?", help="用户输入；省略时从 stdin 读取")
    parser.add_argument("--session-id", default="default", help="Session 标识")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="进入交互式多轮对话；同一进程内复用 --session-id 指定的 Session",
    )
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

    def latest_dependency_error(self, since: int = 0):
        """返回 ``since`` 之后最近一次依赖失败事件，供失败诊断使用。

        交互模式下每轮都会复用同一个 TraceSink，必须只看本轮新增的事件，
        否则会把上一轮的错误误报成这一轮的错误。
        """

        return next(
            (
                event
                for event in reversed(self.memory.events[since:])
                if event.event_type.value == "dependency_failed"
            ),
            None,
        )

# 组装根
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

    # 参数校验必须在装配任何依赖之前完成：这类问题没有发起运行，退出码是 2。
    # 放在这里而不是交给 LoopConfig，是因为 LoopConfig 在 build_loop 里构造，
    # 那里抛出的 ValueError 会被下面的运行失败分支吞成 1。
    if args.max_iterations < 1:
        print("--max-iterations 必须是大于等于 1 的整数。", file=sys.stderr)
        return 2

    # 一次性模式在装配任何依赖之前就校验输入，坏字符不会走到模型或 Session。
    # 交互模式不在这里读 stdin，交给 _run_interactive 逐轮处理。
    user_input: str | None = None
    if not args.interactive:
        user_input = (args.message or sys.stdin.readline()).strip()
        if not user_input:
            print("请输入非空消息。", file=sys.stderr)
            return 2
        if _contains_surrogate(user_input):
            print(SURROGATE_INPUT_MESSAGE, file=sys.stderr)
            return 2

    # 配置阶段：只加载 .env 和读取 LLM 配置，不触碰网络与 Session。
    # 这里任何失败都意味着“没有发起运行”，按退出码约定返回 2；
    # 只捕获配置类异常（含 .env 读写与解码失败），其余异常照常抛出，
    # 避免把代码缺陷伪装成配置问题。
    try:
        prepare_environment(
            env_file=args.env_file,
            use_env_file=not args.no_env_file,
        )
        config = OpenAICompatibleConfig.from_environment()
    except (EnvFileError, LLMConfigurationError, OSError, UnicodeDecodeError) as error:
        print(f"配置失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    trace = TraceSink(resolve_trace_path(args))
    try:
        llm = OpenAICompatibleLLMClient(config)
        try:
            loop = build_loop(
                llm=llm,
                trace=trace,
                max_iterations=args.max_iterations,
            )
            if args.interactive:
                # 交互模式自己读取多轮输入，不消费一次性模式的那一行 stdin。
                return _run_interactive(
                    loop,
                    args.session_id,
                    trace,
                    initial=args.message,
                )
            assert user_input is not None  # 非交互分支已在前面完成校验
            run_started = perf_counter()
            result = loop.run(RunRequest(args.session_id, user_input))
            elapsed_ms = (perf_counter() - run_started) * 1000
        finally:
            llm.close()
            trace.close()
    except Exception as error:  # noqa: BLE001
        _report_trace_path(trace.path)
        print(f"运行失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    _report_trace_path(trace.path)
    return _report_result(result, trace, elapsed_ms)


def _run_interactive(
    loop: AgentLoop,
    session_id: str,
    trace: TraceSink,
    *,
    initial: str | None = None,
) -> int:
    """在同一进程里连续多轮对话，复用同一个 Session。

    与一次性模式的区别只有两点：

    1. 进程不退出，循环读取用户输入；
    2. 每一轮都复用同一个 ``session_id``，所以历史会自然累积。

    这里不做流式输出，也不改变模型调用次数：每条消息仍然要等模型返回。
    交互模式解决的是“反复启动进程、反复输入 session id”的麻烦，不是延迟。

    退出码与一次性模式保持一致：只要有一轮没拿到最终答案（异常或非
    final_answer 终止），整个进程就返回 1。否则脚本化调用交互模式时，
    一次 llm_error 会被误判成成功。用户自己 Ctrl-C 中断的那一轮不算失败。
    """

    print(
        f"session: {session_id}"
        "（/exit 退出，/new <id> 切换会话，/help 查看命令）",
        file=sys.stderr,
    )
    _report_trace_path(trace.path)

    failed = False
    pending: list[str] = []
    if initial:
        if _contains_surrogate(initial):
            print(SURROGATE_INPUT_MESSAGE, file=sys.stderr)
        else:
            pending.append(initial)

    while True:
        if pending:
            text = pending.pop(0)
            print(f"你> {text}")
        else:
            try:
                text = input("你> ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break

        text = text.strip()
        if not text:
            continue
        if _contains_surrogate(text):
            print(SURROGATE_INPUT_MESSAGE, file=sys.stderr)
            continue
        if text in {"/exit", "/quit"}:
            break
        if text == "/help":
            print(
                "命令：/exit 退出；/new <session-id> 切换会话；"
                "其它输入作为用户消息。",
                file=sys.stderr,
            )
            continue
        if text.startswith("/new"):
            new_session_id = text[len("/new") :].strip()
            if not new_session_id:
                print("用法：/new <session-id>", file=sys.stderr)
                continue
            session_id = new_session_id
            print(f"已切换到 session: {session_id}", file=sys.stderr)
            continue

        # 只检查本轮新增的 Trace 事件，避免把上一轮的错误误报给这一轮。
        started = len(trace.memory.events)
        turn_started = perf_counter()
        try:
            result = loop.run(RunRequest(session_id, text))
        except KeyboardInterrupt:
            # 模型调用可能持续十几秒，允许用户中断本轮而不退出整个对话。
            elapsed_ms = (perf_counter() - turn_started) * 1000
            print("\n已中断本轮。", file=sys.stderr)
            _report_timing(trace, elapsed_ms, since=started)
            continue
        except Exception as error:  # noqa: BLE001
            elapsed_ms = (perf_counter() - turn_started) * 1000
            failed = True
            print(f"本轮失败：{type(error).__name__}: {error}", file=sys.stderr)
            _report_timing(trace, elapsed_ms, since=started)
            continue

        elapsed_ms = (perf_counter() - turn_started) * 1000
        if result.answer is not None:
            print(f"助手> {result.answer}")
            _report_timing(trace, elapsed_ms, since=started)
            continue

        failed = True
        print(
            f"运行未返回最终答案，终止原因：{result.termination_reason.value}",
            file=sys.stderr,
        )
        failure = trace.latest_dependency_error(since=started)
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
        _report_timing(trace, elapsed_ms, since=started)

    print("已退出。", file=sys.stderr)
    return 1 if failed else 0


def _report_trace_path(path: Path | None) -> None:
    """把 Trace 位置写到 stderr，成功与失败都要告知用户。"""

    if path is not None:
        print(f"trace: {path}", file=sys.stderr)


def _format_timing(events: Sequence[TraceEvent], elapsed_ms: float) -> str:
    """把一次运行的耗时聚合成一行，便于直接在终端判断慢在哪。

    ``llm_completed`` / ``tool_completed`` 事件本身已经带 ``duration_ms``，
    这里只做求和，不重新计时；``elapsed_ms`` 是调用方用墙钟测的总耗时，
    因此它 >= 模型 + 工具，差额是 Context 组装、Session 读写等开销。
    """

    llm_ms = 0.0
    tool_ms = 0.0
    llm_calls = 0
    tool_calls = 0
    iterations = 0
    for event in events:
        event_type = event.event_type.value
        if event_type == "iteration_started":
            iterations += 1
        elif event_type == "llm_completed":
            llm_ms += _duration_ms(event)
            llm_calls += 1
        elif event_type == "tool_completed":
            tool_ms += _duration_ms(event)
            tool_calls += 1
    return (
        f"耗时：总计 {elapsed_ms / 1000:.1f}s｜"
        f"模型 {llm_ms / 1000:.1f}s（{llm_calls} 次）｜"
        f"工具 {tool_ms / 1000:.2f}s（{tool_calls} 次）｜"
        f"轮次 {iterations}"
    )


def _duration_ms(event: TraceEvent) -> float:
    value = event.attributes.get("duration_ms", 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _report_timing(trace: TraceSink, elapsed_ms: float, since: int = 0) -> None:
    """只统计 ``since`` 之后的本轮事件，避免交互模式把上一轮算进来。"""

    print(
        _format_timing(trace.memory.events[since:], elapsed_ms),
        file=sys.stderr,
    )


def _report_result(result: RunResult, trace: TraceSink, elapsed_ms: float) -> int:
    """按运行结果决定退出码，并在失败时附带最近依赖错误。"""

    if result.answer is not None:
        print(result.answer)
        _report_timing(trace, elapsed_ms)
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
    _report_timing(trace, elapsed_ms)
    return 1
