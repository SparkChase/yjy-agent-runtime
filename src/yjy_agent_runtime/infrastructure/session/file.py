"""可恢复的 JSON 文件 SessionStore。"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    Session,
    SessionSummary,
    TimeoutSemantics,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)

from .in_memory import SessionNotFoundError
from .invariants import (
    assert_tool_interactions_consistent,
    ensure_unique_call_id,
    reject_standalone_tool_message,
    validate_tool_interaction,
)


class SessionStorageError(RuntimeError):
    """Session 快照不存在、损坏或无法写入。"""


class JsonFileSessionStore:
    """将每个 Session 保存为独立 JSON 快照，并原子替换文件。"""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def get(self, session_id: str) -> Session | None: # 一次读取
        self._validate_session_id(session_id)
        path = self._path_for(session_id)
        if not path.exists():
            return None
        with self._lock:
            try:
                payload = json.loads(path.read_text(encoding="utf-8")) # 这个意思就是说，字符串变成dict。
                session = self._decode_session(payload, expected_id=session_id) # 这个是Dict变成Section，也就是逐条重建Message、To a Call和To a Result。
            except SessionStorageError:
                raise
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise SessionStorageError(
                    f"session snapshot is corrupted: {session_id}"
                ) from error
            # 读取时再做一次一致性检查：旧版本可能留下过“半条工具交互”的快照，
            # 这种历史一旦进入 Context，模型会看到有结果没调用、或有调用没结果。
            try:
                assert_tool_interactions_consistent(session) # 闸门3
            except ValueError as error: # 如果闸门三失败，就会包装成这个SessionStorageError，然后不会让一条残缺的历史进入我们的context。
                raise SessionStorageError(
                    f"session snapshot has an incomplete tool interaction: {session_id}"
                ) from error
            return session

    def get_or_create(self, session_id: str) -> Session:
        current = self.get(session_id)
        if current is not None:
            return current
        session = Session(session_id=session_id)
        self._save(session)
        return session

    def append_message(self, session_id: str, message: Message) -> Session:
        reject_standalone_tool_message(message) # 第一道闸门
        current = self._require(session_id)
        updated = Session(
            session_id=session_id,
            messages=(*current.messages, message),
            tool_calls=current.tool_calls,
            tool_results=current.tool_results,
            todos=current.todos,
            summary=current.summary,
        )
        self._save(updated)
        return updated

    def append_tool_interaction(
        self,
        session_id: str,
        call: ToolCall,
        result: ToolResult,
        message: Message,
    ) -> Session:
        """用一次原子替换写入调用、结果和工具消息。

        三者进入同一个 ``Session`` 快照，再交给 ``_save`` 做"临时文件 + fsync +
        os.replace"。进程在任意时刻崩溃，磁盘上要么是写入前的完整快照，要么是
        写入后的完整快照，不会出现只有其中一部分的半条交互。
        """

        validate_tool_interaction(call, result, message) # 这个相当于是一道闸门，保证我们的call、result和message进行相互指认。
        current = self._require(session_id) # 这个函数的功能就是去读当前的快照。
        ensure_unique_call_id(current, call) # 这就是一个校验：我们的call id不能重复。
        updated = Session( # 这一步就是构造一个新的session：三个位置都各追加一项，其余原样带过来。
            session_id=session_id,
            messages=(*current.messages, message), # 追加
            tool_calls=(*current.tool_calls, call), # 追加
            tool_results=(*current.tool_results, result), # 追加
            todos=current.todos,
            summary=current.summary,
        ) # 其实，这个解包current.x y就是在同一个表达式里面，这就是原子在代码层面的一个样子了。就不存在先追加一、再追加二这样的中间状态，因为整个section是一次性构造出来的新对象，因为frozen等于True。
        # 这个地方还可以另外说一下：为什么不直接用append，而是这样来做？首先就是因为Tuple它是不可变的，然后没有这个append方法。还有就是因为这个就是函数式编程里面的不可变数据的一个设计的方法。就是说原对象永远都不动，每次有新消息都复制上一份，生成一个全新的快照。
        self._save(updated) # 然后这个步骤的话，就是写临时文件嘛。
        return updated

    def set_summary(self, session_id: str, summary: SessionSummary | None) -> Session:
        current = self._require(session_id)
        updated = Session(
            session_id=session_id,
            messages=current.messages,
            tool_calls=current.tool_calls,
            tool_results=current.tool_results,
            todos=current.todos,
            summary=summary,
        )
        self._save(updated)
        return updated

    def append_todo(self, session_id: str, todo: str) -> Session:
        if not todo:
            raise ValueError("todo must not be empty")
        current = self._require(session_id)
        updated = Session(
            session_id=session_id,
            messages=current.messages,
            tool_calls=current.tool_calls,
            tool_results=current.tool_results,
            todos=(*current.todos, todo),
            summary=current.summary,
        )
        self._save(updated)
        return updated

    def list_todos(self, session_id: str) -> tuple[str, ...]:
        return self._require(session_id).todos

    def _require(self, session_id: str) -> Session:
        self._validate_session_id(session_id)
        session = self.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return session

    def _save(self, session: Session) -> None:
        # 先写同目录临时文件、fsync，再 os.replace 原子替换：
        # 进程在任何时刻崩溃，磁盘上要么是旧快照，要么是新快照，不会出现半个 JSON。
        payload = self._encode_session(session)
        path = self._path_for(session.session_id)
        with self._lock:
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self._directory,
                    prefix=".session-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
                    temporary.write("\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = temporary.name
                os.replace(temporary_path, path) # 这个是一个原子替换。
            except OSError as error:
                if temporary_path:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass
                raise SessionStorageError(
                    f"unable to save session: {session.session_id}"
                ) from error

    def _path_for(self, session_id: str) -> Path:
        # 文件名用 sha256，而不是直接拼 session_id：
        # session_id 来自命令行，可能包含 "/"、".." 等字符，直接拼会越出目录
        # 或产生非法文件名。哈希后既安全，又能保持“一个 Session 一个文件”。
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not session_id:
            raise ValueError("session id must not be empty")

    @staticmethod
    def _encode_session(session: Session) -> dict[str, Any]: # session对象->dict
        return {
            "session_id": session.session_id,
            "messages": [
                {
                    "role": message.role.value,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                }
                for message in session.messages
            ],
            "tool_calls": [
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                }
                for call in session.tool_calls
            ],
            "tool_results": [
                {
                    "call_id": result.call_id,
                    "tool_name": result.tool_name,
                    "status": result.status.value,
                    "content": result.content,
                    "error_type": result.error_type,
                    "timeout_semantics": (
                        result.timeout_semantics.value
                        if result.timeout_semantics is not None
                        else None
                    ),
                }
                for result in session.tool_results
            ],
            "todos": list(session.todos),
            "summary": (
                {
                    "content": session.summary.content,
                    "covered_message_count": session.summary.covered_message_count,
                }
                if session.summary is not None
                else None
            ),
        }

    @staticmethod
    def _decode_session(payload: Any, *, expected_id: str) -> Session: # dict->session对象
        if not isinstance(payload, dict) or payload.get("session_id") != expected_id:
            raise SessionStorageError("session snapshot has an invalid session id")
        messages = tuple(
            Message(
                role=MessageRole(item["role"]),
                content=item["content"],
                tool_call_id=item.get("tool_call_id"),
            )
            for item in payload.get("messages", [])
        )
        calls = tuple(
            ToolCall(
                call_id=item["call_id"],
                name=item["name"],
                arguments=item.get("arguments", {}),
            )
            for item in payload.get("tool_calls", [])
        )
        results = tuple(
            ToolResult(
                call_id=item["call_id"],
                tool_name=item["tool_name"],
                status=ToolResultStatus(item["status"]),
                content=item["content"],
                error_type=item.get("error_type"),
                # 旧快照没有这个字段，缺省即"未记录超时语义"，不伪造。
                timeout_semantics=(
                    TimeoutSemantics(item["timeout_semantics"])
                    if item.get("timeout_semantics")
                    else None
                ),
            )
            for item in payload.get("tool_results", [])
        )
        raw_summary = payload.get("summary")
        summary = (
            SessionSummary(
                content=raw_summary["content"],
                covered_message_count=raw_summary["covered_message_count"],
            )
            if raw_summary is not None
            else None
        )
        return Session(
            session_id=expected_id,
            messages=messages,
            tool_calls=calls,
            tool_results=results,
            todos=tuple(payload.get("todos", [])),
            summary=summary,
        )
