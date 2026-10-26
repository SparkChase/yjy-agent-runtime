"""最小线性 Agent Loop。"""

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

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")


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
            self._record_trace(
                TraceEventType.ITERATION_STARTED,
                session_id=request.session_id,
                run_id=run_id,
                trace_id=trace_id,
                iteration=iteration,
                message_count=len(messages),
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
                )
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
                )

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
