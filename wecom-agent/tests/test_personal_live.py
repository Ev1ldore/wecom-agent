"""Exercise authorized personal live dispatch using only fake native/model boundaries."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agent_loop
from adapters.inbox import Msg
from src.core.desktop_loop import AgentLoop, message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings

PERSONAL_CHAT = "desktop:wechat:Ri"


def message(content: str, label: str, **updates: Any) -> Msg:
    """Build synthetic minute-precision input without inventing an epoch value."""
    return Msg(
        chat=PERSONAL_CHAT,
        sender=PERSONAL_CHAT,
        content=content,
        ts=None,
        time_precision="minute",
        time_display=label,
    ).model_copy(update=updates)


def settings_for(path: Path, *, dry_run: bool = False) -> DesktopSettings:
    """Disable environment-file loading and isolate every test's durable claims."""
    return DesktopSettings(
        _env_file=None,
        dry_run=dry_run,
        inbox_backend="personal",
        db_url=f"sqlite:///{path}",
        llm_base_url="https://model.invalid/v1",
        llm_api_key="synthetic-model-key",
        llm_model="synthetic-model",
    )


def install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: list[list[Msg]],
    *,
    cycles: int,
    send_error: BaseException | None = None,
) -> tuple[AsyncMock, AsyncMock, list[AsyncMock]]:
    """Replace all UI/model/send boundaries while keeping real tail/loop/SQLite code."""
    from adapters import personal_refresh, personal_reply, wecom_personal

    source = AsyncMock()
    source.poll_unread.side_effect = snapshots
    model = AsyncMock()
    model.reply.return_value = "synthetic new reply"
    channels: list[AsyncMock] = []
    reader = object()

    def fake_history(actual_reader: object) -> AsyncMock:
        assert actual_reader is reader
        return source

    def fake_channel(
        runner: object | None = None,
        *,
        live_enabled: bool = False,
        send_runner: object | None = None,
    ) -> AsyncMock:
        assert runner is None and send_runner is None
        channel = AsyncMock()
        channel.live_enabled = live_enabled
        if send_error is not None:
            channel.send_text.side_effect = send_error
        channels.append(channel)
        return channel

    @asynccontextmanager
    async def fake_model(*args: Any, **kwargs: Any) -> AsyncIterator[AsyncMock]:
        assert kwargs["template_name"] == "personal_reply.txt"
        yield model

    async def run_cycles(loop: Any, *, once: bool, interval: float) -> int:
        assert once is False
        assert interval > 0
        failed = False
        for _ in range(cycles):
            stats = await loop.run_once()
            failed = failed or bool(stats["send_failed"])
        return int(failed)

    monkeypatch.setattr(personal_refresh, "PersonalRefreshingReader", lambda: reader)
    monkeypatch.setattr(wecom_personal, "PersonalHistoryInbox", fake_history)
    monkeypatch.setattr(personal_reply, "PersonalDesktopReplyChannel", fake_channel)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", fake_model)
    monkeypatch.setattr(agent_loop, "run_cycles", run_cycles)
    monkeypatch.setattr(
        agent_loop.DisabledSender,
        "send_text",
        AsyncMock(
            side_effect=AssertionError("live must use its explicit personal channel")
        ),
    )
    return source, model, channels


