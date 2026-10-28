"""Session 中工具交互的一致性约束。

一次工具交互由三部分组成：

    ToolCall  +  ToolResult  +  带 tool_call_id 的 TOOL 消息

三者必须**同时**写入、彼此对应。分成两次写，进程在中间崩溃就会留下半条交互：
要么有调用和结果却没有工具消息，要么有工具消息却找不到对应结果。模型看到残缺
历史会误判下一步，审计也无法还原当时到底发生了什么。

本模块把"什么算一致"收敛成几个纯函数，内存 Store、文件 Store 和读取校验共用
同一套规则，避免两处实现各自漂移。
"""

from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    Session,
    ToolCall,
    ToolResult,
)

# 这算是第一道闸门，就是防止、拒绝追加单独的一个Tool。
def reject_standalone_tool_message(message: Message) -> None:
    """拒绝单独追加的 TOOL 消息。

    TOOL 消息必须通过 ``append_tool_interaction`` 与调用、结果一起落盘；单独写
    会让"半条交互"重新变得可表示。
    """

    if message.role is MessageRole.TOOL:
        raise ValueError(
            "tool messages must be appended together with their tool interaction"
        )

# 这是第二道闸门，就是防止挡住调用消息和结果，也就是不是同一次交互。
def validate_tool_interaction(
    call: ToolCall,
    result: ToolResult,
    message: Message,
) -> None:
    """校验调用、结果和工具消息确实描述同一次交互。"""

    if call.call_id != result.call_id or call.name != result.tool_name:
        raise ValueError("tool call and result must refer to the same interaction")
    if message.role is not MessageRole.TOOL:
        raise ValueError("tool interaction message must use the tool role")
    if message.tool_call_id != call.call_id:
        raise ValueError("tool message must reference the same call id")
    if message.content != result.content:
        raise ValueError("tool message content must match the tool result")


def ensure_unique_call_id(session: Session, call: ToolCall) -> None:
    """拒绝重复的 ``call_id``，避免同一调用在历史里出现两份结果。"""

    if any(existing.call_id == call.call_id for existing in session.tool_calls):
        raise ValueError(f"tool call id already exists in session: {call.call_id}")

# 第三道闸门：挡住读回来的旧快照本身是残缺的
def assert_tool_interactions_consistent(session: Session) -> None:
    """检查快照里的工具交互是否完整：调用 ID 唯一，消息必须能对上调用与结果。"""

    call_ids = [call.call_id for call in session.tool_calls]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("session contains duplicate tool call ids")

    result_ids = [result.call_id for result in session.tool_results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("session contains duplicate tool result ids")

    calls = {call.call_id: call for call in session.tool_calls}
    results = {result.call_id: result for result in session.tool_results}
    for result in session.tool_results:
        call = calls.get(result.call_id)
        if call is None:
            raise ValueError(f"tool result without matching call: {result.call_id}")
        if call.name != result.tool_name:
            raise ValueError(
                f"tool result name does not match its call: {result.call_id}"
            )

    for message in session.messages:
        if message.role is not MessageRole.TOOL:
            continue
        call_id = message.tool_call_id or ""
        if call_id not in calls or call_id not in results:
            raise ValueError(f"tool message without matching interaction: {call_id}")
