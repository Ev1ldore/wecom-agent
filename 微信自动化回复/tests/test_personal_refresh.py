"""Verify the fixed history refresh boundary without UI actions or live services."""

import asyncio
import json
import logging
import re
import subprocess
import sys
import traceback
from unittest.mock import AsyncMock

import pytest

from adapters import personal_refresh as refresh
from adapters.wecom_personal import (
    PERSONAL_TITLE,
    PersonalHistoryReader,
    PersonalHistorySnapshot,
)


def snapshot() -> PersonalHistorySnapshot:
    """Return a content-free verified observation for the reader protocol test."""
    return PersonalHistorySnapshot(PERSONAL_TITLE, True, True, "", ())


@pytest.mark.asyncio
async def test_reader_protocol_refreshes_before_returning_readonly_snapshot() -> None:
    """Keep the native action separate and preserve the existing snapshot object."""
    events: list[str] = []

    async def do_refresh() -> None:
        events.append("refresh")

    async def read() -> PersonalHistorySnapshot:
        events.append("read")
        return original

    original = snapshot()
    refresher = AsyncMock()
    refresher.refresh.side_effect = do_refresh
    readonly = AsyncMock()
    readonly.read_active_chat.side_effect = read
    reader: PersonalHistoryReader = refresh.PersonalRefreshingReader(
        refresher, readonly
    )

    assert await reader.read_active_chat() is original
    assert events == ["refresh", "read"]


@pytest.mark.asyncio
async def test_refresh_failure_never_reads_stale_history() -> None:
    """Refuse stale content when the action cannot prove the panel was refreshed."""
    refresher = AsyncMock()
    refresher.refresh.side_effect = refresh.PersonalRefreshError("TITLE_MISMATCH")
    readonly = AsyncMock()
    reader = refresh.PersonalRefreshingReader(refresher, readonly)
    with pytest.raises(refresh.PersonalRefreshError, match="Ri"):
        await reader.read_active_chat()
    readonly.read_active_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_reads_cannot_close_history_during_another_snapshot() -> None:
    """Serialize the complete action/read pair instead of locking only AXPress."""
    events: list[str] = []

    async def do_refresh() -> None:
        events.append("refresh")
        await asyncio.sleep(0)

    async def read() -> PersonalHistorySnapshot:
        events.append("read")
        await asyncio.sleep(0)
        events.append("read_done")
        return snapshot()

    refresher = AsyncMock()
    refresher.refresh.side_effect = do_refresh
    readonly = AsyncMock()
    readonly.read_active_chat.side_effect = read
    reader = refresh.PersonalRefreshingReader(refresher, readonly)
    await asyncio.gather(reader.read_active_chat(), reader.read_active_chat())
    assert events == ["refresh", "read", "read_done"] * 2


@pytest.mark.asyncio
async def test_refresher_protocol_retries_uncertain_action_as_recovery_only_once() -> (
    None
):
    """A first interruption may have closed history, so recovery may reopen it."""
    runner = AsyncMock(side_effect=["ERROR\tREFRESH_INCOMPLETE\n", "OK\n"])
    refresher: refresh.PersonalRefresher = refresh.AppleScriptPersonalRefresher(runner)
    await refresher.refresh()
    assert [call.kwargs for call in runner.await_args_list] == [
        {"recover_closed": False},
        {"recover_closed": True},
    ]


@pytest.mark.asyncio
async def test_owned_partial_refresh_can_recover_on_next_poll_then_clears_evidence() -> (
    None
):
    """Two partial attempts must not permanently strand a pane we closed ourselves."""
    runner = AsyncMock(
        side_effect=[
            "ERROR\tREFRESH_INCOMPLETE\n",
            "ERROR\tREFRESH_INCOMPLETE\n",
            "OK\n",
            "ERROR\tHISTORY_CLOSED\n",
        ]
    )
    refresher = refresh.AppleScriptPersonalRefresher(runner)
    with pytest.raises(refresh.PersonalRefreshError) as caught:
        await refresher.refresh()
    assert caught.value.code == "REFRESH_INCOMPLETE"
    await refresher.refresh()
    with pytest.raises(refresh.PersonalRefreshError) as caught:
        await refresher.refresh()
    assert caught.value.code == "HISTORY_CLOSED"
    assert [call.kwargs for call in runner.await_args_list] == [
        {"recover_closed": False},
        {"recover_closed": True},
        {"recover_closed": True},
        {"recover_closed": False},
    ]