@pytest.mark.asyncio
async def test_live_baselines_historical_drafts_then_sends_only_new_append_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing mode cannot promote a saved dry-run or failed preview into a send."""
    settings = settings_for(tmp_path / "live.db")
    old = message("old dry-run input", "10/16 12:11")
    failed = message("old failed preview", "10/17 16:30")
    unseen = message("history never previously claimed", "10/17 16:31")
    new = message("new input after live baseline", "10/17 16:32")
    store = SQLiteStore(settings.sqlite_path)
    try:
        assert await store.claim(old, message_key(old))
        await store.finish(message_key(old), "dry_run", reply="old dry-run draft")
        assert await store.claim(failed, message_key(failed))
        await store.finish(
            message_key(failed),
            "emit_failed",
            reply="old failed draft",
            error="PersonalReplyError",
        )
    finally:
        await store.aclose()
    source, model, channels = install_fake_runtime(
        monkeypatch,
        [[old, failed, unseen], [old, failed, unseen, new], [old, failed, unseen, new]],
        cycles=3,
    )
    recover = AsyncMock(
        side_effect=AssertionError("live cannot recover historical drafts")
    )
    monkeypatch.setattr(agent_loop, "_recover_personal_drafts", recover)

    assert await agent_loop.run_personal(settings, once=False) == 0

    assert source.poll_unread.await_count == 3
    recover.assert_not_awaited()
    model.reply.assert_awaited_once_with(new.content)
    assert len(channels) == 1 and channels[0].live_enabled is True
    channels[0].prepare_reply.assert_not_awaited()
    channels[0].send_text.assert_awaited_once_with(PERSONAL_CHAT, "synthetic new reply")
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = {row["content"]: row for row in await reopened.records()}
        assert rows[old.content]["status"] == "dry_run"
        assert rows[old.content]["reply"] == "old dry-run draft"
        assert rows[failed.content]["status"] == "emit_failed"
        assert rows[failed.content]["reply"] == "old failed draft"
        assert rows[unseen.content]["status"] == "baseline"
        assert rows[new.content]["status"] == "sent"
        assert rows[new.content]["reply"] == "synthetic new reply"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_live_restart_baselines_messages_received_while_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart preserves prior sent claims and never sends newly visible backlog."""
    settings = settings_for(tmp_path / "restart.db")
    old = message("startup history", "10/16 12:11")
    new = message("first live new input", "10/17 16:32")
    stopped = message("arrived while runtime stopped", "10/17 16:33")
    _, first_model, first_channels = install_fake_runtime(
        monkeypatch, [[old], [old, new]], cycles=2
    )
    assert await agent_loop.run_personal(settings, once=False) == 0
    first_model.reply.assert_awaited_once_with(new.content)
    first_channels[0].send_text.assert_awaited_once()

    source, second_model, second_channels = install_fake_runtime(
        monkeypatch,
        [[old, new, stopped], [old, new, stopped]],
        cycles=2,
    )
    assert await agent_loop.run_personal(settings, once=False) == 0
    assert source.poll_unread.await_count == 2
    second_model.reply.assert_not_awaited()
    second_channels[0].send_text.assert_not_awaited()
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = {row["content"]: row for row in await reopened.records()}
        assert rows[new.content]["status"] == "sent"
        assert rows[stopped.content]["status"] == "baseline"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "own_updates",
    [
        {"sender": "desktop:self", "is_self": False},
        {"sender": PERSONAL_CHAT, "is_self": True},
    ],
)
async def test_live_own_output_never_reaches_model_or_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, own_updates: dict[str, Any]
) -> None:
    """Reject both the explicit owner identity and the source's self-direction flag."""
    settings = settings_for(tmp_path / "self.db")
    old = message("startup history", "10/16 12:11")
    own = message("own outgoing bubble", "10/17 16:32", **own_updates)
    incoming = message("new peer request", "10/17 16:33")
    _, model, channels = install_fake_runtime(
        monkeypatch, [[old], [old, own, incoming]], cycles=2
    )
    assert await agent_loop.run_personal(settings, once=False) == 0
    model.reply.assert_awaited_once_with(incoming.content)
    channels[0].send_text.assert_awaited_once_with(PERSONAL_CHAT, "synthetic new reply")
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        assert own.content not in {row["content"] for row in await reopened.records()}
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_live_send_error_is_not_retried_when_same_input_is_polled_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An uncertain fake submission stays claimed and is never blindly resubmitted."""
    settings = settings_for(tmp_path / "uncertain.db")
    old = message("startup history", "10/16 12:11")
    incoming = message("new input with uncertain result", "10/17 16:32")
    _, model, channels = install_fake_runtime(
        monkeypatch,
        [[old], [old, incoming], [old, incoming]],
        cycles=3,
        send_error=RuntimeError("synthetic uncertain send"),
    )
    assert await agent_loop.run_personal(settings, once=False) == 1
    model.reply.assert_awaited_once_with(incoming.content)
    channels[0].send_text.assert_awaited_once()
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = {row["content"]: row for row in await reopened.records()}
        assert rows[incoming.content]["status"] == "send_failed"
        assert not await reopened.claim(incoming, message_key(incoming))
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_live_unknown_submission_preserves_reply_without_retrying_same_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep uncertain native outcomes separate from known pre-send failures."""
    from adapters.personal_reply import PersonalSendUnknown

    settings = settings_for(tmp_path / "unknown.db")
    old = message("startup history", "10/16 12:11")
    incoming = message("uncertain native submission", "10/17 16:32")
    _, model, channels = install_fake_runtime(
        monkeypatch,
        [[old], [old, incoming], [old, incoming]],
        cycles=3,
        send_error=PersonalSendUnknown("RECEIPT_UNCONFIRMED"),
    )
    assert await agent_loop.run_personal(settings, once=False) == 1
    model.reply.assert_awaited_once_with(incoming.content)
    channels[0].send_text.assert_awaited_once()
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = {row["content"]: row for row in await reopened.records()}
        assert rows[incoming.content]["status"] == "send_unknown"
        assert rows[incoming.content]["reply"] == "synthetic new reply"
        assert not await reopened.claim(incoming, message_key(incoming))
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_live_cancelled_submission_records_unknown_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not turn cancellation after a potential UI action into a retryable error."""
    from adapters.personal_reply import PersonalSendCancelled

    settings = settings_for(tmp_path / "cancelled.db")
    old = message("startup history", "10/16 12:11")
    incoming = message("cancelled native submission", "10/17 16:32")
    _, model, channels = install_fake_runtime(
        monkeypatch,
        [[old], [old, incoming]],
        cycles=2,
        send_error=PersonalSendCancelled(),
    )
    with pytest.raises(asyncio.CancelledError):
        await agent_loop.run_personal(settings, once=False)
    model.reply.assert_awaited_once_with(incoming.content)
    channels[0].send_text.assert_awaited_once()
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        rows = {row["content"]: row for row in await reopened.records()}
        assert rows[incoming.content]["status"] == "send_unknown"
        assert rows[incoming.content]["reply"] == "synthetic new reply"
        assert rows[incoming.content]["error"] == "PersonalSendCancelled"
        assert not await reopened.claim(incoming, message_key(incoming))
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_unknown_result_persistence_failure_is_audited_and_claim_prevents_resend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed UNKNOWN update keeps its first claim and records only safe metadata."""
    from adapters.personal_reply import PersonalSendUnknown

    path = tmp_path / "unknown-audit.db"
    incoming = message("synthetic request", "10/17 16:32")
    inbox = AsyncMock()
    inbox.poll_unread.return_value = [incoming]
    model = AsyncMock()
    model.reply.return_value = "synthetic draft"
    sender = AsyncMock()
    sender.send_text.side_effect = PersonalSendUnknown("RECEIPT_UNCONFIRMED")
    store = SQLiteStore(path)
    monkeypatch.setattr(
        store, "finish", AsyncMock(side_effect=OSError("private-db-detail"))
    )
    loop = AgentLoop(
        inbox,
        model,
        sender,
        store,
        allowed_chat=PERSONAL_CHAT,
        allowed_sender=PERSONAL_CHAT,
        self_senders=frozenset({"desktop:self"}),
        dry_run=False,
    )
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(OSError):
            await loop.run_once()
        events = [json.loads(record.getMessage()) for record in caplog.records]
        audits = [
            event
            for event in events
            if event["event"] == "desktop_outcome_persistence_failed"
        ]
        assert len(audits) == 1
        assert audits[0]["outcome_unknown"] is True
        assert audits[0]["persistence_failed"] is True
        assert audits[0]["error"] == "OSError"
        assert audits[0]["msg_id"] == message_key(incoming)
        assert "private-db-detail" not in caplog.text
    finally:
        await store.aclose()
    reopened = SQLiteStore(path)
    try:
        repeated = AgentLoop(
            inbox,
            model,
            sender,
            reopened,
            allowed_chat=PERSONAL_CHAT,
            allowed_sender=PERSONAL_CHAT,
            self_senders=frozenset({"desktop:self"}),
            dry_run=False,
        )
        assert (await repeated.run_once())["duplicate"] == 1
        assert (await reopened.records())[0]["status"] == "claimed"
        sender.send_text.assert_awaited_once()
        model.reply.assert_awaited_once()
    finally:
        await reopened.aclose()


