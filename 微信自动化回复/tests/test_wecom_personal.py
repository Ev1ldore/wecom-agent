"""Verify personal-history reads and minute-precision mapping without native UI access."""

import asyncio
import base64
import json
import logging
import subprocess
import sys
import traceback
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from adapters import wecom_personal as personal
from adapters.inbox import Inbox
from adapters.wecom_personal import (
    AppleScriptPersonalHistoryReader,
    PersonalAppleScriptRunner,
    PersonalHistoryInbox,
    PersonalHistoryReader,
    PersonalHistoryRow,
    PersonalHistorySnapshot,
    PersonalReadError,
)


def encoded(text: str) -> str:
    """Encode a protocol field without losing Unicode, tabs or line breaks."""
    return base64.b64encode(text.encode()).decode()


def row(
    *,
    senders: tuple[str, ...] = ("Ri@微信",),
    texts: tuple[str, ...] = ("你好",),
    labels: tuple[str, ...] = ("10/16 12:11",),
) -> PersonalHistoryRow:
    """Build one observed AX row with no inherited fields."""
    return PersonalHistoryRow(senders, texts, labels)


def snapshot(
    rows: tuple[PersonalHistoryRow, ...] | None = None,
) -> PersonalHistorySnapshot:
    """Describe a locally verified, open, unfiltered history pane."""
    return PersonalHistorySnapshot(
        personal.PERSONAL_TITLE, True, True, "", (row(),) if rows is None else rows
    )


def framed(value: PersonalHistorySnapshot | None = None) -> str:
    """Serialize fixture data using the native script's public framing contract."""
    value = snapshot() if value is None else value
    lines = [
        "WECOM_PERSONAL_HISTORY\t1",
        f"TITLE\t{encoded(value.title)}",
        f"PERSONAL\t{int(value.personal_verified)}",
        f"HISTORY\t{int(value.history_open)}",
        f"SEARCH\t{encoded(value.search_text)}",
    ]
    for item in value.rows:
        lines.append(f"ROW\t{len(item.senders)}\t{len(item.texts)}\t{len(item.labels)}")
        lines.extend(f"SENDER\t{encoded(text)}" for text in item.senders)
        lines.extend(f"TEXT\t{encoded(text)}" for text in item.texts)
        lines.extend(f"LABEL\t{encoded(text)}" for text in item.labels)
        lines.append("ENDROW")
    lines.append(f"END\t{len(value.rows)}")
    return "\n".join(lines)


@pytest.mark.asyncio
async def test_reader_protocol_preserves_exact_per_row_native_text() -> None:
    """Keep all original text instead of flattening or constructing epoch times."""
    original = snapshot(
        (
            row(
                texts=("完整中文\n第二行\t原文\u200b",), labels=("10/16 11:07", "10/16")
            ),
            row(senders=(), texts=("系统提示",), labels=()),
            row(),
        )
    )
    runner = AsyncMock(return_value=framed(original))
    reader: PersonalHistoryReader = AppleScriptPersonalHistoryReader(runner)

    actual = await reader.read_active_chat()

    assert actual == original
    runner.assert_awaited_once_with(
        [personal.OSASCRIPT_PATH, str(personal.SCRIPT_PATH), "Ri@微信"]
    )


