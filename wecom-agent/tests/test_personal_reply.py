"""Verify original-chat preflight without typing, native UI or real sends."""

import asyncio
import json
import logging
import re
import subprocess
import sys
import traceback
from unittest.mock import AsyncMock

import pytest
from structlog.contextvars import bound_contextvars

from adapters import personal_reply as reply
from adapters.wecom_personal import PERSONAL_IDENTITY, PERSONAL_TITLE


@pytest.fixture(autouse=True)
def no_native_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require every real native-process boundary to be explicitly mocked."""
    spawn = AsyncMock(side_effect=AssertionError("Unexpected native UI process"))
    monkeypatch.setattr(reply.asyncio, "create_subprocess_exec", spawn)


@pytest.mark.asyncio
async def test_prepare_only_preflights_and_send_stays_disabled_after_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prepared channel reports its stage while never receiving private draft data."""
    runner = AsyncMock(return_value="OK\n")
    channel = reply.PersonalDesktopReplyChannel(runner)
    preview: reply.ReplyPreview = channel
    with caplog.at_level(logging.INFO), bound_contextvars(msg_id="personal-proof-key"):
        assert (
            await preview.prepare_reply(PERSONAL_IDENTITY, "private reply text") is None
        )
        with pytest.raises(reply.RealSendingDisabled):
            await channel.send_text(PERSONAL_IDENTITY, "private reply text")
    runner.assert_awaited_once_with()
    assert "private reply text" not in caplog.text
    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert events[-1]["msg_id"] == "personal-proof-key"
    assert events[-1]["stage"] == "preflight" and events[-1]["dry_run"] is True
    assert events[-1]["enabled_unknown_allowed"] is True
    assert events[-1]["delivery_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["Ri", "app:1000002:Ri", "wm-unverified", "", None])
async def test_wrong_target_never_runs_native_precheck(target: object) -> None:
    """Only the canonical verified local original-chat identity is accepted."""
    runner = AsyncMock()
    with pytest.raises(reply.PersonalReplyError) as error:
        await reply.PersonalDesktopReplyChannel(runner).prepare_reply(target, "draft")
    assert error.value.code == "TARGET_MISMATCH"
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", " \t\n", None, 123])
async def test_empty_or_nontext_draft_never_runs_native_precheck(
    content: object,
) -> None:
    """Content validation does not let an empty draft reach even a read-only process."""
    runner = AsyncMock()
    with pytest.raises(reply.PersonalReplyError) as error:
        await reply.PersonalDesktopReplyChannel(runner).prepare_reply(
            PERSONAL_IDENTITY, content
        )
    assert error.value.code == "EMPTY_CONTENT"
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "NO_WINDOW",
        "TITLE_MISMATCH",
        "NOT_PERSONAL",
        "STRUCTURE_CHANGED",
        "COMPOSER_UNAVAILABLE",
        "COMPOSER_AMBIGUOUS",
        "COMPOSER_NOT_EMPTY",
        "COMPOSER_CHANGED",
        "PERMISSION_DENIED",
    ],
)
async def test_failed_identity_or_composer_guard_stops_without_retry(code: str) -> None:
    """Do not retry a wrong recipient, dirty composer, readonly or ambiguous control."""
    runner = AsyncMock(return_value=f"ERROR\t{code}\n")
    with pytest.raises(reply.PersonalReplyError) as error:
        await reply.PersonalDesktopReplyChannel(runner).prepare_reply(
            PERSONAL_IDENTITY, "draft"
        )
    assert error.value.code == code
    runner.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("private raw details"), "ERROR\tPROCESS_FAILED\n"]
)
async def test_transient_failure_retries_exactly_once(failure: object) -> None:
    """Only transient preflight failures may produce a second read-only attempt."""
    runner = AsyncMock(side_effect=[failure, "OK"])
    await reply.PersonalDesktopReplyChannel(runner).prepare_reply(
        PERSONAL_IDENTITY, "draft"
    )
    assert runner.await_count == 2


