"""Verify append-only snapshot cursors with synthetic personal history sources."""

import asyncio
from collections.abc import Callable

import pytest

from adapters.inbox import Inbox, Msg
from adapters.personal_tail import PersonalTailInbox, TailError, TailProtocol


def message(content: str, *, own: bool = False) -> Msg:
    """Build synthetic minute messages without timestamps, UI or environment access."""
    return Msg(
        chat="desktop:wechat:Ri",
        sender="desktop:self" if own else "desktop:wechat:Ri",
        content=content,
        ts=None,
        time_precision="minute",
        time_display="10/16 12:11",
        is_self=own,
    )


def key_of(msg: Msg) -> str:
    """Use distinct synthetic contents as injected stable test fingerprints."""
    return msg.content


class SnapshotSource:
    """Expose only prescribed local snapshots or failures, counting source reads."""

    def __init__(self, snapshots: list[list[Msg] | Exception]) -> None:
        """Retain deterministic source outcomes without external dependencies."""
        self.snapshots = list(snapshots)
        self.calls: int = 0

    async def poll_unread(self) -> list[Msg]:
        """Return a copy so callers cannot mutate later fixture observations."""
        self.calls += 1
        outcome = self.snapshots.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)


@pytest.mark.asyncio
async def test_fresh_baseline_is_idempotent_and_first_poll_does_not_reread() -> None:
    """First installs baseline all visible history without treating it as new input."""
    old = [message("old-a"), message("old-b")]
    source = SnapshotSource([old])
    tail: TailProtocol = PersonalTailInbox(source, key_of=key_of)
    baseline = await tail.baseline()
    assert baseline == old
    baseline.clear()
    assert await tail.baseline() == old
    inbox: Inbox = tail
    assert await inbox.poll_unread() == []
    assert source.calls == 1


@pytest.mark.asyncio
async def test_resume_uses_the_last_persisted_anchor_and_buffers_the_new_suffix() -> (
    None
):
    """Old rows through the last known key remain history; later unseen rows survive."""
    rows = [
        message(value) for value in ("older", "known-a", "between", "known-b", "new")
    ]
    source = SnapshotSource([rows, rows])
    tail = PersonalTailInbox(
        source, known_keys=frozenset({"known-a", "known-b"}), key_of=key_of
    )
    assert await tail.baseline() == rows[:4]
    assert await tail.poll_unread() == rows[4:]
    assert source.calls == 1
    assert await tail.poll_unread() == []
    assert source.calls == 2