@pytest.mark.asyncio
async def test_inbox_maps_only_exact_ri_rows_with_original_minute_display() -> None:
    """Ignore owner/system output and never invent server identity or seconds."""
    reader = AsyncMock()
    reader.read_active_chat.return_value = snapshot(
        (
            row(texts=("已通过好友验证\n原文",), labels=("10/16 11:07", "10/16")),
            row(senders=(), texts=("系统提示",), labels=()),
            row(senders=("李承云",), texts=("本机账号输出",)),
            row(senders=("Ri @微信",), texts=("不能猜作者别名",)),
            row(),
        )
    )
    inbox: Inbox = PersonalHistoryInbox(reader)

    messages = await inbox.poll_unread()

    assert len(messages) == 2
    assert [item.content for item in messages] == ["已通过好友验证\n原文", "你好"]
    assert [item.time_display for item in messages] == ["10/16 11:07", "10/16 12:11"]
    for item in messages:
        assert item.chat == item.sender == "desktop:wechat:Ri"
        assert item.ts is None and item.time_precision == "minute"
        assert item.chat_type == "single" and item.is_self is False
    assert personal.PERSONAL_CHAT == personal.PERSONAL_IDENTITY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"title": "Other@微信"}, "TITLE_MISMATCH"),
        ({"title": "Ri @微信"}, "TITLE_MISMATCH"),
        ({"personal_verified": False}, "NOT_PERSONAL"),
        ({"history_open": False}, "HISTORY_CLOSED"),
        ({"search_text": "你好"}, "SEARCH_FILTERED"),
        ({"search_text": " "}, "SEARCH_FILTERED"),
    ],
)
async def test_identity_and_history_gates_also_apply_to_injected_snapshots(
    changes: dict[str, object], code: str
) -> None:
    """An injected reader cannot bypass wrong-title, bot/group or search rejection."""
    invalid = replace(snapshot(), **changes)
    reader = AsyncMock()
    reader.read_active_chat.return_value = invalid
    with pytest.raises(PersonalReadError) as caught:
        await PersonalHistoryInbox(reader).poll_unread()
    assert caught.value.code == code
    with pytest.raises(PersonalReadError) as parsed:
        personal.parse_personal_snapshot(framed(invalid))
    assert parsed.value.code == code


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: text.rsplit("\n", 1)[0],
        lambda text: text + "\nEXTRA\tprivate-text",
        lambda text: text.replace("END\t1", "END\t2"),
        lambda text: text.replace("ROW\t1\t1\t1", "ROW\t2\t1\t1"),
        lambda text: text.replace("ROW\t1\t1\t1", "ROW\t-1\t1\t1"),
        lambda text: text.replace("ROW\t1\t1\t1", "ROW\t01\t1\t1"),
        lambda text: text.replace(
            "WECOM_PERSONAL_HISTORY\t1", "WECOM_PERSONAL_HISTORY\t2"
        ),
        lambda text: text.replace("PERSONAL\t1", "PERSONAL\ttrue"),
        lambda text: text.replace(f"TEXT\t{encoded('你好')}", "TEXT\t!!"),
        lambda text: text.replace(f"TEXT\t{encoded('你好')}", "TEXT\t/w=="),
        lambda text: text.replace(f"TEXT\t{encoded('你好')}", "TEXT\teA"),
        lambda text: "ERROR\tprivate-diagnostic",
    ],
)
def test_partial_extended_or_noncanonical_snapshot_is_rejected(transform) -> None:
    """Require the exact declared records before accepting any personal body."""
    with pytest.raises(PersonalReadError) as caught:
        personal.parse_personal_snapshot(transform(framed()))
    assert caught.value.code == "MALFORMED_OUTPUT"
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        (),
        ("12:11",),
        ("10/16",),
        ("10/16 12:11", "10/16 12:12"),
        ("10/16 12:11", "10/17"),
        ("2026-10-16 12:11",),
        ("10/16 12:11:00",),
        ("10/16 24:00",),
        ("02/30 12:11",),
        ("00/16 12:11",),
    ],
)
async def test_missing_ambiguous_or_invalid_original_time_refuses_whole_poll(
    labels: tuple[str, ...],
) -> None:
    """Never inherit the previous row's date/time or substitute receive time."""
    reader = AsyncMock()
    reader.read_active_chat.return_value = snapshot((row(), row(labels=labels)))
    with pytest.raises(PersonalReadError) as caught:
        await PersonalHistoryInbox(reader).poll_unread()
    assert caught.value.code == "TIME_UNVERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        row(senders=("Ri@微信", "Ri@微信")),
        row(senders=("Ri@微信", "李承云")),
        row(texts=()),
        row(texts=("第一段", "第二段")),
        row(senders=(), labels=("10/16 12:11",)),
    ],
)
async def test_sender_body_and_time_are_never_paired_across_ambiguous_rows(
    invalid: PersonalHistoryRow,
) -> None:
    """Require one sender and one body within the same target message row."""
    reader = AsyncMock()
    reader.read_active_chat.return_value = snapshot((row(), invalid))
    with pytest.raises(PersonalReadError) as caught:
        await PersonalHistoryInbox(reader).poll_unread()
    assert caught.value.code == "ROW_AMBIGUOUS"


