"""具有严格 Session 隔离的进程内存储。"""

from threading import RLock

from yjy_agent_runtime.domain import (
    Message,
    Session,
    SessionSummary,
    ToolCall,
    ToolResult,
)


class SessionNotFoundError(LookupError):
    """请求更新一个尚未创建的 Session。"""


class InMemorySessionStore:
    """以不可变快照保存 Session，避免状态引用在 Session 间泄漏。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def get(self, session_id: str) -> Session | None:
        self._validate_session_id(session_id)
        with self._lock:
            return self._sessions.get(session_id)

    def get_or_create(self, session_id: str) -> Session:
        self._validate_session_id(session_id)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(session_id=session_id)
                self._sessions[session_id] = session
            return session

    def append_message(self, session_id: str, message: Message) -> Session:
        with self._lock:
            current = self._require(session_id)
            updated = Session(
                session_id=session_id,
                messages=(*current.messages, message),
                tool_calls=current.tool_calls,
                tool_results=current.tool_results,
                todos=current.todos,
                summary=current.summary,
            )
            self._sessions[session_id] = updated
            return updated

    def append_tool_interaction(
        self,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
    ) -> Session:
        if call.call_id != result.call_id or call.name != result.tool_name:
            raise ValueError(
                "tool call and result must refer to the same interaction"
            )
        with self._lock:
            current = self._require(session_id)
            updated = Session(
                session_id=session_id,
                messages=current.messages,
                tool_calls=(*current.tool_calls, call),
                tool_results=(*current.tool_results, result),
                todos=current.todos,
                summary=current.summary,
            )
            self._sessions[session_id] = updated
            return updated

    def set_summary(
        self,
        session_id: str,
        summary: SessionSummary | None,
    ) -> Session:
        with self._lock:
            current = self._require(session_id)
            updated = Session(
                session_id=session_id,
                messages=current.messages,
                tool_calls=current.tool_calls,
                tool_results=current.tool_results,
                todos=current.todos,
                summary=summary,
            )
            self._sessions[session_id] = updated
            return updated

    def append_todo(self, session_id: str, todo: str) -> Session:
        """向指定 Session 追加一个非空待办。"""

        if not todo:
            raise ValueError("todo must not be empty")
        with self._lock:
            current = self._require(session_id)
            updated = Session(
                session_id=session_id,
                messages=current.messages,
                tool_calls=current.tool_calls,
                tool_results=current.tool_results,
                todos=(*current.todos, todo),
                summary=current.summary,
            )
            self._sessions[session_id] = updated
            return updated

    def list_todos(self, session_id: str) -> tuple[str, ...]:
        """返回指定 Session 的待办快照。"""

        return self._require(session_id).todos

    def _require(self, session_id: str) -> Session:
        self._validate_session_id(session_id)
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise SessionNotFoundError(f"session not found: {session_id}") from error

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id:
            raise ValueError("session id must not be empty")
