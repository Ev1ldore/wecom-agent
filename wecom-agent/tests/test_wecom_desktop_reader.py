"""Verify desktop snapshots with synthetic script stdout and fake owned children."""

import asyncio
import base64
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from adapters import wecom_cli
from adapters import wecom_desktop_reader as module
from adapters.wecom_desktop_reader import (
    AppleScriptCommandRunner,
    AppleScriptSnapshotReader,
    DesktopReadError,
    Rect,
    SnapshotReader,
)

TITLE = "已核实的测试机器人"
BODY = "中文正文\n第二行包含\t制表符和 <原样内容>"


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript runtime is macOS-only"
)
def test_native_protocol_header_executes_without_reading_ui(tmp_path: Path) -> None:
    """Execute the real Foundation/header code to catch reserved-word assignments."""
    helpers = module.SCRIPT_PATH.read_text().split("on validateTitle(", 1)[0]
    script = tmp_path / "header.applescript"
    script.write_text(
        helpers
        + '\non run\nset headerLines to my protocolHeader("测试标题", '
        + "{-606, -632, 771, 613})\n"
        + "set AppleScript's text item delimiters to linefeed\n"
        + "return headerLines as text\nend run\n"
    )
    result = subprocess.run(
        [module.OSASCRIPT_PATH, str(script)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "WECOM_DESKTOP_SNAPSHOT\t1",
        "TITLE\t" + base64.b64encode("测试标题".encode()).decode(),
        "BOT\t1",
        "TABLE\t-606\t-632\t771\t613",
    ]


def encoded(text: str) -> str:
    """Encode only fixed synthetic content using the script's wire format."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def snapshot_output(title: str = TITLE, bot: str = "1") -> str:
    """Represent all text areas in one row, with independent unparsed time labels."""
    return "\n".join(
        [
            "WECOM_DESKTOP_SNAPSHOT\t1",
            f"TITLE\t{encoded(title)}",
            f"BOT\t{bot}",
            "TABLE\t-606\t-632\t771\t613",
            "ROW\t2\t2",
            f"TEXT\t{encoded(BODY)}\t-6\t-584\t144\t22",
            f"TEXT\t{encoded('另一个正文区域')}\t-580\t-540\t200\t40",
            f"TIME\t{encoded('10:17')}",
            "TIME\t" + encoded("原始标签\t不解析日期或秒"),
            "ENDROW",
            "END\t1",
        ]
    )


class FakeRunner:
    """Observe arguments while returning only fixture stdout or fixture failures."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> str:
        """Never execute a script, read a desktop or access the network."""
        self.calls.append(argv)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.asyncio
async def test_snapshot_preserves_all_text_rectangles_and_unparsed_display_times() -> (
    None
):
    """Do not flatten text areas, normalize Unicode or invent message timestamps."""
    runner = FakeRunner([snapshot_output() + "\n"])
    reader: SnapshotReader = AppleScriptSnapshotReader(TITLE, runner)

    snapshot = await reader.read_active_chat()

    assert snapshot.title == TITLE and snapshot.bot_tag is True
    assert snapshot.table_rect == Rect(-606.0, -632.0, 771.0, 613.0)
    assert len(snapshot.rows) == 1
    row = snapshot.rows[0]
    assert row.texts == [BODY, "另一个正文区域"]
    assert row.rects == [Rect(-6.0, -584.0, 144.0, 22.0), Rect(-580, -540, 200, 40)]
    assert len(row.texts) == len(row.rects)
    assert row.display_times == ["10:17", "原始标签\t不解析日期或秒"]
    assert not hasattr(row, "ts") and not hasattr(row, "sender")
    assert runner.calls == [[module.OSASCRIPT_PATH, str(module.SCRIPT_PATH), TITLE]]


@pytest.mark.asyncio
async def test_title_argument_is_separate_from_script_not_shell_interpolated() -> None:
    """Pass quotes, tabs and shell metacharacters as one literal verified-title argv."""
    title = '测试"机器人\t$(do-not-run)'
    runner = FakeRunner([snapshot_output(title)])

    assert (
        await AppleScriptSnapshotReader(title, runner).read_active_chat()
    ).title == title

    assert runner.calls[0] == ["/usr/bin/osascript", str(module.SCRIPT_PATH), title]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "code"),
    [
        (snapshot_output("同名之外的其他会话"), "TITLE_MISMATCH"),
        (snapshot_output(bot="0"), "NOT_BOT"),
        ("ERROR\tNO_WINDOW", "NO_WINDOW"),
        ("ERROR\tPERMISSION_DENIED", "PERMISSION_DENIED"),
        ("ERROR\tSTRUCTURE_CHANGED", "STRUCTURE_CHANGED"),
    ],
)
async def test_wrong_target_missing_bot_window_and_permissions_fail_safely(
    output: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Return no partial data and retry only the same read operation once."""
    runner = FakeRunner([output, output])
    with caplog.at_level(logging.WARNING), pytest.raises(DesktopReadError) as caught:
        await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()

    assert caught.value.code == code
    assert len(runner.calls) == 2 and runner.calls[0] == runner.calls[1]
    combined = str(caught.value) + caplog.text
    assert TITLE not in combined and BODY not in combined
    assert "同名之外的其他会话" not in combined
    assert caught.value.__context__ is None


def malformed_outputs() -> list[str]:
    """Describe protocol corruption using fixed, non-private sample values."""
    valid = snapshot_output()
    return [
        "",
        "WECOM_DESKTOP_SNAPSHOT\t1",
        valid.rsplit("\n", 1)[0],
        valid + "\nEXTRA\tprivate-body",
        valid + "\n\n",
        valid.replace("WECOM_DESKTOP_SNAPSHOT\t1", "WECOM_DESKTOP_SNAPSHOT\t2"),
        valid.replace("ROW\t2\t2", "ROW\t3\t2"),
        valid.replace("ROW\t2\t2", "ROW\t-1\t2"),
        valid.replace("ROW\t2\t2", "ROW\t02\t2"),
        valid.replace("ROW\t2\t2", "ROW\t10001\t2"),
        valid.replace("END\t1", "END\t2"),
        valid.replace("ENDROW\n", ""),
        valid.replace(f"TEXT\t{encoded(BODY)}", "TEXT\t%%%"),
        valid.replace(f"TEXT\t{encoded(BODY)}", "TEXT\t/w=="),
        valid.replace(f"TEXT\t{encoded(BODY)}", "TEXT\tYQ==="),
        valid.replace("TABLE\t-606\t-632\t771\t613", "TABLE\t-606\t-632\t0\t613"),
        valid.replace("TABLE\t-606\t-632\t771\t613", "TABLE\t-606\t-632\t-1\t613"),
        valid.replace("TABLE\t-606\t-632\t771\t613", "TABLE\tNaN\t-632\t771\t613"),
        valid.replace("TABLE\t-606\t-632\t771\t613", "TABLE\t-606\t-inf\t771\t613"),
        valid.replace("\t-6\t-584\t144\t22", "\t-6\t-584\t144"),
        valid.replace("\t-6\t-584\t144\t22", "\t-6\t-584\t144\t-22"),
        valid.replace("BOT\t1", "BOT\ttrue"),
        "ERROR\tprivate-raw-error",
        "ERROR\tNO_WINDOW\n" + valid,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("output", malformed_outputs())
async def test_truncated_malformed_and_extended_outputs_are_rejected(
    output: str,
) -> None:
    """Require exact record order, count, encoding and finite geometry."""
    runner = FakeRunner([output, output])

    with pytest.raises(DesktopReadError) as caught:
        await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()

    assert caught.value.code == "MALFORMED_OUTPUT"
    assert len(runner.calls) == 2
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_empty_table_and_empty_text_area_are_preserved() -> None:
    """An empty view is distinct from a truncated or failed snapshot."""
    header = "\n".join(snapshot_output().splitlines()[:4])
    runner = FakeRunner([header + "\nEND\t0"])
    assert (
        await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()
    ).rows == []
    output = header + "\nROW\t1\t0\nTEXT\t\t-10\t-20\t0\t0\nENDROW\nEND\t1"
    runner = FakeRunner([output])
    snapshot = await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()
    assert snapshot.rows[0].texts == [""]
    assert snapshot.rows[0].display_times == []
    assert snapshot.rows[0].rects == [Rect(-10, -20, 0, 0)]


@pytest.mark.asyncio
async def test_safe_retry_recovers_without_repeating_a_third_time() -> None:
    """One transient failure may recover with exactly one additional read."""
    runner = FakeRunner(
        [RuntimeError("private stderr and private body"), snapshot_output()]
    )

    assert (
        await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()
    ).title == TITLE
    assert len(runner.calls) == 2


@pytest.mark.asyncio
async def test_injected_runner_timeout_is_bounded_and_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the read budget even to an injected runner that never returns."""
    calls = 0

    async def blocked(argv: list[str]) -> str:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        raise AssertionError("Must time out")

    monkeypatch.setattr(module, "READ_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(1):
        with pytest.raises(DesktopReadError) as caught:
            await AppleScriptSnapshotReader(TITLE, blocked).read_active_chat()
    assert caught.value.code == "READ_TIMEOUT" and calls == 2


@pytest.mark.asyncio
async def test_output_size_limit_never_exposes_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject unexpectedly large snapshots before decoding any message records."""
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 10)
    runner = FakeRunner([snapshot_output(), snapshot_output()])
    with pytest.raises(DesktopReadError) as caught:
        await AppleScriptSnapshotReader(TITLE, runner).read_active_chat()
    assert caught.value.code == "MALFORMED_OUTPUT"


@pytest.mark.parametrize("title", ["", " \n "])
def test_constructor_requires_an_explicit_target_title(title: str) -> None:
    """Refuse an unbound reader before a runner can touch any process."""
    with pytest.raises(DesktopReadError) as caught:
        AppleScriptSnapshotReader(title, FakeRunner([]))
    assert caught.value.code == "TARGET_REQUIRED"


class FakeChild:
    """Support owned-process cleanup without spawning or signalling a real process."""

    registry: ClassVar[dict[int, "FakeChild"]] = {}

    def __init__(
        self, *, block: bool = False, output: bytes | None = None, status: int = 0
    ) -> None:
        self.pid = 950000 + len(self.registry)
        self.registry[self.pid] = self
        self.returncode: int | None = None
        self.block = block
        self.output = output if output is not None else snapshot_output().encode()
        self.status = status
        self.killed = False
        self.communications = 0
        self.waits = 0
        self.started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communications += 1
        self.started.set()
        if self.block and not self.killed:
            await asyncio.Event().wait()
        self.returncode = -9 if self.killed else self.status
        return self.output, b"private raw stderr and private message body"

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waits += 1
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def fake_groups(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Intercept all process-group signals and permit only this test's fake PIDs."""
    FakeChild.registry = {}
    signals: list[tuple[int, int]] = []

    def kill_group(pid: int, signal: int) -> None:
        assert pid in FakeChild.registry
        signals.append((pid, signal))
        FakeChild.registry[pid].kill()

    monkeypatch.setattr(module, "IS_POSIX", True)
    monkeypatch.setattr(wecom_cli, "IS_POSIX", True)
    monkeypatch.setattr(wecom_cli.os, "killpg", kill_group, raising=False)
    return signals


@pytest.mark.asyncio
async def test_default_runner_executes_only_fixed_packaged_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a literal argv and a private process session without any shell command."""
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        calls.append((argv, kwargs))
        return FakeChild()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    snapshot = await AppleScriptSnapshotReader(TITLE).read_active_chat()
    assert snapshot.title == TITLE
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv == ("/usr/bin/osascript", str(module.SCRIPT_PATH), TITLE)
    assert module.SCRIPT_PATH.is_file()
    assert options == {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "stdin": asyncio.subprocess.DEVNULL,
        "start_new_session": module.IS_POSIX,
    }


@pytest.mark.asyncio
async def test_process_timeout_kills_and_reaps_each_owned_process_group(
    monkeypatch: pytest.MonkeyPatch, fake_groups: list[tuple[int, int]]
) -> None:
    """A timed-out read retries once only after its child group has been reaped."""
    children: list[FakeChild] = []

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        child = FakeChild(block=True)
        children.append(child)
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(DesktopReadError) as caught:
        await AppleScriptSnapshotReader(TITLE).read_active_chat()

    assert caught.value.code == "READ_TIMEOUT"
    assert len(children) == len(fake_groups) == 2
    assert all(
        child.killed and child.communications == 2 and child.waits == 1
        for child in children
    )


@pytest.mark.asyncio
async def test_cancellation_cleans_owned_group_and_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch, fake_groups: list[tuple[int, int]]
) -> None:
    """Never leave an osascript descendant running when its caller cancels."""
    child = FakeChild(block=True)
    creations = 0

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        nonlocal creations
        creations += 1
        return child

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(AppleScriptSnapshotReader(TITLE).read_active_chat())
    await asyncio.wait_for(child.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert creations == len(fake_groups) == 1
    assert child.killed and child.communications == 2 and child.waits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "status", "code"),
    [(b"private raw output", 1, "PROCESS_FAILED"), (b"\xff", 0, "MALFORMED_OUTPUT")],
)
async def test_raw_stderr_and_invalid_stdout_never_escape(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    output: bytes,
    status: int,
    code: str,
) -> None:
    """Treat execution diagnostics as private, not as safe public error messages."""

    async def spawn(*argv: str, **kwargs: Any) -> FakeChild:
        return FakeChild(output=output, status=status)

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    with caplog.at_level(logging.WARNING), pytest.raises(DesktopReadError) as caught:
        await AppleScriptSnapshotReader(TITLE).read_active_chat()

    assert caught.value.code == code
    assert "private" not in str(caught.value) + caplog.text
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_command_runner_refuses_other_scripts_and_shell_like_commands() -> None:
    """An injected command cannot turn this runner into a generic UI executor."""
    runner = AppleScriptCommandRunner()
    for argv in (
        ["/bin/sh", "-c", "private command"],
        [
            "/usr/bin/osascript",
            "-e",
            'tell application "System Events" to keystroke "x"',
        ],
        ["/usr/bin/osascript", "/tmp/other-script.applescript", TITLE],
    ):
        with pytest.raises(DesktopReadError):
            await runner(argv)


def test_packaged_script_contains_only_read_operations_and_identity_gates() -> None:
    """Keep mutation primitives out of the fixed script and retain both safety gates."""
    script = module.SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'bundle identifier is "com.tencent.WeWorkMac"' in script
    assert 'attribute "AXSelected"' in script
    assert "character id 8203" in script
    assert 'cleanText is "BOT"' in script
    assert script.count("my validateTitle(targetWindow, expectedTitle)") == 2
    assert script.index("if not (my hasSelectedBot(sidebarTable))") < script.index(
        "set messageRows to rows of messageTable"
    )
    assert 'if childRole is "AXTextArea"' in script
    for forbidden in (
        "activate",
        "keystroke",
        "key code",
        "perform action",
        "do shell script",
        "click ",
        "scroll ",
    ):
        assert forbidden not in script.lower()