@pytest.mark.asyncio
async def test_empty_history_has_no_messages_or_invented_time() -> None:
    """An empty history table is a valid read-only observation."""
    reader = AsyncMock()
    reader.read_active_chat.return_value = snapshot(())
    assert await PersonalHistoryInbox(reader).poll_unread() == []
    assert personal.parse_personal_snapshot(framed(snapshot(()))).rows == ()


@pytest.mark.asyncio
async def test_reader_retries_once_then_returns_complete_snapshot() -> None:
    """A failed first observation may recover without a third invocation."""
    runner = AsyncMock(side_effect=[RuntimeError("private AX diagnostic"), framed()])
    assert (
        await AppleScriptPersonalHistoryReader(runner).read_active_chat() == snapshot()
    )
    assert runner.await_count == 2


@pytest.mark.asyncio
async def test_reader_failure_hides_raw_diagnostics_and_message_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep private native errors out of public exceptions, chains and logs."""
    runner = AsyncMock(side_effect=RuntimeError("private AX diagnostic and body"))
    with caplog.at_level(logging.WARNING), pytest.raises(PersonalReadError) as caught:
        await AppleScriptPersonalHistoryReader(runner).read_active_chat()
    assert runner.await_count == 2
    assert caught.value.code == "READ_FAILED"
    rendered = caplog.text + "".join(traceback.format_exception(caught.value))
    assert "private AX diagnostic and body" not in rendered
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_injected_runner_timeout_is_bounded_and_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the total attempt budget even to an injected transport."""

    async def blocked(argv: list[str]) -> str:
        """Wait for timeout cancellation without any native process."""
        await asyncio.Event().wait()
        return framed()

    runner = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(personal, "READ_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(1):
        with pytest.raises(PersonalReadError) as caught:
            await AppleScriptPersonalHistoryReader(runner).read_active_chat()
    assert caught.value.code == "READ_TIMEOUT"
    assert runner.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_native_timeout_or_cancel_cleans_owned_process_without_ui(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Use fake children to verify bounded retries and cancellation propagation."""
    entered = asyncio.Event()

    async def blocked_communicate() -> tuple[bytes, bytes]:
        """Signal process creation and await cancellation."""
        entered.set()
        await asyncio.Event().wait()
        return b"", b""

    process = AsyncMock()
    process.communicate.side_effect = blocked_communicate
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(personal.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(personal, "stop_cli_process", cleanup)
    reader = AppleScriptPersonalHistoryReader()
    if mode == "timeout":
        monkeypatch.setattr(personal, "READ_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(PersonalReadError) as caught:
            await reader.read_active_chat()
        assert caught.value.code == "READ_TIMEOUT"
        assert spawn.await_count == cleanup.await_count == 2
    else:
        task = asyncio.create_task(reader.read_active_chat())
        async with asyncio.timeout(1):
            await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawn.await_count == cleanup.await_count == 1
    for call in spawn.await_args_list:
        assert call.args == (
            personal.OSASCRIPT_PATH,
            str(personal.SCRIPT_PATH),
            "Ri@微信",
        )
        assert call.kwargs["stdin"] is asyncio.subprocess.DEVNULL
        assert call.kwargs["start_new_session"] is personal.IS_POSIX
    for call in cleanup.await_args_list:
        assert call.kwargs == {"isolated_session": personal.IS_POSIX}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "private-command"],
        [personal.OSASCRIPT_PATH, "/private/other.applescript", "Ri@微信"],
        [personal.OSASCRIPT_PATH, str(personal.SCRIPT_PATH), "Other@微信"],
        [personal.OSASCRIPT_PATH, str(personal.SCRIPT_PATH), "Ri@微信", "extra"],
    ],
)
async def test_runner_rejects_non_allowlisted_scripts_and_titles(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Refuse unexpected commands before creating any process."""
    spawn = AsyncMock()
    monkeypatch.setattr(personal.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PersonalReadError) as caught:
        await PersonalAppleScriptRunner()(argv)
    assert caught.value.code == "PROCESS_FAILED"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_runner_preserves_framing_and_ignores_private_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real runner boundary returns only successful framed stdout."""
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (framed().encode(), b"private stderr")
    monkeypatch.setattr(
        personal.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    assert await AppleScriptPersonalHistoryReader().read_active_chat() == snapshot()


def test_snapshot_size_limit_and_script_read_only_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep oversized private output and native mutation primitives outside this reader."""
    monkeypatch.setattr(personal, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(PersonalReadError) as caught:
        personal.parse_personal_snapshot(framed())
    assert caught.value.code == "MALFORMED_OUTPUT"
    script = personal.SCRIPT_PATH.read_text()
    for forbidden in (
        "activate",
        "click ",
        "keystroke",
        "key code",
        "AXPress",
        "do shell script",
    ):
        assert forbidden not in script
    assert "AXSelected" in script and "validateObservedPersonal" in script
    assert 'closeLabel is not "关闭"' in script and '"查找聊天记录"' in script
    assert "validatePersonal(sidebarTable)" in script
    assert script.count("my validatePersonal(sidebarTable)") == 2


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
@pytest.mark.parametrize(
    ("titles", "accepted"),
    [
        (["Ri@微信"], True),
        (["Ri", "@微信"], True),
        (["Ri", "@微信", "BOT"], False),
        (["Ri ", "@微信"], False),
        (["Other", "@微信"], False),
        (["@微信", "Ri"], False),
        (["ri", "@微信"], False),
        (["Ri@微信", ""], False),
        ([], False),
        (["Ri", "@微信群聊"], False),
    ],
)
def test_native_title_validator_accepts_only_two_observed_forms_without_ui(
    titles: list[str], accepted: bool
) -> None:
    """Execute only the pure title handler, without System Events or live UI reads."""
    source = personal.SCRIPT_PATH.read_text()
    start = source.index("on validateObservedTitle(")
    end = source.index("end validateObservedTitle", start) + len(
        "end validateObservedTitle"
    )
    handler = source[start:end]
    literal = (
        "{" + ", ".join(json.dumps(value, ensure_ascii=False) for value in titles) + "}"
    )
    script = handler + "\nreturn my validateObservedTitle(" + literal + ', "Ri@微信")\n'
    result = subprocess.run(
        [personal.OSASCRIPT_PATH, "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "Ri@微信\n"
    else:
        assert result.returncode != 0
        assert result.stdout == ""


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
@pytest.mark.parametrize(
    ("static_values", "areas", "selected_count", "accepted"),
    [
        (["Ri", "你好", "昨天", "@微信"], [], 1, True),
        (["Ri", "任意\n预览\tBOT", "10:30", "@微信"], [], 1, True),
        (["Other", "你好", "昨天", "@微信"], [], 1, False),
        (["ri", "你好", "昨天", "@微信"], [], 1, False),
        (["@微信", "你好", "昨天", "Ri"], [], 1, False),
        (["Ri", "你好", "昨天", "BOT"], [], 1, False),
        (["Ri", "你好", "", "@微信"], [], 1, False),
        (["Ri", "你好", "昨天"], [], 1, False),
        (["Ri", "Ri", "你好", "昨天", "@微信"], [], 1, False),
        (["Ri 你好", "昨天 @微信"], [], 1, False),
        (["Ri", "你好", "昨天", "@微信"], ["BOT"], 1, False),
        (["Ri", "你好", "昨天", "@微信"], ["private ambiguous area"], 1, False),
        (["Ri", "你好", "昨天", "@微信"], [], 2, False),
        (["Ri", "你好", "昨天", "@微信"], [], 0, False),
    ],
)
def test_native_selected_personal_validator_uses_exact_observed_static_sequence(
    static_values: list[str],
    areas: list[str],
    selected_count: int,
    accepted: bool,
) -> None:
    """Execute only synthetic identity checks, without System Events or UI reads."""
    source = personal.SCRIPT_PATH.read_text()
    start = source.index("on validateObservedPersonal(")
    end = source.index("end validateObservedPersonal", start) + len(
        "end validateObservedPersonal"
    )
    handler = source[start:end]

    def literal(values: list[str]) -> str:
        """Encode fixed fixture strings as an AppleScript list argument."""
        return (
            "{"
            + ", ".join(json.dumps(value, ensure_ascii=False) for value in values)
            + "}"
        )

    script = (
        handler
        + "\nreturn my validateObservedPersonal("
        + literal(static_values)
        + ", "
        + literal(areas)
        + ", "
        + str(selected_count)
        + ")\n"
    )
    result = subprocess.run(
        [personal.OSASCRIPT_PATH, "-e", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "true\n"
    else:
        assert result.returncode != 0
        assert result.stdout == ""


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
@pytest.mark.parametrize(
    ("title", "help_text", "description", "expected"),
    [
        ("Ri@微信", "other help", "按钮", "Ri@微信"),
        ("", "关闭聊天记录", "按钮", "关闭聊天记录"),
        ("", "关闭", "按钮", "关闭"),
        ("", "", "关闭聊天记录", "关闭聊天记录"),
        ("", "", "按钮", ""),
        ("", "", "", ""),
        (None, "关闭聊天记录", "按钮", "关闭聊天记录"),
        (None, None, "按钮", ""),
        (None, None, None, ""),
    ],
)
def test_native_label_selector_prefers_title_then_help_and_ignores_generic_description(
    title: str | None,
    help_text: str | None,
    description: str | None,
    expected: str,
) -> None:
    """Exercise the actual label priority without querying live AX attributes."""
    source = personal.SCRIPT_PATH.read_text()
    start = source.index("on observedLabel(")
    end = source.index("end observedLabel", start) + len("end observedLabel")
    handler = source[start:end]
    arguments = ", ".join(
        "missing value" if item is None else json.dumps(item, ensure_ascii=False)
        for item in (title, help_text, description)
    )
    result = subprocess.run(
        [
            personal.OSASCRIPT_PATH,
            "-e",
            handler + f"\nreturn my observedLabel({arguments})\n",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected + "\n"


def test_history_structure_discovery_is_cached_and_revalidated_without_second_window_scan() -> (
    None
):
    """Keep final guards while avoiding another traversal of unrelated sidebar nodes."""
    script = personal.SCRIPT_PATH.read_text()
    assert "my allContents(targetWindow)" not in script
    assert "my allContents(targetPanel)" not in script
    assert script.count("my discoverHistory(historyPanel)") == 1
    assert script.count("my validateHistory(historyStructure, sidebarTable)") == 2
    assert "my validateTitle(targetWindow, expectedTitle)" in script
    assert script.count("my validatePersonal(sidebarTable)") == 2
    assert "count rows of historyTable" in script
    assert "set sidebarArea to scroll area 1 of targetPanel" in script
    assert "set sidebarTable to table 1 of sidebarArea" in script
    assert "set historyPanel to splitter group 1 of targetPanel" in script


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
@pytest.mark.parametrize(
    ("names", "expected_index"),
    [
        (["Window", "企业微信"], 2),
        (["企业微信", "Window"], 1),
        (["企业微信"], 1),
        (["Window", "企业微信", "tooltip"], 2),
        ([], None),
        (["Window"], None),
        (["企业微信", "企业微信"], None),
        (["企业微信 ", "Window"], None),
    ],
)
def test_native_main_window_selector_never_assumes_first_popup_is_the_chat(
    names: list[str], expected_index: int | None
) -> None:
    """Select one exact main-window name using pure synthetic inventory data."""
    source = personal.SCRIPT_PATH.read_text()
    start = source.index("on mainWindowIndex(")
    end = source.index("end mainWindowIndex", start) + len("end mainWindowIndex")
    handler = source[start:end]
    literal = (
        "{" + ", ".join(json.dumps(value, ensure_ascii=False) for value in names) + "}"
    )
    result = subprocess.run(
        [
            personal.OSASCRIPT_PATH,
            "-e",
            handler + f"\nreturn my mainWindowIndex({literal})\n",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if expected_index is None:
        assert result.returncode != 0
        assert result.stdout == ""
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{expected_index}\n"


def test_native_main_window_inventory_is_validated_before_and_after_body_reads() -> (
    None
):
    """Keep popup content out of scope and fail closed when window inventory changes."""
    script = personal.SCRIPT_PATH.read_text()
    assert 'set targetWindow to window "企业微信" of targetProcess' in script
    assert "set targetWindow to window 1 of targetProcess" not in script
    assert (
        script.count(
            "my validateWindowInventory(targetProcess, targetWindow, initialWindowCount)"
        )
        == 2
    )
    assert "(count windowNames) is not expectedWindowCount" in script


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
@pytest.mark.parametrize(
    ("heading", "fields", "scrolls", "buttons", "close", "query", "checks", "accepted"),
    [
        (["查找聊天记录"], 1, 2, 3, "关闭", "", [1, 0, 0, 0, 0], True),
        ([], 1, 2, 3, "关闭", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录", "extra"], 1, 2, 3, "关闭", "", [1, 0, 0, 0, 0], False),
        (["other"], 1, 2, 3, "关闭", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 2, 2, 3, "关闭", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 1, 1, 3, "关闭", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 1, 2, 4, "关闭", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 1, 2, 3, "按钮", "", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 1, 2, 3, "关闭", "private search", [1, 0, 0, 0, 0], False),
        (["查找聊天记录"], 1, 2, 3, "关闭", "", [0, 1, 0, 0, 0], False),
        (["查找聊天记录"], 1, 2, 3, "关闭", "", [1, 0, 0, 0], False),
    ],
)
def test_native_focused_history_gates_keep_query_filter_heading_and_close_checks(
    heading: list[str],
    fields: int,
    scrolls: int,
    buttons: int,
    close: str,
    query: str,
    checks: list[int],
    accepted: bool,
) -> None:
    """Execute only the focused panel's pure guards without reading any UI."""
    source = personal.SCRIPT_PATH.read_text()
    start = source.index("on validateObservedHistory(")
    end = source.index("end validateObservedHistory", start) + len(
        "end validateObservedHistory"
    )
    handler = source[start:end]
    heading_literal = (
        "{" + ", ".join(json.dumps(text, ensure_ascii=False) for text in heading) + "}"
    )
    check_literal = "{" + ", ".join(str(value) for value in checks) + "}"
    call = (
        f"\nreturn my validateObservedHistory({heading_literal}, {fields}, {scrolls}, {buttons}, "
        + json.dumps(close, ensure_ascii=False)
        + ", "
        + json.dumps(query, ensure_ascii=False)
        + f", {check_literal})\n"
    )
    result = subprocess.run(
        [personal.OSASCRIPT_PATH, "-e", handler + call],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if accepted:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "true\n"
    else:
        assert result.returncode != 0
        assert result.stdout == ""
