"""最小线性 Agent Loop。

一次 ``run()`` 的固定顺序（对应 AGENTS.md 第 5 节）：

    读/建 Session -> 追加 USER 消息
    -> [构建 Context -> LLM 决策 -> 执行工具并写回 -> 判断是否继续]
    -> 追加 ASSISTANT 消息 -> 返回 RunResult

三个关键约定：

1. 循环只有三种出口：拿到最终回答、依赖报错、达到最大轮次。每种出口都带明确的
   ``TerminationReason``，不存在"悄悄返回 None"。
2. 所有 Session 读写都显式带 ``request.session_id``。AgentLoop 自己不保存任何
   跨请求的对话状态，所以多个窗口/进程之间不会串线。
3. 依赖错误不向调用方抛出，而是转成 ``RunResult`` + trace 事件；由入口层决定
   退出码。这样 CLI、HTTP 等入口都能拿到结构化的终止原因。

数据流：谁写、谁读、写几次
--------------------------

整个循环里，**Session 是唯一的事实来源**，其他一切都是它的投影：

    用户输入 ──写①──> Session.messages
                          │
                          ├─读─> ContextBuilder ──> 发给模型的消息
                          │                            │
                          │                        模型决策
                          │                            │
                          └──写②──> Session.messages / tool_calls / tool_results
                                     （工具交互，三者一次原子写）
                          │
                          └──写③──> Session.messages（最终回答）

一次 ``run()`` 最多落盘三次：

- **写①** 用户消息：在进入循环**之前**完成。因为 ContextBuilder 只从 Session 读，
  不先写就会漏掉本轮问题。
- **写②** 工具交互：每执行完一个工具立刻写，调用、结果、TOOL 消息三者同一次提交。
  下一轮构建 Context 时才能看到它，从而决定"继续调工具"还是"给出回答"。
- **写③** 最终回答：拿到 ``FinalAnswerDecision`` 时写。不写的话，下一次追问就
  看不到上一轮的结论。

循环体本身不持有任何对话状态：每一轮都从 Session 重新读、重新拼 Context。
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
    """Agent Loop 的安全边界配置。

    这两个数字都不是"调优"出来的，而是"明显安全"的边界：

    - ``max_iterations``：一轮工具调用 = 1 次模型调用 + N 次工具。真实任务
      （算数、查文档、记待办）通常 1-3 轮收敛，20 是很宽松的上限——宁可多给
      几轮，也不让正常任务被截断。代价是模型不收敛时成本更高，所以必须配合
      Trace 的 ``iterations`` 观察实际用了多少轮，再决定要不要收紧。
    - ``max_trace_value_chars``：Trace 是给人 grep 的，不是给模型读的。500 字符
      约一段话，够定位问题；不设上限则一条工具输出就能把 JSONL 撑到几 MB。
    """

    max_iterations: int = 20
    # Trace 中单条属性值的字符上限，避免把整段工具输出写进日志。
    max_trace_value_chars: int = 500

    def __post_init__(self) -> None:
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if self.max_trace_value_chars < 1:
            raise ValueError("max_trace_value_chars must be at least 1")


class AgentLoop:
    """执行"构建 Context、模型决策、工具或回答"的显式循环。

    它只依赖 5 个 ``Protocol``（见 ``agent/ports.py``），不认识任何具体实现：
    换模型、换存储、换工具执行器、换 Trace 后端，都不需要改这个文件。
    具体装配发生在入口层的 ``build_loop()``（组装根）。


    workflows vs skills

    react ～ TAO

    superpower trellis

    cordis

    hermes 自进化
    模块 verify

    局部最优

    reward hack




    """

    def __init__( # T - A - O    jev
        self,
        *,
        context_provider: ContextProvider,
        llm_client: LLMClient,
        session_store: SessionStore,
        tool_executor: ToolExecutor,
        trace_recorder: TraceRecorder,
        config: LoopConfig | None = None,
    ) -> None:
        # 五个依赖各管一件事，互不知道对方存在：
        #   context_provider —— 决定"这一轮发什么给模型"（读 Session）
        #   llm_client       —— 把消息变成决策（不碰 Session）
        #   session_store    —— 唯一的持久化入口（写 Session）
        #   tool_executor    —— 校验并执行一次调用（不碰 Session）
        #   trace_recorder   —— 记录发生了什么（只追加）
        self._context_provider = context_provider
        self._llm_client = llm_client
        self._session_store = session_store
        self._tool_executor = tool_executor
        self._trace_recorder = trace_recorder
        self._config = config or LoopConfig()

    def run(self, request: RunRequest) -> RunResult:
        """运行到模型给出最终回答或达到最大循环轮次。

        参数 ``request`` 只带两样东西：``session_id`` 和 ``user_input``。
        循环需要的其它信息（历史、工具结果）全部从 Session 读——**没有隐式状态**。
        """

        # ═══════════════════════════════════════════════════════════════
        # 阶段 0：准备本次运行的标识
        # ═══════════════════════════════════════════════════════════════
        # run_id 标识"这一次 run()"，trace_id 留给将来接入跨服务追踪。
        # 现在两者一一对应，但分开存：一次 trace 将来可能包含多次 run（如重试）。
        run_id = str(uuid4())
        trace_id = str(uuid4())
        # 本轮已执行的工具结果，按执行顺序累积。它只是一份便于传递的视图：
        # 结果本身已经写进 Session，这里传一份给 ContextBuilder，免得它为拿
        # "刚执行的结果"再扫一遍全量历史。
        tool_results: list[ToolResult] = []

        # ═══════════════════════════════════════════════════════════════
        # 阶段 1：把用户输入写进 Session（落盘点 ①）
        # ═══════════════════════════════════════════════════════════════
        # 顺序很重要：先落盘，再构建 Context。
        # ContextBuilder 只从 Session 读，所以必须先写；否则本轮问题不会出现在
        # 模型输入里——症状是"第一轮永远答非所问"，而且不检查"模型收到了什么"
        # 的测试根本发现不了。
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

        # ═══════════════════════════════════════════════════════════════
        # 阶段 2~5：循环体。每一轮都完整走一遍，不缓存任何中间结果
        # ═══════════════════════════════════════════════════════════════
        for iteration in range(1, self._config.max_iterations + 1):
            # ───────────────────────────────────────────────────────────
            # 阶段 2：构建本轮 Context
            # ───────────────────────────────────────────────────────────
            # 每轮都重新构建，不复用上一轮的 messages。因为上一轮的工具结果刚写进
            # Session，模型必须看到它才能决定"继续调工具"还是"给出回答"。
            # 缓存 Context 是这类系统最常见的错误来源。
            #
            # session_before / session_after 只用于 Trace 观察压缩效果
            # （压缩前多少条、压缩后多少条），**不参与任何业务判断**。
            session_before = self._session_store.get(request.session_id)
            try:
                messages = self._context_provider.build(request, tuple(tool_results))
            except Exception as error:  # noqa: BLE001
                # Context 构建失败（例如 system prompt 本身就超预算）属于依赖失败，
                # 转成结构化的终止原因，不向上抛。
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

            # ───────────────────────────────────────────────────────────
            # 阶段 3：请求模型决策
            # ───────────────────────────────────────────────────────────
            # 这里只做一件事：把 messages 交给 LLMClient，拿回一个 AgentDecision。
            # 解析、校验、工具调度都不在这里——适配器只负责"消息进、决策出"。
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
                # 网络错误、HTTP 错误、JSON 解析失败都在这里收口，统一转成
                # LLM_ERROR。调用方拿到的是 termination_reason，不是异常栈。
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

            # ───────────────────────────────────────────────────────────
            # 阶段 4：分派决策。只有两条合法分支，其余一律视为非法
            # ───────────────────────────────────────────────────────────
            if isinstance(decision, FinalAnswerDecision):
                # 分支 A：模型认为信息够了 → 这是出口一
                self._record_trace(
                    TraceEventType.DECISION_MADE,
                    session_id=request.session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    iteration=iteration,
                    decision_type="final_answer",
                    decision_summary=decision.decision_summary,
                )
                # 落盘点 ③：最终回答也要写进 Session。
                # 不写的话，用户下一次追问时模型看不到上一轮的结论，会重新问一遍。
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
                # 防御：AgentDecision 是封闭联合，理论上到不了这里。
                # 但如果某个 LLMClient 实现返回了别的对象，必须明确失败，
                # 而不是让后面的属性访问抛一个看不懂的 AttributeError。
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

            # 分支 B：模型要调工具 → 执行完继续下一轮
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

            # ───────────────────────────────────────────────────────────
            # 阶段 5：逐个执行工具并写回（落盘点 ②）
            # ───────────────────────────────────────────────────────────
            # 串行执行，不是并发。并发会引入"部分成功"的中间态：一个成功一个超时，
            # 落盘顺序和 call_id 顺序不一致，invariants.py 的校验会更复杂。
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
                    # 执行器内部完成四道关：注册表 → JSON Schema → 按副作用分流执行
                    # → 结果校验。工具自身的失败**不抛异常**，而是变成一个
                    # status=ERROR 的 ToolResult 返回——模型需要看到"为什么失败"，
                    # 才能决定换参数还是换方案。
                    tool_result = self._tool_executor.execute(
                        request.session_id,
                        call,
                    )
                except Exception as error:  # noqa: BLE001
                    # 走到这里说明执行器自身出了故障（例如 session_id 为空），
                    # 而不是工具业务失败。这属于依赖失败，终止整轮。
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
                # 关键：一次工具交互必须在同一次写入里落盘——调用、结果和 TOOL
                # 消息三者是同一件事。分成两次写，进程在中间崩溃就会留下"有结果
                # 没消息"的半条交互，模型看到的历史会自相矛盾。
                #
                # 注意这里传进去的是**四样**：session_id、call、result、message。
                # 落盘实现会校验它们互相指认（invariants.validate_tool_interaction），
                # 并用一次原子替换写入。
                self._session_store.append_tool_interaction(
                    request.session_id,
                    call,
                    tool_result,
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
                    timeout_semantics=(
                        tool_result.timeout_semantics.value
                        if tool_result.timeout_semantics is not None
                        else None
                    ),
                    duration_ms=self._duration_ms(tool_started),
                    result_summary=self._summarize(tool_result.content),
                )
            # 循环体结束 → 回到阶段 2，重新从 Session 读、重新构建 Context。
            # 此时上一轮的工具结果已经在 Session 里，模型能看到它。

        # ═══════════════════════════════════════════════════════════════
        # 阶段 6：达到上限。出口三
        # ═══════════════════════════════════════════════════════════════
        # 没有答案就是没有答案。这里返回结构化原因而不是抛异常，
        # CLI 会把 termination_reason 打印出来，便于区分"模型没收敛"
        # 和"网络/工具失败"。
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
        """记录依赖失败的公开摘要，并返回结构化终止结果。

        三个依赖错误出口（context / llm / tool）都走这里，保证：

        - Trace 里一定有一条 ``dependency_failed``，带 stage 和错误类型；
        - 返回值一定是 RunResult，绝不把异常抛给入口层；
        - ``answer`` 一定是 None（RunResult 的构造期校验会强制这一点）。
        """

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
        """统一记录当前版本所有受控终止出口。

        所有 return 之前都调它一次，所以 Trace 的最后一条永远是 run_terminated。
        少了这条，日志看起来就像"进程被杀了"。
        """

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
        """构造并追加一条 Trace 事件。

        时间戳在这里统一生成，调用方不必关心；attributes 是每种事件自己的字段。
        注意：这里只记公开事实（参数、结果摘要、耗时、错误类型），
        **不记模型私有思维链**。
        """

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
        """把 perf_counter 的差值换算成毫秒，保留三位小数。"""

        return round((perf_counter() - started_at) * 1000, 3)

    @staticmethod
    def _prompt_chars(messages: Sequence[Message]) -> int:
        """统计本轮发送给模型的内容字符数，用于观察 Context 预算。

        用字符而不是 token：精确 token 要接各家 tokenizer，字符数是保守近似，
        但永远可算、不依赖外部服务。
        """

        return sum(len(message.content) for message in messages)

    def _summarize(self, value: str) -> str:
        """限制 Trace 中保存的文本长度，保留明确截断标记。

        截断必须留下 marker。否则读日志的人会以为"工具就返回了这么点内容"，
        而不是"被截断了"。
        """

        limit = self._config.max_trace_value_chars
        if len(value) <= limit:
            return value
        marker = "...[truncated]"
        if limit <= len(marker):
            return marker[:limit]
        return value[: limit - len(marker)] + marker
