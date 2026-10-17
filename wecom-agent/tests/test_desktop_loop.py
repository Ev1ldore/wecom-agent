"""Verify authorization, durable dry-runs and send-circuit behavior offline."""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from structlog.contextvars import bound_contextvars, get_contextvars

from adapters.inbox import Msg, application_chat
from src.core import desktop_loop
from src.core.desktop_loop import AgentLoop, LLMProtocol, LoopProtocol, message_key
from src.core.desktop_store import SQLiteStore
from src.gateway.wecom_client import WeComAPIError

CHAT = application_chat(1000001, "LiChengYun")
SELF_SENDERS = frozenset({"agent:1000001", "application-bot"})


def message(content: str = "human request", **fields: object) -> Msg:
    """Construct an authorized human request with optional filtered variations."""
    return Msg(
        **{
            "chat": CHAT,
            "sender": "LiChengYun",
            "content": content,
            "ts": 1700000001,
            **fields,
        }
    )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[SQLiteStore]:
    """Provide persistent storage isolated from any user database."""
    value = SQLiteStore(tmp_path / "desktop.db")
    try:
        yield value
    finally:
        await value.aclose()


def loop_for(
    messages: list[Msg], store: SQLiteStore, **options: object
) -> tuple[AgentLoop, AsyncMock, AsyncMock, AsyncMock, list[str]]:
    """Build a real loop with mocked inbox, model and outbound client boundaries."""
    inbox = AsyncMock()
    inbox.poll_unread.return_value = messages
    llm = AsyncMock()
    llm.reply.return_value = "model reply"
    sender = AsyncMock()
    emitted: list[str] = []
    emit = options.pop("emit", emitted.append)
    loop = AgentLoop(
        inbox,
        llm,
        sender,
        store,
        allowed_chat=CHAT,
        self_senders=SELF_SENDERS,
        emit=emit,
        **options,
    )
    return loop, inbox, llm, sender, emitted


