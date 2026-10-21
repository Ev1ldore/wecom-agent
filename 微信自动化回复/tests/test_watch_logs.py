"""Exercise the read-only terminal transcript without any worker side effects."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from watch_logs import (
    TranscriptView,
    format_event,
    format_record,
    read_records,
    redact_text,
)


def record(key: str = "one", **changes: Any) -> dict[str, Any]:
    """Create a fixed Ri conversation row with a minute-precision source time."""
    return {
        "key": key,
        "chat": "desktop:wechat:Ri",
        "sender": "desktop:wechat:Ri",
        "content": f"来信-{key}",
        "ts": None,
        "time_precision": "minute",
        "time_display": "10/17 18:20",
        "chat_type": "single",
        "is_self": 0,
        "status": "sent",
        "reply": f"回复-{key}",
        "error": "",
        "created_at": 1,
        "updated_at": 1,
        **changes,
    }


def test_redaction_hides_secrets_and_terminal_control_characters() -> None:
    """Known credentials and terminal escape codes must never reach the screen."""
    output = redact_text("正文 key-123 \x1b[2J\r改写", ("key-123", ""))
    assert "key-123" not in output
    assert "[REDACTED]" in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert "正文" in output


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("sent", "已提交"),
        ("dry_run", "草稿"),
        ("send_unknown", "未知"),
        ("send_failed", "失败"),
    ],
)
def test_format_records_preserve_real_outcome(status: str, label: str) -> None:
    """An uncertain or failed write cannot be presented as successful delivery."""
    output = "\n".join(format_record(record(status=status), include_received=True))
    assert "收到" in output
    assert "来信-one" in output
    assert label in output
    assert "回复-one" in output
    assert "10/17 18:20" in output
    if status != "sent":
        assert "已提交" not in output
    assert "对方已收到" not in output


def test_format_record_status_update_does_not_repeat_received_text() -> None:
    """A status transition prints its reply without duplicating the incoming line."""
    output = "\n".join(format_record(record(), include_received=False))
    assert "收到" not in output
    assert "来信-one" not in output
    assert "回复-one" in output


def test_view_first_snapshot_only_shows_last_processed_history() -> None:
    """Opening a terminal shows context without replaying all stored history."""
    view = TranscriptView()
    rows = [record("older"), record("newer"), record("baseline", status="baseline")]
    output = "\n".join(view.update(rows))
    assert "历史" in output
    assert "来信-newer" in output
    assert "回复-newer" in output
    assert "来信-older" not in output
    assert "来信-baseline" not in output
    assert view.update(rows) == []


def test_view_new_claim_and_completion_are_printed_exactly_once() -> None:
    """Polling emits a fresh arrival and then its completed reply once each."""
    view = TranscriptView()
    assert view.update([]) == []
    claimed = record("new", status="claimed", reply="")
    incoming = "\n".join(view.update([claimed]))
    assert "收到" in incoming
    assert "来信-new" in incoming
    assert "已提交" not in incoming
    assert view.update([claimed]) == []
    sent = {**claimed, "status": "sent", "reply": "新回复", "updated_at": 2}
    completed = "\n".join(view.update([sent]))
    assert "已提交" in completed
    assert "新回复" in completed
    assert "来信-new" not in completed
    assert view.update([sent]) == []


def test_read_records_missing_database_cannot_create_file(tmp_path: Path) -> None:
    """Read-only mode refuses a nonexistent database instead of initializing one."""
    db_path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        read_records(db_path)
    assert not db_path.exists()


def test_read_records_filters_exact_ri_single_chat_without_writes(
    tmp_path: Path,
) -> None:
    """Only the authorized incoming conversation is visible in the terminal."""
    db_path = tmp_path / "messages.db"
    rows = [
        record("allowed"),
        record("other", chat="desktop:wechat:Other"),
        record("spoof", sender="desktop:wechat:Other"),
        record("self", is_self=1),
        record("group", chat_type="group"),
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TABLE desktop_messages (
                key TEXT, chat TEXT, sender TEXT, content TEXT, ts INTEGER,
                time_precision TEXT, time_display TEXT, chat_type TEXT,
                is_self INTEGER, status TEXT, reply TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER
            )"""
        )
        columns = tuple(rows[0])
        connection.executemany(
            f"INSERT INTO desktop_messages ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in rows],
        )
    original = db_path.read_bytes()
    result = read_records(db_path)
    assert [row["key"] for row in result] == ["allowed"]
    assert db_path.read_bytes() == original


@pytest.mark.parametrize("level", ["warning", "error"])
def test_format_event_shows_warning_or_error_safely(level: str) -> None:
    """Troubleshooting stays visible while known secrets remain redacted."""
    output = format_event(
        json.dumps({"level": level, "event": "connection_failed", "error": "key-123"}),
        secrets=("key-123",),
    )
    assert output is not None
    assert "connection_failed" in output
    assert "key-123" not in output
    assert "[REDACTED]" in output


def test_format_event_hides_routine_info_polling() -> None:
    """Idle polling does not bury incoming messages and replies in noise."""
    assert format_event(json.dumps({"level": "info", "event": "personal_poll"})) is None
