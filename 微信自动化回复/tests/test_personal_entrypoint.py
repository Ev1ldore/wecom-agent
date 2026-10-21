"""Exercise the personal runtime baseline and dry-run gates without desktop I/O."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agent_loop
from adapters.inbox import InboxError, Msg
from src.core.desktop_loop import message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings


def minute_message(content: str, label: str) -> Msg:
    """Retain a raw display label without assigning a fabricated epoch value."""
    return Msg(
        chat="desktop:wechat:Ri",
        sender="desktop:wechat:Ri",
        content=content,
        ts=None,
        time_precision="minute",
        time_display=label,
    )


@pytest.mark.asyncio
async def test_personal_runtime_baselines_history_then_only_prepares_new_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Skip existing history, ignore own messages and keep all real sends disabled."""
    from adapters import personal_refresh, personal_reply, wecom_personal

    old = minute_message("old", "10/16 12:11")
    new = minute_message("new", "10/17 15:30")
    own = new.model_copy(
        update={"content": "own", "sender": "desktop:self", "is_self": True}
    )
    polls = AsyncMock(side_effect=[[old], [old, new, own]])
    reader = object()
    model = AsyncMock()
    model.reply.return_value = "mock personal reply"

    class FakeInbox:
        """Return two deterministic snapshots using the production constructor."""

        def __init__(self, actual_reader: object) -> None:
            """Verify that only the injected read-only reader is selected."""
            assert actual_reader is reader

        async def poll_unread(self) -> list[Msg]:
            """Return startup history followed by a new incoming request."""
            return await polls()

    @asynccontextmanager
    async def fake_model(*args: Any, **kwargs: Any):
        """Keep all model calls mocked, including context setup."""
        assert kwargs["template_name"] == "personal_reply.txt"
        yield model

    sender = AsyncMock(side_effect=AssertionError("no real sends"))
    preview = AsyncMock()
    monkeypatch.setattr(
        personal_reply, "PersonalDesktopReplyChannel", lambda **kwargs: preview
    )
    monkeypatch.setattr(personal_refresh, "PersonalRefreshingReader", lambda: reader)
    monkeypatch.setattr(wecom_personal, "PersonalHistoryInbox", FakeInbox)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", fake_model)
    monkeypatch.setattr(agent_loop.DisabledSender, "send_text", sender)

    async def two_cycles(loop: Any, *, once: bool, interval: float) -> int:
        """Exercise the empty initial tail batch followed by an actual new row."""
        assert once is False
        assert (await loop.run_once())["dry_run"] == 0
        assert (await loop.run_once())["dry_run"] == 1
        return 0

    monkeypatch.setattr(agent_loop, "run_cycles", two_cycles)
    settings = DesktopSettings(
        _env_file=None,
        inbox_backend="personal",
        db_url=f"sqlite:///{tmp_path / 'personal.db'}",
    )
    assert await agent_loop.run_personal(settings, once=False) == 0
    assert polls.await_count == 2
    model.reply.assert_awaited_once_with("new")
    sender.assert_not_awaited()
    preview.prepare_reply.assert_awaited_once_with(
        "desktop:wechat:Ri", "mock personal reply"
    )
    store = SQLiteStore(settings.sqlite_path)
    try:
        rows = await store.records()
        assert [(row["content"], row["status"]) for row in rows] == [
            ("old", "baseline"),
            ("new", "dry_run"),
        ]
        assert all(
            row["ts"] is None and row["time_precision"] == "minute" for row in rows
        )
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_personal_runtime_wrong_recipient_precedes_reader_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling live sending never expands the verified Ri recipient boundary."""
    from adapters import personal_refresh

    read = AsyncMock(side_effect=AssertionError("must not read"))
    monkeypatch.setattr(personal_refresh, "PersonalRefreshingReader", read)
    settings = DesktopSettings(_env_file=None, dry_run=False).model_copy(
        update={"personal_contact_name": "Other"}
    )
    with pytest.raises(agent_loop.SetupError, match="Ri"):
        await agent_loop.run_personal(settings, once=True)
    read.assert_not_called()


@pytest.mark.asyncio
async def test_personal_wrong_window_stops_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Never fall back to another conversation when the history reader rejects UI."""
    from adapters import wecom_personal

    inbox = AsyncMock()
    inbox.poll_unread.side_effect = InboxError("fixed wrong-window error")
    model_factory = AsyncMock(side_effect=AssertionError("no model call"))
    monkeypatch.setattr(wecom_personal, "PersonalHistoryInbox", lambda reader: inbox)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", model_factory)
    settings = DesktopSettings(
        _env_file=None, db_url=f"sqlite:///{tmp_path / 'closed.db'}"
    )
    with pytest.raises(InboxError):
        await agent_loop.run_personal(settings, once=True)
    model_factory.assert_not_called()