@pytest.mark.asyncio
async def test_identity_guard_clears_owned_partial_refresh_recovery() -> None:
    """A conversation change revokes recovery instead of carrying it to a new poll."""
    runner = AsyncMock(
        side_effect=[
            "ERROR\tREFRESH_INCOMPLETE\n",
            "ERROR\tREFRESH_INCOMPLETE\n",
            "ERROR\tTITLE_MISMATCH\n",
            "ERROR\tHISTORY_CLOSED\n",
        ]
    )
    refresher = refresh.AppleScriptPersonalRefresher(runner)
    for code in ("REFRESH_INCOMPLETE", "TITLE_MISMATCH", "HISTORY_CLOSED"):
        with pytest.raises(refresh.PersonalRefreshError) as caught:
            await refresher.refresh()
        assert caught.value.code == code
    assert [call.kwargs for call in runner.await_args_list] == [
        {"recover_closed": False},
        {"recover_closed": True},
        {"recover_closed": True},
        {"recover_closed": False},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("private"), "ERROR\tPROCESS_FAILED\n"]
)
async def test_failure_without_action_stage_never_authorizes_opening_closed_history(
    failure: object,
) -> None:
    """An early process failure is retried as a normal guarded refresh."""
    runner = AsyncMock(side_effect=[failure, "OK\n"])
    await refresh.AppleScriptPersonalRefresher(runner).refresh()
    assert [call.kwargs for call in runner.await_args_list] == [
        {"recover_closed": False},
        {"recover_closed": False},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "NO_WINDOW",
        "TITLE_MISMATCH",
        "NOT_PERSONAL",
        "HISTORY_CLOSED",
        "SEARCH_FILTERED",
        "STRUCTURE_CHANGED",
        "PERMISSION_DENIED",
    ],
)
async def test_known_identity_filter_or_closed_guard_is_not_retried(code: str) -> None:
    """Do not use recovery mode to bypass the user's initial closed/filtered pane."""
    runner = AsyncMock(return_value=f"ERROR\t{code}\n")
    with pytest.raises(refresh.PersonalRefreshError) as caught:
        await refresh.AppleScriptPersonalRefresher(runner).refresh()
    assert caught.value.code == code
    runner.assert_awaited_once_with(recover_closed=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["OK\n\n", "OK\tprivate", "ERROR\tprivate", ""])
async def test_invalid_completion_records_fail_after_one_retry(output: str) -> None:
    """Only the fixed complete record can authorize reading a refreshed panel."""
    runner = AsyncMock(return_value=output)
    with pytest.raises(refresh.PersonalRefreshError) as caught:
        await refresh.AppleScriptPersonalRefresher(runner).refresh()
    assert caught.value.code == "PROCESS_FAILED"
    assert "private" not in str(caught.value)
    assert runner.await_count == 2


