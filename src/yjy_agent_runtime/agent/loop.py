"""最小线性 Agent Loop。

一次 ``run()`` 的固定顺序（对应 AGENTS.md 第 5 节）：

    读/建 Session -> 追加 USER 消息
    -> [构建 Context -> LLM 决策 -> 执行工具并写回 -> 判断是否继续]
    -> 追加 ASSISTANT 消息 -> 返回 RunResult

三个关键约定：

1. 循环只有三种出口：拿到最终回答、依赖报错、达到最大轮次。每种出口都带明确的
   ``TerminationReason``，不存在“悄悄返回 None”。
2. 所有 Session 读写都显式带 ``request.session_id``。AgentLoop 自己不保存任何
   跨请求的对话状态，所以多个窗口/进程之间不会串线。
3. 依赖错误不向调用方抛出，而是转成 ``RunResult`` + trace 事件；由入口层决定
   退出码。这样 CLI、HTTP 等入口都能拿到结构化的终止原因。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from yjy_agent_runtime.domain import (
    FinalAnswerDecision,
    Message,
    MessageRole,
    RunRequest,
    RunResult,
    TerminationReason,
    ToolCallDecision,
    ToolResult,
    TraceEvent,
    TraceEventType,
)

from .ports import ContextProvider, LLMClient, SessionStore, ToolExecutor, TraceRecorder


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """Agent Loop 的安全边界配置。"""

    max_iterations: int = 8
    # Trace 中单条属性值的字符上限，避免把整段工具输出写进日志。
    max_trace_value_chars: int = 500

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_trace_value_chars < 1:
            raise ValueError("max_trace_value_chars must be at least 1")


class AgentLoop:
    """执行“构建 Context、模型决策、工具或回答”的显式循环。"""

    def __init__(
        self,
        *,
        context_provider: ContextProvider,
        llm_client: LLMClient,
        session_store: SessionStore,
        tool_executor: ToolExecutor,
        trace_recorder: TraceRecorder,
        config: LoopConfig | None = None,
    ) -> None:
        self._context_provider = context_provider
        self._llm_client = llm_client
        self._session_store = session_store
        self._tool_executor = tool_executor
        self._trace_recorder = trace_recorder
        self._config = config or LoopConfig()

    def run(self, request: RunRequest) -> RunResult:
        """运行到模型给出最终回答或达到最大循环轮次。"""

        run_id = str(uuid4())
        trace_id = str(uuid4())
        tool_results: list[ToolResult] = []
        # 1. 先落盘用户输入，再构建 Context。ContextBuilder 只从 Session 读，
        #    所以必须先写；否则本轮问题不会出现在模型输入里。
        self._session_store.get_or_create(request.session_id)
        self._session_store.append_message(
            request.session_id,
            Message(MessageRole.USER, request.user_input),
        )
        self._record_trace(
            TraceEventType.RUN_STARTED,
            session_id=request.session_id,
            run_id=run_id,
            trace_id=trace_id,
            iteration=0,
        )

        for iteration in range(1, self._config.max_iterations + 1):
            # 2. 每一轮都重新构建 Context：上一轮的工具结果已经写进 Session，
            #    模型必须看到它，才能决定“继续调工具”还是“给出最终回答”。
            #    session_before/after 只用于 Trace 观察压缩效果，不参与业务判断。
            session_before = self._session_store.get(request.session_id)
            try:
                messages = self._context_provider.build(request, tuple(tool_results))
            except Exception as error:  # noqa: BLE001
                return self._terminate_after_dependency_error(
                    request=request,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    reason=TerminationReason.CONTEXT_ERROR,
                    stage="context",
                    error=error,
                )
            session_after = self._session_store.get(request.session_id)
            summary = session_after.summary if session_after is not None else None
            self._record_trace(
                TraceEventType.ITERATION_STARTED,
                session_id=request.session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
                message_count=len(messages),
                prompt_chars=self._prompt_chars(messages),
                session_messages_before=(
                    len(session_before.messages) if session_before is not None else 0
                ),
                session_chars_before=(
                    self._prompt_chars(session_before.messages)
                    if session_before is not None
                    else 0
                ),
                summary_covered_message_count=(
                    summary.covered_message_count if summary is not None else 0
                ),
                summary_chars=len(summary.content) if summary is not None else 0,
            )

            llm_started = perf_counter()
            self._record_trace(
                TraceEventType.LLM_STARTED,
                session_id=request.session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
            )
            try:
                decision = self._llm_client.decide(messages)
            except Exception as error:  # noqa: BLE001
                return self._terminate_after_dependency_error(
                    request=request,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    reason=TerminationReason.LLM_ERROR,
                    stage="llm",
                    error=error,
                    duration_ms=self._duration_ms(llm_started),
                )
            self._record_trace(
                TraceEventType.LLM_COMPLETED,
                session_id=request.session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
                duration_ms=self._duration_ms(llm_started),
            )

            if isinstance(decision, FinalAnswerDecision):
                self._record_trace(
                    TraceEventType.DECISION_MADE,
                    session_id=request.session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    decision_type="final_answer",
                    decision_summary=decision.decision_summary,
                )
                # 3. 最终回答也写入 Session，下一次追问才能看到上一轮结论。
                self._session_store.append_message(
                    request.session_id,
                    Message(MessageRole.ASSISTANT, decision.answer),
                )
                result = RunResult(
                    session_id=request.session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    termination_reason=TerminationReason.FINAL_ANSWER,
                    iterations=iteration,
                    answer=decision.answer,
                )
                self._record_termination(result)
                return result

            if not isinstance(decision, ToolCallDecision):
                error = TypeError(
                    f"unsupported decision type: {type(decision).__name__}"
                )
                return self._terminate_after_dependency_error(
                    request=request,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    reason=TerminationReason.INVALID_DECISION,
                    stage="decision",
                    error=error,
                )

            self._record_trace(
                TraceEventType.DECISION_MADE,
                session_id=request.session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
                decision_type="tool_calls",
                tool_call_count=len(decision.tool_calls),
                decision_summary=decision.decision_summary,
            )
            for call in decision.tool_calls:
                tool_started = perf_counter()
                self._record_trace(
                    TraceEventType.TOOL_STARTED,
                    session_id=request.session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    call_id=call.call_id,
                    tool_name=call.name,
                    arguments=dict(call.arguments),
                )
                try:
                    tool_result = self._tool_executor.execute(
                        request.session_id,
                        call,
                    )
                except Exception as error:  # noqa: BLE001
                    return self._terminate_after_dependency_error(
                        request=request,
                        run_id=run_id,
                        trace_id=trace_id,
                        iteration=iteration,
                        reason=TerminationReason.TOOL_ERROR,
                        stage="tool",
                        error=error,
                        duration_ms=self._duration_ms(tool_started),
                    )
                tool_results.append(tool_result)
                # 4. 工具交互写两次，各自服务不同读者：
                #    - append_tool_interaction：结构化的 call/result，供审计与
                #      Context 组装“工具信封”；
                #    - append_message(TOOL)：纯文本结果，供模型对话历史使用。
                #    两次写之间若进程中断，会留下半条交互。这是当前版本的已知
                #    边界（见 docs/runtime-remediation-plan.md 的 P2-01），
                #    不影响单进程内的一次完整运行。
                self._session_store.append_tool_interaction(
                    request.session_id,
                    call,
                    tool_result,
                )
                self._session_store.append_message(
                    request.session_id,
                    Message(
                        MessageRole.TOOL,
                        tool_result.content,
                        tool_result.call_id,
                    ),
                )
                self._record_trace(
                    TraceEventType.TOOL_COMPLETED,
                    session_id=request.session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    call_id=call.call_id,
                    tool_name=call.name,
                    status=tool_result.status.value,
                    error_type=tool_result.error_type,
                    duration_ms=self._duration_ms(tool_started),
                    result_summary=self._summarize(tool_result.content),
                )

        # 5. 达到上限时没有答案。这里返回结构化原因而不是抛异常，
        #    CLI 会把 termination_reason 打印出来，便于区分“模型没收敛”
        #    和“网络/工具失败”。
        result = RunResult(
            session_id=request.session_id,
            run_id=run_id,
            trace_id=trace_id,
            termination_reason=TerminationReason.MAX_ITERATIONS,
            iterations=self._config.max_iterations,
        )
        self._record_termination(result)
        return result

    def _terminate_after_dependency_error(
        self,
        *,
        request: RunRequest,
        run_id: str,
        trace_id: str,
        iteration: int,
        reason: TerminationReason,
        stage: str,
        error: Exception,
        duration_ms: float | None = None,
    ) -> RunResult:
        """记录依赖失败的公开摘要，并返回结构化终止结果。"""

        attributes: dict[str, object] = {
            "stage": stage,
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        if duration_ms is not None:
            attributes["duration_ms"] = duration_ms
        self._record_trace(
            TraceEventType.DEPENDENCY_FAILED,
            session_id=request.session_id,
            run_id=run_id,
            trace_id=trace_id,
            iteration=iteration,
            **attributes,
        )
        result = RunResult(
            session_id=request.session_id,
            run_id=run_id,
            trace_id=trace_id,
            termination_reason=reason,
            iterations=iteration,
        )
        self._record_termination(result)
        return result

    def _record_termination(self, result: RunResult) -> None:
        """统一记录当前版本所有受控终止出口。"""

        self._record_trace(
            TraceEventType.RUN_TERMINATED,
            session_id=result.session_id,
            run_id=result.run_id,
            trace_id=result.trace_id,
            iteration=result.iterations,
            reason=result.termination_reason.value,
        )

    def _record_trace(
        self,
        event_type: TraceEventType,
        *,
        session_id: str,
        run_id: str,
        trace_id: str,
        iteration: int,
        **attributes: object,
    ) -> None:
        self._trace_recorder.record(
            TraceEvent(
                event_type=event_type,
                session_id=session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
                timestamp=datetime.now(UTC).isoformat(),
                attributes=attributes,
            )
        )

    @staticmethod
    def _duration_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)

    @staticmethod
    def _prompt_chars(messages: Sequence[Message]) -> int:
        """统计本轮发送给模型的内容字符数，用于观察 Context 预算。"""

        return sum(len(message.content) for message in messages)

    def _summarize(self, value: str) -> str:
        """限制 Trace 中保存的文本长度，保留明确截断标记。"""

        limit = self._config.max_trace_value_chars
        if len(value) <= limit:
            return value
        marker = "...[truncated]"
        if limit <= len(marker):
            return marker[:limit]
        return value[: limit - len(marker)] + marker
