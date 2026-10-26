"""最小线性 Agent Loop。

循环只负责编排 Context、LLM 决策和工具执行。模型 SDK、参数校验、Session
存储与 Trace 的具体实现均通过应用端口注入。
"""

from dataclasses import dataclass
from uuid import uuid4

from yjy_agent_runtime.domain import (
    FinalAnswerDecision,
    RunRequest,
    RunResult,
    TerminationReason,
    ToolCallDecision,
    ToolResult,
)

from .ports import ContextProvider, LLMClient, ToolExecutor, TraceRecorder


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
        tool_executor: ToolExecutor,
        trace_recorder: TraceRecorder,
        config: LoopConfig | None = None,
    ) -> None:
        self._context_provider = context_provider
        self._llm_client = llm_client
        self._tool_executor = tool_executor
        self._trace_recorder = trace_recorder
        self._config = config or LoopConfig()

    def run(self, request: RunRequest) -> RunResult:
        """运行到模型给出最终回答或达到最大循环轮次。

        ToolExecutor 应把可恢复的工具失败表达为 ToolResult，使模型可以在下一
        轮调整决策。基础设施端口直接抛出的异常不会在这里被伪装成成功结果。
        """

        trace_id = str(uuid4())
        tool_results: list[ToolResult] = []
        self._trace_recorder.record(
            "run_started",
            session_id=request.session_id,
            trace_id=trace_id,
        )

        for iteration in range(1, self._config.max_iterations + 1):
            messages = self._context_provider.build(request, tuple(tool_results))
            self._trace_recorder.record(
                "iteration_started",
                session_id=request.session_id,
                trace_id=trace_id,
                iteration=iteration,
                message_count=len(messages),
            )
            decision = self._llm_client.decide(messages)

            if isinstance(decision, FinalAnswerDecision):
                result = RunResult(
                    session_id=request.session_id,
                    trace_id=trace_id,
                    termination_reason=TerminationReason.FINAL_ANSWER,
                    iterations=iteration,
                    answer=decision.answer,
                )
                self._record_termination(result)
                return result

            if not isinstance(decision, ToolCallDecision):
                raise TypeError(
                    f"unsupported decision type: {type(decision).__name__}"
                )

            for call in decision.tool_calls:
                tool_results.append(
                    self._tool_executor.execute(request.session_id, call)
                )

        result = RunResult(
            session_id=request.session_id,
            trace_id=trace_id,
            termination_reason=TerminationReason.MAX_ITERATIONS,
            iterations=self._config.max_iterations,
        )
        self._record_termination(result)
        return result

    def _record_termination(self, result: RunResult) -> None:
        """统一记录当前版本所有受控终止出口。"""

        self._trace_recorder.record(
            "run_terminated",
            session_id=result.session_id,
            trace_id=result.trace_id,
            iteration=result.iterations,
            reason=result.termination_reason.value,
        )