@pytest.mark.asyncio
async def test_dry_run_validates_personal_destination_before_printing(
    store: SQLiteStore,
) -> None:
    """Keep destination validation correlated with the same durable message claim."""
    preview = AsyncMock()
    incoming = message()
    loop, _inbox, _llm, sender, emitted = loop_for(
        [incoming], store, reply_preview=preview
    )

    async def prepare(user_id: str, content: str) -> None:
        """Observe preflight before any local output or real send."""
        assert user_id == "LiChengYun" and content == "model reply"
        assert emitted == []
        assert get_contextvars()["msg_id"] == message_key(incoming)

    preview.prepare_reply.side_effect = prepare
    assert (await loop.run_once())["dry_run"] == 1
    preview.prepare_reply.assert_awaited_once()
    sender.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_personal_preflight_retains_draft_without_send_or_retry(
    store: SQLiteStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed chat cannot print an authorized draft or trigger a later resend."""
    preview = AsyncMock()
    preview.prepare_reply.side_effect = RuntimeError("private native detail")
    loop, _inbox, llm, sender, emitted = loop_for(
        [message()], store, reply_preview=preview
    )
    assert (await loop.run_once())["emit_failed"] == 1
    assert emitted == []
    assert (await store.records())[0]["reply"] == "model reply"
    assert (await loop.run_once())["duplicate"] == 1
    preview.prepare_reply.assert_awaited_once()
    llm.reply.assert_awaited_once()
    sender.send_text.assert_not_awaited()
    assert "private native detail" not in caplog.text


def test_minute_dedup_uses_exact_display_time_without_epoch_or_second_bucketing() -> (
    None
):
    """Keep original display labels distinct from second buckets and other chats."""
    minute = message(ts=None, time_precision="minute", time_display="10/16 12:11")
    key = message_key(minute)
    assert json.loads(key)[2] == ["display-minute", "10/16 12:11"]
    assert message_key(minute, bucket_seconds=300) == key
    assert key != message_key(message())
    assert key != message_key(minute.model_copy(update={"time_display": "10/16 12:12"}))
    assert key != message_key(minute.model_copy(update={"chat": "another-chat"}))
    assert key != message_key(minute.model_copy(update={"content": "another-input"}))


@pytest.mark.asyncio
async def test_minute_dry_run_survives_restart_without_second_model_call(
    tmp_path: Path,
) -> None:
    """Persist the exact minute label and print its precision without any send."""
    path = tmp_path / "minute.db"
    incoming = message(ts=None, time_precision="minute", time_display="10/16 12:11")
    store = SQLiteStore(path)
    loop, _inbox, llm, sender, emitted = loop_for([incoming, incoming], store)
    try:
        stats = await loop.run_once()
        assert stats["dry_run"] == stats["duplicate"] == 1
        llm.reply.assert_awaited_once_with(incoming.content)
        sender.send_text.assert_not_awaited()
        assert len(emitted) == 1
        assert "企微原始显示时间：10/16 12:11，分钟精度" in emitted[0]
    finally:
        await store.aclose()
    reopened = SQLiteStore(path)
    try:
        later, _inbox, later_llm, later_sender, later_output = loop_for(
            [incoming], reopened
        )
        assert (await later.run_once())["duplicate"] == 1
        later_llm.reply.assert_not_awaited()
        later_sender.send_text.assert_not_awaited()
        assert later_output == []
        row = (await reopened.records())[0]
        assert row["ts"] is None
        assert row["time_display"] == "10/16 12:11"
        assert row["time_precision"] == "minute"
        assert row["status"] == "dry_run"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_protocol_loop_keeps_human_requester_distinct_from_agent(
    store: SQLiteStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The logged-in human may request a reply; only the application is self."""
    incoming = message("human message exactly")
    loop, inbox, llm, sender, emitted = loop_for([incoming], store)
    protocol: LoopProtocol = loop
    llm_protocol: LLMProtocol = llm
    assert llm_protocol is llm

    async def reply(content: str) -> str:
        """Assert claim durability and message correlation before model execution."""
        records = await store.records()
        assert records[0]["status"] == "claimed"
        assert records[0]["content"] == content
        assert get_contextvars()["msg_id"] == message_key(incoming)
        return "full reply\n第二行"

    llm.reply.side_effect = reply
    with caplog.at_level(logging.INFO), bound_contextvars(msg_id="caller-message"):
        stats = await protocol.run_once()
        assert get_contextvars()["msg_id"] == "caller-message"

    inbox.poll_unread.assert_awaited_once()
    llm.reply.assert_awaited_once_with("human message exactly")
    sender.send_text.assert_not_awaited()
    assert stats["dry_run"] == 1
    assert stats["sent"] == 0
    assert emitted == ["[DRY-RUN] 将发送至 LiChengYun：full reply\n第二行"]
    record = (await store.records())[0]
    assert record["status"] == "dry_run"
    assert record["reply"] == "full reply\n第二行"
    assert all(
        json.loads(item.getMessage())["msg_id"] == message_key(incoming)
        for item in caplog.records
        if item.name == "src.core.desktop_loop"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"is_self": True},
        {"sender": "agent:1000001"},
        {"sender": "application-bot"},
        {"chat_type": "group"},
        {"chat": application_chat(1000002, "LiChengYun")},
        {"sender": "another-member"},
        {"sender": "lichengyun"},
        {"content": " \n\t"},
    ],
)
async def test_filtered_messages_cannot_reach_model_or_sender(
    store: SQLiteStore, fields: dict[str, object]
) -> None:
    """Require every whitelist and direction predicate before creating a claim."""
    loop, _inbox, llm, sender, emitted = loop_for([message(**fields)], store)

    stats = await loop.run_once()

    assert stats["filtered"] == 1
    llm.reply.assert_not_awaited()
    sender.send_text.assert_not_awaited()
    assert emitted == []
    assert await store.records() == []


def test_message_key_uses_exact_content_and_five_second_bucket() -> None:
    """Avoid sender, trimming and ambiguous delimiters changing the requested key."""
    incoming = message(" exact content ", ts=101)
    key = json.loads(message_key(incoming))
    assert key == [CHAT, hashlib.sha256(b" exact content ").hexdigest(), 20]
    assert message_key(incoming) == message_key(message(" exact content ", ts=104))
    assert message_key(incoming) != message_key(message(" exact content ", ts=105))
    assert message_key(incoming) != message_key(message("exact content", ts=101))
    assert message_key(incoming) != message_key(
        message(" exact content ", chat="other", ts=101)
    )