@pytest.mark.asyncio
async def test_personal_runtime_resumes_new_input_after_durable_anchor_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recover an unprocessed new message and keep restart from generating twice."""
    from adapters import personal_refresh, personal_reply, wecom_personal

    old = minute_message("old", "10/16 12:11")
    new = minute_message("接收测试-1017", "10/17 16:30")
    settings = DesktopSettings(
        _env_file=None, db_url=f"sqlite:///{tmp_path / 'resume.db'}"
    )
    store = SQLiteStore(settings.sqlite_path)
    try:
        assert await store.claim(old, message_key(old))
        await store.finish(message_key(old), "baseline")
    finally:
        await store.aclose()
    source = AsyncMock()
    source.poll_unread.return_value = [old, new]
    model = AsyncMock()
    model.reply.return_value = "收到测试消息。"

    @asynccontextmanager
    async def fake_model(*args: Any, **kwargs: Any):
        """Keep the configured model and sender fully mocked."""
        yield model

    sender = AsyncMock(side_effect=AssertionError("must not send"))
    preview = AsyncMock()
    monkeypatch.setattr(
        personal_reply, "PersonalDesktopReplyChannel", lambda **kwargs: preview
    )
    monkeypatch.setattr(personal_refresh, "PersonalRefreshingReader", lambda: object())
    monkeypatch.setattr(wecom_personal, "PersonalHistoryInbox", lambda reader: source)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", fake_model)
    monkeypatch.setattr(agent_loop.DisabledSender, "send_text", sender)
    assert await agent_loop.run_personal(settings, once=True) == 0
    assert await agent_loop.run_personal(settings, once=True) == 0
    model.reply.assert_awaited_once_with("接收测试-1017")
    sender.assert_not_awaited()
    preview.prepare_reply.assert_awaited_once_with(
        "desktop:wechat:Ri", "收到测试消息。"
    )
    assert source.poll_unread.await_count == 2
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = await reopened.records()
        assert [(row["content"], row["status"]) for row in rows] == [
            ("old", "baseline"),
            ("接收测试-1017", "dry_run"),
        ]
        assert rows[-1]["time_display"] == "10/17 16:30"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_saved_personal_draft_recovers_without_changing_key_or_regenerating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repair a failed precheck using its saved reply and preserve durable dedup."""
    incoming = minute_message("接收测试-1017", "10/17 16:30")
    settings = DesktopSettings(
        _env_file=None, db_url=f"sqlite:///{tmp_path / 'recover.db'}"
    )
    store = SQLiteStore(settings.sqlite_path)
    preview = AsyncMock()
    key = message_key(incoming)
    try:
        assert await store.claim(incoming, key)
        await store.finish(
            key, "emit_failed", reply="原有完整草稿", error="PersonalReplyError"
        )
        await agent_loop._recover_personal_drafts(settings, store, preview)
        await agent_loop._recover_personal_drafts(settings, store, preview)
        preview.prepare_reply.assert_awaited_once_with(incoming.sender, "原有完整草稿")
        row = (await store.records())[0]
        assert row["key"] == key and row["status"] == "dry_run"
        assert row["reply"] == "原有完整草稿" and row["ts"] is None
        assert not await store.claim(incoming, key)
        assert capsys.readouterr().out.count("原有完整草稿") == 1
    finally:
        await store.aclose()


def test_main_personal_flag_selects_personal_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choose the member-account path without changing private environment files."""
    settings = DesktopSettings(_env_file=None)
    personal = AsyncMock(return_value=0)
    legacy = AsyncMock(side_effect=AssertionError("wrong identity"))
    monkeypatch.setattr(agent_loop, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(agent_loop, "run_personal", personal)
    monkeypatch.setattr(agent_loop, "run_cli", legacy)
    assert agent_loop.main(["--personal", "--once"]) == 0
    personal.assert_awaited_once_with(settings, once=True)
    legacy.assert_not_awaited()
