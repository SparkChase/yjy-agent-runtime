"""CLI 入口层测试。

用假的 LLM 客户端替换真实网络调用，验证参数解析、Trace 路径决策、落盘与退出码。
真实付费 LLM 的端到端验收另有 integration 测试，不在默认门禁里。
"""

import io
from pathlib import Path

import pytest

from yjy_agent_runtime import interfaces
from yjy_agent_runtime.interfaces import cli


class StubLoop:
    """记录运行请求，返回固定成功结果。"""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.requests: list[object] = []

    def run(self, request: object) -> object:
        self.requests.append(request)
        from yjy_agent_runtime.domain import RunResult, TerminationReason

        return RunResult(
            session_id="session-a",
            run_id="run-a",
            trace_id="trace-a",
            termination_reason=TerminationReason.FINAL_ANSWER,
            iterations=1,
            answer="完成",
        )


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把工作目录切到临时目录，避免 CLI 自动读到仓库里的真实 .env。

    这是一道安全阀：即使开发者本地存在带真实密钥的 `.env`，单元测试也不会
    意外发起真实网络请求。需要配置的测试自行用 monkeypatch 注入假值。
    """

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")


def test_build_parser_defaults() -> None:
    args = cli.build_parser().parse_args(["你好"])

    assert args.message == "你好"
    assert args.session_id == "default"
    assert args.max_iterations == 8
    assert args.trace_file is None
    assert args.no_trace is False
    assert args.no_env_file is False
    assert args.interactive is False


def test_resolve_trace_path_explicit_file() -> None:
    args = cli.build_parser().parse_args(["--trace-file", "custom.jsonl", "hi"])

    assert cli.resolve_trace_path(args) == Path("custom.jsonl")


def test_resolve_trace_path_uses_env_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YJY_TRACE_DIR", "traces")
    args = cli.build_parser().parse_args(["--session-id", "window-1", "hi"])

    assert cli.resolve_trace_path(args) == Path("traces/window-1.jsonl")


def test_resolve_trace_path_defaults_to_logs_dir() -> None:
    args = cli.build_parser().parse_args(["--session-id", "window-1", "hi"])

    assert cli.resolve_trace_path(args) == Path("logs/window-1.jsonl")


def test_resolve_trace_path_disabled() -> None:
    args = cli.build_parser().parse_args(["--no-trace", "hi"])

    assert cli.resolve_trace_path(args) is None


def test_main_writes_trace_file_and_reports_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "build_loop", lambda **_kwargs: StubLoop())
    monkeypatch.setenv("YJY_SESSION_DIR", str(tmp_path / "sessions"))
    trace_file = tmp_path / "trace.jsonl"

    exit_code = cli.main(
        ["--trace-file", str(trace_file), "--session-id", "session-a", "你好"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "完成"
    assert f"trace: {trace_file}" in captured.err
    assert "耗时：" in captured.err
    assert trace_file.exists()


def test_main_can_disable_trace_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "build_loop", lambda **_kwargs: StubLoop())
    monkeypatch.setenv("YJY_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("YJY_TRACE_DIR", str(tmp_path / "traces"))

    exit_code = cli.main(["--no-trace", "你好"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "trace:" not in captured.err
    assert not (tmp_path / "traces").exists()


def test_main_interactive_reuses_session_until_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """交互模式应在同一进程内复用 Session，并支持 /new 切换。"""

    loop = StubLoop()
    monkeypatch.setattr(cli, "build_loop", lambda **_kwargs: loop)
    inputs = iter(["第一句", "第二句", "/new other", "第三句", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

    exit_code = cli.main(
        ["--interactive", "--no-trace", "--session-id", "chat"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert [request.session_id for request in loop.requests] == [
        "chat",
        "chat",
        "other",
    ]
    assert [request.user_input for request in loop.requests] == [
        "第一句",
        "第二句",
        "第三句",
    ]
    assert captured.out.count("助手> 完成") == 3
    assert "已切换到 session: other" in captured.err
    assert captured.err.count("耗时：") == 3


def test_format_timing_aggregates_llm_and_tool_durations() -> None:
    from yjy_agent_runtime.domain import TraceEvent, TraceEventType

    def event(
        event_type: TraceEventType,
        **attributes: object,
    ) -> TraceEvent:
        return TraceEvent(
            event_type=event_type,
            session_id="s",
            run_id="r",
            trace_id="t",
            iteration=1,
            timestamp="2026-10-27T00:00:00+00:00",
            attributes=attributes,
        )

    events = (
        event(TraceEventType.ITERATION_STARTED),
        event(TraceEventType.LLM_COMPLETED, duration_ms=1500.0),
        event(TraceEventType.TOOL_COMPLETED, duration_ms=20.0),
        event(TraceEventType.LLM_COMPLETED, duration_ms=500.0),
    )

    text = cli._format_timing(events, 2500.0)

    assert "总计 2.5s" in text
    assert "模型 2.0s（2 次）" in text
    assert "工具 0.02s（1 次）" in text
    assert "轮次 1" in text


def test_main_reports_missing_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 模拟 stdin 读到空行的情况，验证入口给出明确提示而不是继续调用模型。
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))

    exit_code = cli.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "非空消息" in captured.err


def test_main_fails_fast_without_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("YJY_SESSION_DIR", str(tmp_path / "sessions"))

    exit_code = cli.main(["--no-trace", "--no-env-file", "你好"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LLM_API_KEY" in captured.err


def test_main_never_calls_network_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """配置缺失必须在发起任何请求前失败，避免半截真实调用。"""

    def explode(**_kwargs: object) -> object:
        raise AssertionError("must not build a loop without configuration")

    monkeypatch.setattr(cli, "build_loop", explode)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    exit_code = cli.main(["--no-trace", "--no-env-file", "你好"])

    assert exit_code == 1
    assert "LLM_BASE_URL" in capsys.readouterr().err


def test_package_exposes_cli_main() -> None:
    """`yjy-agent-runtime` 脚本入口依赖包顶层暴露 main。"""

    import yjy_agent_runtime

    assert yjy_agent_runtime.main is cli.main
    assert interfaces.__name__ == "yjy_agent_runtime.interfaces"