@pytest.mark.asyncio
async def test_durable_duplicates_are_not_regenerated_after_restart(
    tmp_path: Path,
) -> None:
    """Use one key across repeated polls, duplicates in a batch and reopened stores."""
    path = tmp_path / "restart.db"
    first_store = SQLiteStore(path)
    try:
        loop, _initial_inbox, llm, sender, _initial_emitted = loop_for(
            [message(), message(ts=1700000002)], first_store
        )
        initial = await loop.run_once()
        repeated = await loop.run_once()
        assert initial["dry_run"] == 1 and initial["duplicate"] == 1
        assert repeated["duplicate"] == 2
        llm.reply.assert_awaited_once()
    finally:
        await first_store.aclose()

    reopened = SQLiteStore(path)
    try:
        loop, _inbox, llm, sender, _emitted = loop_for([message()], reopened)
        assert (await loop.run_once())["duplicate"] == 1
        llm.reply.assert_not_awaited()
        sender.send_text.assert_not_awaited()
        assert len(await reopened.records()) == 1
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_incomplete_claim_is_not_replayed_after_restart(tmp_path: Path) -> None:
    """A process stopped after claim must fail closed on the next launch."""
    path = tmp_path / "incomplete.db"
    previous = SQLiteStore(path)
    await previous.claim(message(), message_key(message()))
    await previous.aclose()
    reopened = SQLiteStore(path)
    try:
        loop, _inbox, llm, _sender, _emitted = loop_for([message()], reopened)
        assert (await loop.run_once())["duplicate"] == 1
        llm.reply.assert_not_awaited()
        assert (await reopened.records())[0]["status"] == "claimed"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_model_error_is_recorded_once_and_next_message_continues(
    store: SQLiteStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The model adapter owns retries; the loop never sends a failed generation."""
    loop, _inbox, llm, sender, _emitted = loop_for(
        [message("bad request"), message("next request")], store
    )
    llm.reply.side_effect = [RuntimeError("private model credential"), "next reply"]

    with caplog.at_level(logging.ERROR):
        stats = await loop.run_once()

    assert llm.reply.await_count == 2
    assert stats["llm_failed"] == stats["dry_run"] == 1
    sender.send_text.assert_not_awaited()
    records = await store.records()
    assert records[0]["status"] == "llm_failed"
    assert records[0]["error"] == "RuntimeError"
    assert records[1]["reply"] == "next reply"
    assert "private model credential" not in caplog.text


@pytest.mark.asyncio
async def test_model_timeout_does_not_send_or_retry_at_loop_layer(
    store: SQLiteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a stalled model bounded without multiplying the adapter's HTTP retries."""
    loop, _inbox, llm, sender, _emitted = loop_for([message()], store)

    async def blocked_reply(content: str) -> str:
        """Remain pending until the model budget cancels the request."""
        await asyncio.Event().wait()
        return "never returned"

    llm.reply.side_effect = blocked_reply
    monkeypatch.setattr(desktop_loop, "LLM_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(1):
        stats = await loop.run_once()
    assert stats["llm_failed"] == 1
    llm.reply.assert_awaited_once()
    sender.send_text.assert_not_awaited()
    assert (await store.records())[0]["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_three_send_failures_pause_polling_then_resume_after_sixty_seconds(
    store: SQLiteStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A closed circuit leaves both inbox and external sender untouched."""
    now = [100.0]
    messages = [message(f"request-{index}") for index in range(4)]
    loop, inbox, llm, sender, emitted = loop_for(
        messages, store, dry_run=False, monotonic=lambda: now[0]
    )
    sender.send_text.side_effect = [
        WeComAPIError(60020, "private provider message"),
        RuntimeError("private transport URL"),
        TimeoutError("private timeout detail"),
        None,
    ]
    with caplog.at_level(logging.ERROR):
        first = await loop.run_once()

    assert first["send_failed"] == 3
    assert first["deferred"] == 1 and first["paused"] is True
    assert sender.send_text.await_count == 3
    assert llm.reply.await_count == 3
    assert len(await store.records()) == 3
    assert all(record["status"] == "send_failed" for record in await store.records())
    assert "private provider message" not in caplog.text
    assert "private transport URL" not in caplog.text
    assert "private timeout detail" not in caplog.text

    for current in (100.0, 159.9):
        now[0] = current
        assert (await loop.run_once())["paused"] is True
    inbox.poll_unread.assert_awaited_once()
    assert sender.send_text.await_count == 3

    now[0] = 160.0
    resumed = await loop.run_once()
    assert resumed["paused"] is False
    assert resumed["duplicate"] == 0
    assert resumed["polled"] == 0
    assert resumed["sent"] == 1
    assert inbox.poll_unread.await_count == 1
    assert sender.send_text.await_count == 4
    assert emitted == []
    assert (await store.records())[-1]["status"] == "sent"


@pytest.mark.asyncio
async def test_circuit_retains_unprocessed_messages_from_non_replaying_inbox(
    store: SQLiteStore,
) -> None:
    """Resume retained work after cooldown even when the inbox cursor already advanced."""
    now = [100.0]
    batch = [message(f"request-{index}") for index in range(4)]
    loop, inbox, llm, sender, _emitted = loop_for(
        batch, store, dry_run=False, monotonic=lambda: now[0]
    )
    inbox.poll_unread.side_effect = [batch, []]
    sender.send_text.side_effect = [
        RuntimeError(),
        RuntimeError(),
        RuntimeError(),
        None,
    ]

    first = await loop.run_once()
    assert first["deferred"] == 1
    assert len(await store.records()) == 3
    now[0] = 159.0
    paused = await loop.run_once()
    assert paused["paused"] is True and paused["deferred"] == 1
    inbox.poll_unread.assert_awaited_once()
    assert llm.reply.await_count == sender.send_text.await_count == 3

    now[0] = 160.0
    resumed = await loop.run_once()
    assert resumed["sent"] == 1
    assert resumed["deferred"] == resumed["polled"] == 0
    inbox.poll_unread.assert_awaited_once()
    llm.reply.assert_awaited_with("request-3")
    assert sender.send_text.await_count == 4
    records = await store.records()
    assert len(records) == 4
    assert records[-1]["content"] == "request-3" and records[-1]["status"] == "sent"

    empty = await loop.run_once()
    assert empty["polled"] == 0
    assert inbox.poll_unread.await_count == 2
    assert sender.send_text.await_count == 4


@pytest.mark.asyncio
async def test_success_resets_consecutive_send_failure_counter(
    store: SQLiteStore,
) -> None:
    """Two failures on either side of a success must not trip the circuit."""
    messages = [message(f"request-{index}") for index in range(5)]
    loop, _inbox, _llm, sender, _emitted = loop_for(messages, store, dry_run=False)
    sender.send_text.side_effect = [
        RuntimeError(),
        RuntimeError(),
        None,
        RuntimeError(),
        RuntimeError(),
    ]

    stats = await loop.run_once()

    assert stats["send_failed"] == 4
    assert stats["sent"] == 1
    assert stats["paused"] is False
    assert sender.send_text.await_count == 5


@pytest.mark.asyncio
async def test_failed_send_is_deduplicated_in_later_cycles(store: SQLiteStore) -> None:
    """An uncertain external outcome must not be retried by polling again."""
    loop, _inbox, llm, sender, _emitted = loop_for([message()], store, dry_run=False)
    sender.send_text.side_effect = TimeoutError("possibly accepted")
    assert (await loop.run_once())["send_failed"] == 1
    assert (await loop.run_once())["duplicate"] == 1
    sender.send_text.assert_awaited_once()
    llm.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_output_failure_preserves_reply_without_sending_or_replaying(
    store: SQLiteStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Persist a safe failure even when the local display callback raises."""

    def broken_emit(text: str) -> None:
        """Simulate a local output sink containing private implementation details."""
        raise OSError("private output sink credential")

    loop, _inbox, llm, sender, _emitted = loop_for([message()], store, emit=broken_emit)
    with caplog.at_level(logging.ERROR):
        first = await loop.run_once()
    assert first["emit_failed"] == 1
    record = (await store.records())[0]
    assert record["status"] == "emit_failed"
    assert record["reply"] == "model reply"
    assert record["error"] == "OSError"
    assert "private output sink credential" not in caplog.text
    assert (await loop.run_once())["duplicate"] == 1
    llm.reply.assert_awaited_once()
    sender.send_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_successful_output_followed_by_persistence_failure_is_audited_and_stops(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
) -> None:
    """Do not misreport accepted sends or printed output when the final DB update fails."""
    incoming = message()
    loop, _inbox, llm, sender, emitted = loop_for([incoming], store, dry_run=dry_run)
    finish = AsyncMock(side_effect=OSError("private database failure detail"))
    monkeypatch.setattr(store, "finish", finish)

    with caplog.at_level(logging.ERROR), pytest.raises(OSError):
        await loop.run_once()

    finish.assert_awaited_once()
    assert (await store.records())[0]["status"] == "claimed"
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "src.core.desktop_loop"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "desktop_outcome_persistence_failed"
    assert event["msg_id"] == message_key(incoming)
    assert event["persistence_failed"] is True
    assert event["error"] == "OSError"
    assert event["output_printed" if dry_run else "send_accepted"] is True
    assert "private database failure detail" not in caplog.text
    if dry_run:
        assert emitted == ["[DRY-RUN] 将发送至 LiChengYun：model reply"]
        sender.send_text.assert_not_awaited()
    else:
        assert emitted == []
        sender.send_text.assert_awaited_once_with("LiChengYun", "model reply")

    assert (await loop.run_once())["duplicate"] == 1
    llm.reply.assert_awaited_once()
    assert sender.send_text.await_count == (0 if dry_run else 1)
    finish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_cancelled_final_update_propagates_without_retry_or_failure_log(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    dry_run: bool,
) -> None:
    """Cancellation remains cancellation after an external or local output completed."""
    loop, _inbox, llm, sender, _emitted = loop_for([message()], store, dry_run=dry_run)
    entered_finish = asyncio.Event()

    async def blocked_finish(*args: object, **kwargs: object) -> None:
        """Wait for task cancellation after the output boundary has completed."""
        entered_finish.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store, "finish", blocked_finish)
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(loop.run_once())
        async with asyncio.timeout(1):
            await entered_finish.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "desktop_outcome_persistence_failed" not in caplog.text
    assert (await store.records())[0]["status"] == "claimed"
    assert (await loop.run_once())["duplicate"] == 1
    llm.reply.assert_awaited_once()
    assert sender.send_text.await_count == (0 if dry_run else 1)