@pytest.mark.asyncio
async def test_exhausted_external_errors_and_logs_do_not_retain_private_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The safe public error and formatted traceback contain no raw source values."""
    runner = AsyncMock(side_effect=RuntimeError("private key and reply"))
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(reply.PersonalReplyError) as error,
    ):
        await reply.PersonalDesktopReplyChannel(runner).prepare_reply(
            PERSONAL_IDENTITY, "private draft"
        )
    assert error.value.code == "PREFLIGHT_FAILED" and runner.await_count == 2
    formatted = "".join(traceback.format_exception(error.value))
    assert "private key and reply" not in formatted
    assert "private draft" not in formatted
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_timeout_or_cancellation_reaps_only_owned_preflight_children(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Bound native reads and clean up without touching an existing auth/UI process."""
    entered = asyncio.Event()

    async def blocked(*, input: bytes) -> tuple[bytes, bytes]:
        assert b"on preflightReply(" in input
        assert b"private draft" not in input
        entered.set()
        await asyncio.Event().wait()
        return b"", b""

    process = AsyncMock()
    process.communicate.side_effect = blocked
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(reply.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(reply, "stop_cli_process", cleanup)
    monkeypatch.setattr(reply, "PREFLIGHT_TIMEOUT_SECONDS", 0.01)
    channel = reply.PersonalDesktopReplyChannel()
    if mode == "timeout":
        with pytest.raises(reply.PersonalReplyError) as error:
            await channel.prepare_reply(PERSONAL_IDENTITY, "private draft")
        assert error.value.code == "PREFLIGHT_TIMEOUT"
        assert spawn.await_count == cleanup.await_count == 2
    else:
        task = asyncio.create_task(
            channel.prepare_reply(PERSONAL_IDENTITY, "private draft")
        )
        async with asyncio.timeout(1):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawn.await_count == cleanup.await_count == 1
    for call in spawn.await_args_list:
        assert call.args == (reply.OSASCRIPT_PATH, "-", PERSONAL_TITLE)
        assert call.kwargs["start_new_session"] is reply.IS_POSIX
        assert call.kwargs["stdin"] is asyncio.subprocess.PIPE
    for call in cleanup.await_args_list:
        assert call.kwargs == {"isolated_session": reply.IS_POSIX}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_cleanup_uncertainty_blocks_retry_and_preserves_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """No second process may start if the previous child cannot be confirmed stopped."""
    entered = asyncio.Event()

    async def blocked(*, input: bytes) -> tuple[bytes, bytes]:
        entered.set()
        await asyncio.Event().wait()
        return b"", b""

    process = AsyncMock()
    process.communicate.side_effect = blocked
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock(side_effect=RuntimeError("private cleanup details"))
    monkeypatch.setattr(reply.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(reply, "stop_cli_process", cleanup)
    monkeypatch.setattr(reply, "PREFLIGHT_TIMEOUT_SECONDS", 0.01)
    channel = reply.PersonalDesktopReplyChannel()
    if mode == "timeout":
        with pytest.raises(reply.PersonalReplyError) as error:
            await channel.prepare_reply(PERSONAL_IDENTITY, "draft")
        assert error.value.code == "CLEANUP_FAILED"
        assert "private" not in "".join(traceback.format_exception(error.value))
    else:
        task = asyncio.create_task(channel.prepare_reply(PERSONAL_IDENTITY, "draft"))
        async with asyncio.timeout(1):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert spawn.await_count == cleanup.await_count == 1


@pytest.mark.asyncio
async def test_default_runner_passes_only_fixed_source_and_suppresses_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller identity and draft never become native argv, source or output."""
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"OK\n", b"private native stderr")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(reply.asyncio, "create_subprocess_exec", spawn)
    await reply.PersonalDesktopReplyChannel().prepare_reply(
        PERSONAL_IDENTITY, "private draft"
    )
    source = process.communicate.await_args.kwargs["input"].decode()
    assert source.count("\non run argv\n") == 1
    assert "on validateObservedPersonal(" in source
    assert "on preflightReply(" in source
    assert "private draft" not in source
    assert "private native stderr" not in source
    assert not re.search(
        r"perform\s+action|keystroke|key\s+code|clipboard|paste|\bclick\b|set\s+value\s+of",
        source,
        re.IGNORECASE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["OK\nextra", "ERROR\tprivate\n", "", "OK\n\n"])
async def test_partial_or_extended_native_records_cannot_pass(output: str) -> None:
    """Require exact framing instead of accepting success prefixes or unknown errors."""
    runner = AsyncMock(return_value=output)
    with pytest.raises(reply.PersonalReplyError) as error:
        await reply.PersonalDesktopReplyChannel(runner).prepare_reply(
            PERSONAL_IDENTITY, "draft"
        )
    assert error.value.code == "PROCESS_FAILED" and runner.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_previews_do_not_overlap_native_read_sequences() -> None:
    """One channel keeps its entire retry/preflight observation serialized."""
    active = False

    async def runner() -> str:
        nonlocal active
        assert not active
        active = True
        try:
            await asyncio.sleep(0)
            return "OK"
        finally:
            active = False

    channel = reply.PersonalDesktopReplyChannel(runner)
    await asyncio.gather(
        *(channel.prepare_reply(PERSONAL_IDENTITY, "draft") for _ in range(3))
    )


def pure_handler(name: str) -> str:
    """Extract only scalar validation logic; never include the native entry point."""
    source = reply.REPLY_SCRIPT_PATH.read_text(encoding="utf-8")
    start = source.index(f"on {name}(")
    end = source.index(f"end {name}", start) + len(f"end {name}")
    return source[start:end]


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript scalar checks require macOS"
)
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ('"AXTextArea", true, true, ""', "true"),
        ('"AXTextField", true, true, ""', "-2711"),
        ('"AXTextArea", false, true, ""', "-2711"),
        ('"AXTextArea", true, false, ""', "-2711"),
        ('"AXTextArea", true, missing value, ""', "true"),
        ('"AXTextArea", false, missing value, ""', "-2711"),
        ('"AXTextArea", true, missing value, "existing draft"', "-2713"),
        ('"AXTextArea", true, true, "existing draft"', "-2713"),
        ('"AXTextArea", true, true, " "', "-2713"),
    ],
)
def test_scalar_composer_validator_requires_empty_enabled_editable_textarea(
    arguments: str,
    expected: str,
) -> None:
    """Run scalar-only AppleScript handlers without System Events or desktop access."""
    script = pure_handler("validateObservedComposer") + (
        f"\ntry\nreturn my validateObservedComposer({arguments})\n"
        "on error errorText number errorNumber\nreturn errorNumber\nend try\n"
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stdout.strip() == expected


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript scalar checks require macOS"
)
@pytest.mark.parametrize(
    ("count", "expected"), [(0, "-2711"), (1, "true"), (2, "-2712")]
)
def test_scalar_composer_inventory_accepts_exactly_one_candidate(
    count: int, expected: str
) -> None:
    """Readonly/absent or ambiguous candidates cannot pass the native scalar guard."""
    script = pure_handler("validateObservedComposerCount") + (
        f"\ntry\nreturn my validateObservedComposerCount({count})\n"
        "on error errorText number errorNumber\nreturn errorNumber\nend try\n"
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stdout.strip() == expected


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript scalar checks require macOS"
)
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ('"AXSplitGroup", 2, "AXScrollArea", 1', "true"),
        ('"AXGroup", 2, "AXScrollArea", 1', "-2704"),
        ('"AXSplitGroup", 1, "AXScrollArea", 1', "-2704"),
        ('"AXSplitGroup", 2, "AXGroup", 1', "-2704"),
        ('"AXSplitGroup", 2, "AXScrollArea", 0', "-2711"),
        ('"AXSplitGroup", 2, "AXScrollArea", 2', "-2712"),
    ],
)
def test_verified_composer_layout_rejects_changed_roles_or_child_counts(
    arguments: str,
    expected: str,
) -> None:
    """Validate only the scalar layout observed for the fixed composer-area path."""
    script = (
        pure_handler("validateObservedComposerCount")
        + "\n"
        + pure_handler("validateObservedComposerLayout")
        + (
            f"\ntry\nreturn my validateObservedComposerLayout({arguments})\n"
            "on error errorText number errorNumber\nreturn errorNumber\nend try\n"
        )
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert result.stdout.strip() == expected
