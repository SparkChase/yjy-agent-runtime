"""JSON 文件 SessionStore 的恢复、隔离和损坏处理测试。"""

from pathlib import Path

import pytest

from yjy_agent_runtime.domain import Message, MessageRole
from yjy_agent_runtime.infrastructure.session import (
    JsonFileSessionStore,
    SessionStorageError,
)


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