@pytest.mark.asyncio
async def test_appends_are_emitted_once_and_preserve_own_messages_for_loop_filtering() -> (
    None
):
    """The tail tracker preserves normalized direction without making send decisions."""
    old, incoming, own = message("old"), message("new"), message("own", own=True)
    source = SnapshotSource([[old], [old, incoming, own], [old, incoming, own]])
    tail = PersonalTailInbox(source, key_of=key_of)
    assert await tail.baseline() == [old]
    assert await tail.poll_unread() == []
    assert await tail.poll_unread() == [incoming, own]
    assert await tail.poll_unread() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [[], ["old-a"], ["older", "old-a", "old-b"], ["old-b", "old-a"], ["replacement"]],
)
async def test_discontinuity_recovers_by_position_without_stalling(
    changed: list[str],
) -> None:
    """Truncation, prepending, reordering and lost anchors refeed candidates by position."""
    old = [message("old-a"), message("old-b")]
    changed_rows = [message(value) for value in changed]
    source = SnapshotSource([old, changed_rows, changed_rows])
    tail = PersonalTailInbox(source, key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    assert await tail.poll_unread() == changed_rows
    assert await tail.poll_unread() == []


@pytest.mark.asyncio
async def test_discontinuity_reanchors_at_the_last_own_message() -> None:
    """Recovery refeeds only the unanswered rows after the owner's latest send."""
    old = [message("old")]
    changed = [
        message("stale"),
        message("own", own=True),
        message("new-a"),
        message("new-b"),
    ]
    source = SnapshotSource([old, changed, changed])
    tail = PersonalTailInbox(source, key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    assert await tail.poll_unread() == changed[2:]
    assert await tail.poll_unread() == []


@pytest.mark.asyncio
async def test_missing_resume_anchor_fails_without_baselining_unknown_history() -> None:
    """An unrelated snapshot cannot erase unread input by becoming a fresh baseline."""
    old, new = message("known"), message("new")
    source = SnapshotSource([[message("other")], [old, new]])
    tail = PersonalTailInbox(source, known_keys=frozenset({"known"}), key_of=key_of)
    with pytest.raises(TailError) as error:
        await tail.baseline()
    assert error.value.code == "TAIL_DISCONTINUITY"
    assert await tail.baseline() == [old]
    assert await tail.poll_unread() == [new]


@pytest.mark.asyncio
async def test_empty_baseline_can_retry_without_establishing_a_cursor() -> None:
    """No original history means no baseline, even when this is the first install."""
    old = message("old")
    source = SnapshotSource([[], [old]])
    tail = PersonalTailInbox(source, key_of=key_of)
    with pytest.raises(TailError) as error:
        await tail.baseline()
    assert error.value.code == "EMPTY_BASELINE"
    assert await tail.baseline() == [old]


@pytest.mark.asyncio
async def test_source_failure_is_safe_and_does_not_skip_the_next_valid_append() -> None:
    """A failed read does not consume a new message or disclose source diagnostics."""
    old, new = message("old"), message("new")
    source = SnapshotSource(
        [[old], RuntimeError("private-history-and-credential"), [old, new]]
    )
    tail = PersonalTailInbox(source, key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    with pytest.raises(TailError) as error:
        await tail.poll_unread()
    assert error.value.code == "SOURCE_READ_FAILED"
    assert "private" not in str(error.value)
    assert error.value.__context__ is None
    assert await tail.poll_unread() == [new]


@pytest.mark.asyncio
async def test_polling_requires_an_explicit_baseline_without_source_access() -> None:
    """An Inbox consumer cannot implicitly process startup history as incoming data."""
    source = SnapshotSource([])
    tail = PersonalTailInbox(source, key_of=key_of)
    with pytest.raises(TailError) as error:
        await tail.poll_unread()
    assert error.value.code == "BASELINE_REQUIRED" and source.calls == 0


@pytest.mark.asyncio
async def test_cancelled_poll_retains_cursor_and_releases_the_serialization_lock() -> (
    None
):
    """A cancellation while observing the source leaves the accepted prefix intact."""
    old, new = message("old"), message("new")
    started = asyncio.Event()

    class CancellableSource:
        """Expose a baseline, a cancellable observation and a valid retry."""

        def __init__(self) -> None:
            self.calls: int = 0

        async def poll_unread(self) -> list[Msg]:
            """Hold only the first real poll until the test cancels it."""
            self.calls += 1
            if self.calls == 2:
                started.set()
                await asyncio.Event().wait()
            return [old] if self.calls == 1 else [old, new]

    tail = PersonalTailInbox(CancellableSource(), key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    polling = asyncio.create_task(tail.poll_unread())
    await started.wait()
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling
    assert await tail.poll_unread() == [new]


@pytest.mark.asyncio
async def test_concurrent_baselines_and_polls_never_overlap_or_emit_twice() -> None:
    """Serialize callers while sharing the same successful baseline and cursor."""
    old, new = message("old"), message("new")

    class ConcurrentSource:
        """Yield during each source read and reject overlapping observations."""

        def __init__(self) -> None:
            self.calls: int = 0
            self.active: bool = False

        async def poll_unread(self) -> list[Msg]:
            """Give queued callers a chance to race while one read remains active."""
            assert not self.active
            self.active = True
            self.calls += 1
            try:
                await asyncio.sleep(0)
                return [old] if self.calls == 1 else [old, new]
            finally:
                self.active = False

    source = ConcurrentSource()
    tail = PersonalTailInbox(source, key_of=key_of)
    assert await asyncio.gather(tail.baseline(), tail.baseline()) == [[old], [old]]
    assert source.calls == 1
    results = await asyncio.gather(*(tail.poll_unread() for _ in range(4)))
    assert [item for result in results for item in result] == [new]


@pytest.mark.asyncio
async def test_identical_fingerprints_are_left_to_the_loop_to_deduplicate() -> None:
    """Two appended rows may share a minute fingerprint; the tracker must preserve both."""
    old, repeated = message("old"), message("same-minute-input")
    source = SnapshotSource([[old], [old, repeated, repeated]])
    tail = PersonalTailInbox(source, key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    appended = await tail.poll_unread()
    assert [item.content for item in appended] == ["same-minute-input"] * 2
    keys = [item.dedupe_key or key_of(item) for item in appended]
    assert keys == ["same-minute-input", "same-minute-input#2"]


@pytest.mark.asyncio
async def test_same_minute_resend_in_a_later_poll_gets_a_distinct_key() -> None:
    """A resent copy appended by a later snapshot is not swallowed as a duplicate."""
    old, repeated = message("old"), message("same-minute-input")
    source = SnapshotSource([[old], [old, repeated], [old, repeated, repeated]])
    tail = PersonalTailInbox(source, key_of=key_of)
    await tail.baseline()
    await tail.poll_unread()
    assert await tail.poll_unread() == [repeated]
    resent = await tail.poll_unread()
    assert [item.content for item in resent] == ["same-minute-input"]
    assert (resent[0].dedupe_key or key_of(resent[0])) == "same-minute-input#2"


@pytest.mark.asyncio
async def test_fingerprint_failure_retains_the_previous_snapshot() -> None:
    """Invalid key generation fails safely before any cursor update."""
    old, new = message("old"), message("new")
    fail = False

    def changing_key(msg: Msg) -> str:
        """Fail once at the injected fingerprint boundary, never reading external data."""
        if fail:
            raise ValueError("private-input")
        return msg.content

    source = SnapshotSource([[old], [old, new], [old, new]])
    key: Callable[[Msg], str] = changing_key
    tail = PersonalTailInbox(source, key_of=key)
    await tail.baseline()
    await tail.poll_unread()
    fail = True
    with pytest.raises(TailError) as error:
        await tail.poll_unread()
    assert error.value.code == "KEY_FAILED" and "private" not in str(error.value)
    fail = False
    assert await tail.poll_unread() == [new]