@pytest.mark.asyncio
async def test_raw_native_exception_is_absent_from_errors_chains_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native diagnostics may include private content and must never escape."""
    runner = AsyncMock(side_effect=RuntimeError("private-key and private-message"))
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(refresh.PersonalRefreshError) as caught,
    ):
        await refresh.AppleScriptPersonalRefresher(runner).refresh()
    assert runner.await_count == 2
    assert "private" not in "".join(traceback.format_exception(caught.value))
    assert "private" not in caplog.text
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert all(record["msg_id"] is None for record in records)
    assert [record["retry"] for record in records] == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_native_timeout_or_cancel_reaps_owned_refresh_process(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Exercise bounded process cleanup with fake children and no native actions."""
    entered = asyncio.Event()

    async def blocked(*, input: bytes) -> tuple[bytes, bytes]:
        assert b"on refreshHistory(" in input
        entered.set()
        await asyncio.Event().wait()
        return b"", b""

    process = AsyncMock()
    process.communicate.side_effect = blocked
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(refresh.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(refresh, "stop_cli_process", cleanup)
    monkeypatch.setattr(refresh, "REFRESH_TIMEOUT_SECONDS", 0.01)
    refresher = refresh.AppleScriptPersonalRefresher()
    if mode == "timeout":
        with pytest.raises(refresh.PersonalRefreshError) as caught:
            await refresher.refresh()
        assert caught.value.code == "REFRESH_TIMEOUT"
        assert spawn.await_count == cleanup.await_count == 2
    else:
        task = asyncio.create_task(refresher.refresh())
        async with asyncio.timeout(1):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawn.await_count == cleanup.await_count == 1
    for call in spawn.await_args_list:
        assert call.args == (
            refresh.OSASCRIPT_PATH,
            "-",
            PERSONAL_TITLE,
            "refresh",
        )
        assert call.kwargs["stdin"] is asyncio.subprocess.PIPE
        assert call.kwargs["start_new_session"] is refresh.IS_POSIX
    for call in cleanup.await_args_list:
        assert call.kwargs == {"isolated_session": refresh.IS_POSIX}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_cleanup_failure_cannot_swallow_cancel_or_spawn_another_child(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """An uncertain old child must stop recovery even when cleanup itself fails."""
    entered = asyncio.Event()

    async def blocked(*, input: bytes) -> tuple[bytes, bytes]:
        entered.set()
        await asyncio.Event().wait()
        return b"", b""

    process = AsyncMock()
    process.communicate.side_effect = blocked
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock(side_effect=RuntimeError("private cleanup failure"))
    monkeypatch.setattr(refresh.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(refresh, "stop_cli_process", cleanup)
    monkeypatch.setattr(refresh, "REFRESH_TIMEOUT_SECONDS", 0.01)
    refresher = refresh.AppleScriptPersonalRefresher()
    if mode == "timeout":
        with pytest.raises(refresh.PersonalRefreshError) as caught:
            await refresher.refresh()
        assert caught.value.code == "CLEANUP_FAILED"
        assert "private" not in "".join(traceback.format_exception(caught.value))
    else:
        task = asyncio.create_task(refresher.refresh())
        async with asyncio.timeout(1):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert spawn.await_count == cleanup.await_count == 1


@pytest.mark.asyncio
async def test_packaged_runner_supplies_only_fixed_source_and_suppresses_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No caller-provided path, title, executable or source enters the subprocess."""
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"OK\n", b"private native stderr")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(refresh.asyncio, "create_subprocess_exec", spawn)
    runner: refresh.PersonalRefreshRunner = refresh.PersonalAppleScriptRefreshRunner()
    assert await runner(recover_closed=False) == "OK\n"
    source = process.communicate.await_args.kwargs["input"].decode()
    assert source.count("\non run argv\n") == 1
    assert "on validateObservedPersonal(" in source
    assert "on validateObservedHistory(" in source
    assert "on refreshHistory(" in source
    assert "private native stderr" not in source
    spawn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "recover", 1])
async def test_runner_rejects_non_boolean_recovery_without_spawning(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    """The sole runner option is strict; arbitrary commands have no public input."""
    spawn = AsyncMock()
    monkeypatch.setattr(refresh.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(refresh.PersonalRefreshError):
        await refresh.PersonalAppleScriptRefreshRunner()(recover_closed=value)  # type: ignore[arg-type]
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,stdout", [(1, b"private"), (0, b"\xff")])
async def test_process_failure_or_invalid_utf8_never_forward_native_data(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes
) -> None:
    """Discard failed/native output before parsing refresh completion."""
    process = AsyncMock()
    process.returncode = returncode
    process.communicate.return_value = (stdout, b"private stderr")
    monkeypatch.setattr(
        refresh.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(refresh.PersonalRefreshError) as caught:
        await refresh.PersonalAppleScriptRefreshRunner()(recover_closed=False)
    assert caught.value.code == "PROCESS_FAILED"
    assert "private" not in str(caught.value)


def test_refresh_source_contains_only_one_narrow_mutation_and_fresh_direct_lookup() -> (
    None
):
    """The action cannot type, paste, send, run shell commands or press other UI."""
    script = refresh.REFRESH_SCRIPT_PATH.read_text()
    for forbidden in (
        "keystroke",
        "key code",
        "set value",
        'AXValue" to',
        "do shell script",
        "activate",
        'perform action "AXConfirm"',
    ):
        assert forbidden not in script
    assert script.count('perform action "AXPress" of toggleElement') == 1
    assert "my allContents(targetWindow)" not in script
    assert (
        "splitter group 1 of splitter group 1 of splitter group 1 of splitter group 1 of targetWindow"
        in script
    )
    assert "set toolbarButtons to buttons of toolbarParent" in script
    assert "set toggleElement" not in pure_handler("refreshHistory")
    assert "my discoverRefreshToggle(targetWindow)" in pure_handler(
        "waitForRefreshState"
    )
    assert (
        "my validateHistory(my discoverHistory(historyPanel), sidebarTable)" in script
    )
    assert "my validateRefreshIdentity(" in script
    press_guard = pure_handler("pressRefreshToggle")
    assert press_guard.index("my validateHistory(") < press_guard.index(
        'perform action "AXPress"'
    )
    assert press_guard.index("my validateHistory(") < press_guard.index(
        "my discoverRefreshToggle(targetWindow)"
    )


def pure_handler(name: str) -> str:
    """Extract a pure native decision handler without importing any UI operations."""
    source = refresh.REFRESH_SCRIPT_PATH.read_text()
    found = re.search(rf"on {name}\(.*?\nend {name}", source, flags=re.DOTALL)
    assert found is not None
    return found.group()


@pytest.mark.skipif(sys.platform != "darwin", reason="Native pure AppleScript parser")
@pytest.mark.parametrize(
    "error_number,action_started,expected",
    [
        (-1728, False, "REFRESH_FAILED"),
        (-1728, True, "REFRESH_INCOMPLETE"),
        (-10006, True, "REFRESH_INCOMPLETE"),
        (-2702, True, "TITLE_MISMATCH"),
        (-2703, True, "NOT_PERSONAL"),
        (-2704, True, "STRUCTURE_CHANGED"),
        (-2705, True, "HISTORY_CLOSED"),
        (-2706, True, "SEARCH_FILTERED"),
        (-1743, True, "PERMISSION_DENIED"),
        (-2707, False, "REFRESH_INCOMPLETE"),
    ],
)
def test_native_failure_after_validated_close_preserves_recovery_evidence(
    error_number: int, action_started: bool, expected: str
) -> None:
    """A transient AX error after closing Ri must permit its guarded reopen only."""
    code = pure_handler("observedRefreshError") + (
        f"\nreturn observedRefreshError({error_number}, {str(action_started).lower()})"
    )
    completed = subprocess.run(
        [refresh.OSASCRIPT_PATH, "-e", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Native pure AppleScript parser")
@pytest.mark.parametrize(
    "help_value,role,enabled,expected",
    [
        ("关闭聊天记录", "AXButton", True, "open"),
        ("查找聊天记录", "AXButton", True, "closed"),
        ("发送", "AXButton", True, "-2704"),
        ("关闭聊天记录", "AXTextArea", True, "-2704"),
        ("关闭聊天记录", "AXButton", False, "-2704"),
        ("关闭", "AXButton", True, "-2704"),
    ],
)
def test_native_toggle_decision_uses_exact_help_role_and_enabled_without_ui(
    help_value: str, role: str, enabled: bool, expected: str
) -> None:
    """Compile only a pure handler and synthetic values; never contact System Events."""
    code = pure_handler("refreshToggleState") + (
        "\ntry\nreturn refreshToggleState("
        f'"{help_value}", "{role}", {str(enabled).lower()})'
        "\non error messageText number errorNumber\nreturn errorNumber as text\nend try"
    )
    completed = subprocess.run(
        [refresh.OSASCRIPT_PATH, "-e", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Native pure AppleScript parser")
@pytest.mark.parametrize(
    "state,recover,expected",
    [
        ("open", False, "close,open"),
        ("open", True, "close,open"),
        ("closed", False, "-2705"),
        ("closed", True, "open"),
        ("unknown", True, "-2704"),
    ],
)
def test_native_recovery_plan_never_closes_an_already_closed_panel(
    state: str, recover: bool, expected: str
) -> None:
    """A partial prior refresh can resume with one open action and no blind toggles."""
    code = pure_handler("observedRefreshPlan") + (
        f'\ntry\nset resultItems to observedRefreshPlan("{state}", {str(recover).lower()})'
        '\nset AppleScript\'s text item delimiters to ","\nreturn resultItems as text'
        "\non error messageText number errorNumber\nreturn errorNumber as text\nend try"
    )
    completed = subprocess.run(
        [refresh.OSASCRIPT_PATH, "-e", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="Native pure AppleScript parser")
@pytest.mark.parametrize(
    "help_values,expected",
    [
        (["发起群聊", "关闭聊天记录", "更多"], "2"),
        (["更多", "查找聊天记录"], "2"),
        (["查找聊天记录", "更多"], "1"),
        (["关闭聊天记录"], "1"),
        (["关闭聊天记录", "查找聊天记录"], "-2704"),
        (["关闭聊天记录", "关闭聊天记录"], "-2704"),
        (["发送", "关闭"], "-2704"),
        ([], "-2704"),
        (["", "Ri@微信", "发起群聊"], "-2704"),
    ],
)
def test_native_direct_button_lookup_handles_layout_reordering_without_ui(
    help_values: list[str], expected: str
) -> None:
    """Select the current unique help label, never a previous layout's button index."""
    literal = (
        "{"
        + ", ".join(json.dumps(item, ensure_ascii=False) for item in help_values)
        + "}"
    )
    code = pure_handler("observedRefreshToggleIndex") + (
        f"\ntry\nreturn observedRefreshToggleIndex({literal}) as text"
        "\non error messageText number errorNumber\nreturn errorNumber as text\nend try"
    )
    completed = subprocess.run(
        [refresh.OSASCRIPT_PATH, "-e", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout.strip() == expected
