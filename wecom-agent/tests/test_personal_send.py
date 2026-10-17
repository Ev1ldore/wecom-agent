"""Test one-attempt personal sends using fake children and pure receipt decisions."""

import asyncio
import base64
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from adapters import personal_reply as reply
from adapters.wecom_personal import PERSONAL_IDENTITY, PERSONAL_TITLE


@pytest.mark.asyncio
async def test_personal_send_is_disabled_by_default_before_any_native_call() -> None:
    """A constructed channel cannot send without its explicit live flag."""
    preflight = AsyncMock(return_value="OK\n")
    sender = AsyncMock(return_value="SENT\n")
    channel = reply.PersonalDesktopReplyChannel(preflight, send_runner=sender)
    with pytest.raises(reply.RealSendingDisabled):
        await channel.send_text(PERSONAL_IDENTITY, "synthetic reply")
    preflight.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_channel_preflights_then_submits_exactly_one_native_send() -> None:
    """Only a successful local receipt completes the single send attempt."""
    preflight = AsyncMock(return_value="OK\n")
    sender = AsyncMock(return_value="SENT\n")
    channel = reply.PersonalDesktopReplyChannel(
        preflight, live_enabled=True, send_runner=sender
    )
    assert await channel.send_text(PERSONAL_IDENTITY, "synthetic reply") is None
    preflight.assert_awaited_once_with()
    sender.assert_awaited_once_with("synthetic reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown", "timeout"])
async def test_unknown_or_timed_out_send_never_retries(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """An uncertain UI action cannot be repeated by the channel."""
    preflight = AsyncMock(return_value="OK\n")
    sender = AsyncMock(return_value="UNKNOWN\tRECEIPT_UNCONFIRMED\n")
    if mode == "timeout":

        async def blocked(content: str) -> str:
            await asyncio.Event().wait()
            return "SENT\n"

        sender.side_effect = blocked
        monkeypatch.setattr(reply, "SEND_TIMEOUT_SECONDS", 0.01)
    channel = reply.PersonalDesktopReplyChannel(
        preflight, live_enabled=True, send_runner=sender
    )
    with pytest.raises(reply.PersonalSendError) as caught:
        await channel.send_text(PERSONAL_IDENTITY, "synthetic reply")
    assert caught.value.outcome_unknown is True
    assert caught.value.code == (
        "SEND_TIMEOUT" if mode == "timeout" else "RECEIPT_UNCONFIRMED"
    )
    sender.assert_awaited_once_with("synthetic reply")
    preflight.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_known_prewrite_error_stops_without_send_retry() -> None:
    """Do not retry even a known failed native attempt at this layer."""
    preflight = AsyncMock(return_value="OK\n")
    sender = AsyncMock(return_value="ERROR\tCOMPOSER_NOT_EMPTY\n")
    channel = reply.PersonalDesktopReplyChannel(
        preflight, live_enabled=True, send_runner=sender
    )
    with pytest.raises(reply.PersonalSendError) as caught:
        await channel.send_text(PERSONAL_IDENTITY, "synthetic reply")
    assert caught.value.code == "COMPOSER_NOT_EMPTY"
    assert caught.value.outcome_unknown is False
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_invalid_recipient_cannot_reach_preflight_or_send() -> None:
    """A display name cannot replace the exact verified local chat identity."""
    preflight = AsyncMock()
    sender = AsyncMock()
    channel = reply.PersonalDesktopReplyChannel(
        preflight, live_enabled=True, send_runner=sender
    )
    with pytest.raises(reply.PersonalReplyError) as caught:
        await channel.send_text("Ri", "synthetic reply")
    assert caught.value.code == "TARGET_MISMATCH"
    preflight.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_payload_is_base64_data_on_stdin_and_never_in_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quotes and newlines remain draft data without becoming executable source."""
    content = '独立私密测试正文-UNIQUE-987\n" & error "INJECTED"\n第二行'
    source = reply._send_script_source(content).decode("utf-8")
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    assert f'property encodedReply : "{encoded}"' in source
    assert content not in source and "INJECTED" not in source
    match = re.search(r'property encodedReply : "([A-Za-z0-9+/=]+)"', source)
    assert match is not None
    assert base64.b64decode(match.group(1), validate=True).decode() == content
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"SENT\n", b"private native stderr")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(reply.asyncio, "create_subprocess_exec", spawn)

    assert await reply.PersonalAppleScriptSendRunner()(content) == "SENT\n"

    assert spawn.await_args.args == (reply.OSASCRIPT_PATH, "-", PERSONAL_TITLE)
    assert content not in " ".join(spawn.await_args.args)
    assert encoded not in " ".join(spawn.await_args.args)
    assert spawn.await_args.kwargs["stdin"] is asyncio.subprocess.PIPE
    assert process.communicate.await_args.kwargs["input"] == source.encode()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Pure AppleScript receipt decision"
)
@pytest.mark.parametrize(
    "before,after,body_x,expected",
    [(3, 4, 60, "true"), (3, 3, 60, "false"), (3, 4, 10, "false")],
)
def test_pure_native_receipt_requires_growth_and_right_side_geometry(
    before: int, after: int, body_x: int, expected: str
) -> None:
    """Execute only scalar synthetic logic, with no System Events or live UI."""
    source = reply.SEND_SCRIPT_PATH.read_text()
    handler = re.search(
        r"on observedOutgoingReceipt\(.*?\nend observedOutgoingReceipt",
        source,
        flags=re.DOTALL,
    )
    assert handler is not None
    pure_source = handler.group()
    assert "System Events" not in pure_source
    code = pure_source + (
        f'\nreturn observedOutgoingReceipt({before}, {after}, 1, "draft", "draft", '
        f"0, 100, {body_x}, 30, true)"
    )
    completed = subprocess.run(
        [reply.OSASCRIPT_PATH, "-e", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Pure AppleScript prefix decision")
@pytest.mark.parametrize(
    "after,expected",
    [
        (
            '{{{"AXTextArea", "history"}}, {{"AXTextArea", "anchor"}}, {{"AXTextArea", "reply"}}}',
            "true",
        ),
        (
            '{{{"AXTextArea", "older"}}, {{"AXTextArea", "history"}}, {{"AXTextArea", "anchor"}}}',
            "false",
        ),
    ],
)
def test_pure_native_prefix_rejects_history_prepend_as_a_send_receipt(
    after: str, expected: str
) -> None:
    """Require an unchanged prior prefix instead of interpreting any growth as new."""
    source = reply.SEND_SCRIPT_PATH.read_text()
    handler = re.search(
        r"on observedSendPrefix\(.*?\nend observedSendPrefix",
        source,
        flags=re.DOTALL,
    )
    assert handler is not None
    pure_source = handler.group()
    assert "System Events" not in pure_source
    before = '{{{"AXTextArea", "history"}}, {{"AXTextArea", "anchor"}}}'
    completed = subprocess.run(
        [
            reply.OSASCRIPT_PATH,
            "-e",
            pure_source + f"\nreturn observedSendPrefix({before}, {after})",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Compile native source without running it"
)
def test_complete_synthetic_send_source_compiles_without_executing_ui(
    tmp_path: Path,
) -> None:
    """Catch entrypoint syntax errors while only compiling a synthetic fixed script."""
    target = tmp_path / "send.scpt"
    completed = subprocess.run(
        ["/usr/bin/osacompile", "-o", str(target), "-"],
        input=reply._send_script_source("synthetic"),
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert target.is_file()