@pytest.mark.parametrize(
    "argv,backend",
    [
        (["--personal", "--live", "--once"], "cli"),
        (["--live", "--once"], "personal"),
    ],
)
def test_live_flag_dispatches_only_personal_runtime_with_false_dry_run(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], backend: str
) -> None:
    """The explicit flag enables the personal channel while preserving configured data."""
    settings = DesktopSettings(_env_file=None, inbox_backend=backend, dry_run=True)
    personal = AsyncMock(return_value=0)
    legacy = AsyncMock(
        side_effect=AssertionError("live must not select legacy app sends")
    )
    monkeypatch.setattr(agent_loop, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(agent_loop, "run_personal", personal)
    monkeypatch.setattr(agent_loop, "run_cli", legacy)

    assert agent_loop.main(argv) == 0

    personal.assert_awaited_once()
    actual_settings = personal.await_args.args[0]
    assert actual_settings.dry_run is False
    assert actual_settings.personal_chat == PERSONAL_CHAT
    assert personal.await_args.kwargs == {"once": True}
    legacy.assert_not_awaited()


def test_false_dry_run_configuration_selects_personal_runtime_without_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The newly authorized setting works without requiring redundant flag approval."""
    settings = DesktopSettings(_env_file=None, inbox_backend="personal", dry_run=False)
    personal = AsyncMock(return_value=0)
    legacy = AsyncMock(side_effect=AssertionError("wrong sender identity"))
    monkeypatch.setattr(agent_loop, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(agent_loop, "run_personal", personal)
    monkeypatch.setattr(agent_loop, "run_cli", legacy)
    assert agent_loop.main(["--once"]) == 0
    personal.assert_awaited_once_with(settings, once=True)
    legacy.assert_not_awaited()


@pytest.mark.parametrize("argv", [["--live"], ["--demo", "--live"]])
def test_live_flag_rejects_legacy_or_demo_without_invoking_runtime(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """An incompatible mode cannot begin observations, model calls or send actions."""
    from smoke import smoke_desktop

    settings = DesktopSettings(_env_file=None, inbox_backend="cli", dry_run=True)
    personal = AsyncMock(side_effect=AssertionError("must not run"))
    legacy = AsyncMock(side_effect=AssertionError("must not run"))
    demo = AsyncMock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(agent_loop, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(agent_loop, "run_personal", personal)
    monkeypatch.setattr(agent_loop, "run_cli", legacy)
    monkeypatch.setattr(smoke_desktop, "run_desktop_smoke", demo)
    assert agent_loop.main(argv) == 2
    personal.assert_not_awaited()
    legacy.assert_not_awaited()
    demo.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_cli_false_dry_run_still_refuses_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorizing personal desktop replies does not enable legacy application sends."""
    auth = AsyncMock(side_effect=AssertionError("must not authenticate"))
    monkeypatch.setattr(agent_loop, "_require_cli_authorized", auth)
    settings = DesktopSettings(_env_file=None, inbox_backend="cli", dry_run=False)
    with pytest.raises(agent_loop.SetupError):
        await agent_loop.run_cli(settings, once=True)
    auth.assert_not_awaited()
