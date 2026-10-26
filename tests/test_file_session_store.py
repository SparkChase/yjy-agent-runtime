"""JSON 文件 SessionStore 的恢复、隔离和损坏处理测试。"""

import json
import os
from pathlib import Path

import pytest

from yjy_agent_runtime.domain import (
    Message,
    MessageRole,
    ToolCall,
    ToolResult,
    ToolResultStatus,
)
from yjy_agent_runtime.infrastructure.session import (
    JsonFileSessionStore,
    SessionStorageError,
)
from yjy_agent_runtime.infrastructure.session import file as session_file


def test_file_store_recovers_messages_and_todos_after_reopen(tmp_path: Path) -> None:
    first = JsonFileSessionStore(tmp_path)
    first.get_or_create("window-1")
    first.append_message("window-1", Message(MessageRole.USER, "第一问"))
    first.append_todo("window-1", "记得写周报")

    reopened = JsonFileSessionStore(tmp_path)
    restored = reopened.get_or_create("window-1")

    assert [message.content for message in restored.messages] == ["第一问"]
    assert restored.todos == ("记得写周报",)


def test_file_store_keeps_two_windows_isolated(tmp_path: Path) -> None:
    store = JsonFileSessionStore(tmp_path)
    store.get_or_create("window-1")
    store.get_or_create("window-2")
    store.append_message("window-1", Message(MessageRole.USER, "天气"))
    store.append_todo("window-2", "写周报")

    assert store.get("window-1").todos == ()
    assert store.get("window-2").messages == ()
    assert store.get("window-2").todos == ("写周报",)


def test_file_store_reports_corrupted_snapshot(tmp_path: Path) -> None:
    store = JsonFileSessionStore(tmp_path)
    store.get_or_create("broken")
    snapshot = next(tmp_path.glob("*.json"))
    snapshot.write_text("not-json", encoding="utf-8")

    with pytest.raises(SessionStorageError, match="corrupted"):
        store.get("broken")


def test_tool_interaction_is_persisted_in_one_snapshot(tmp_path: Path) -> None:
    """一次工具交互只落一次盘，快照里三类数据同时出现。"""

    class SpyStore(JsonFileSessionStore):
        def __init__(self, directory: Path) -> None:
            super().__init__(directory)
            self.saved: list[object] = []

        def _save(self, session) -> None:  # type: ignore[no-untyped-def]
            self.saved.append(session)
            super()._save(session)

    store = SpyStore(tmp_path)
    store.get_or_create("window-1")
    store.append_message("window-1", Message(MessageRole.USER, "算一下"))
    call = ToolCall("call-1", "echo", {"text": "hi"})
    result = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "hi")
    store.saved.clear()

    store.append_tool_interaction(
        "window-1",
        call,
        result,
        Message(MessageRole.TOOL, result.content, call.call_id),
    )

    assert len(store.saved) == 1
    snapshot = store.saved[0]
    assert snapshot.tool_calls == (call,)
    assert snapshot.tool_results == (result,)
    assert snapshot.messages[-1].role is MessageRole.TOOL
    assert snapshot.messages[-1].tool_call_id == "call-1"


def test_failed_tool_interaction_write_leaves_no_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原子替换失败时，磁盘上仍是写入前的完整快照，不会出现半条交互。"""

    store = JsonFileSessionStore(tmp_path)
    store.get_or_create("window-1")
    store.append_message("window-1", Message(MessageRole.USER, "算一下"))
    call = ToolCall("call-1", "echo", {"text": "hi"})
    result = ToolResult("call-1", "echo", ToolResultStatus.SUCCESS, "hi")

    class FailingOs:
        """只让原子替换失败，其余 os 能力仍然真实可用。"""

        @staticmethod
        def replace(*_args: object) -> None:
            raise OSError("disk full")

        unlink = staticmethod(os.unlink)
        fsync = staticmethod(os.fsync)

    monkeypatch.setattr(session_file, "os", FailingOs)

    with pytest.raises(SessionStorageError, match="unable to save session"):
        store.append_tool_interaction(
            "window-1",
            call,
            result,
            Message(MessageRole.TOOL, result.content, call.call_id),
        )

    restored = JsonFileSessionStore(tmp_path).get_or_create("window-1")
    assert restored.tool_calls == ()
    assert restored.tool_results == ()
    assert [message.content for message in restored.messages] == ["算一下"]


def test_file_store_rejects_snapshot_with_half_tool_interaction(
    tmp_path: Path,
) -> None:
    """旧快照若只剩工具消息，读取时明确失败，而不是把残缺历史交给模型。"""

    store = JsonFileSessionStore(tmp_path)
    store.get_or_create("broken")
    snapshot = next(tmp_path.glob("*.json"))
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["messages"].append(
        {"role": "tool", "content": "半条交互", "tool_call_id": "call-1"}
    )
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SessionStorageError, match="incomplete tool interaction"):
        store.get("broken")
