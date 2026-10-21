"""Read one explicitly selected bot chat without author or timestamp inference."""

import asyncio
import base64
import binascii
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from adapters.inbox import InboxError
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.logging import get_logger

OSASCRIPT_PATH = "/usr/bin/osascript"
SCRIPT_PATH = Path(__file__).with_name("wecom_snapshot.applescript").resolve()
READ_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_ROW_ITEMS = 10000
logger = get_logger(__name__)

_ERRORS = {
    "TARGET_REQUIRED": "桌面读取必须配置明确的目标会话标题。",
    "NO_WINDOW": "企业微信没有可读取窗口；请用户保持已验证目标单聊窗口可见。",
    "PERMISSION_DENIED": "无法读取企业微信辅助功能；请检查本人授予的读取权限。",
    "TITLE_MISMATCH": "当前企业微信会话不是已验证目标；已停止读取。",
    "NOT_BOT": "当前选中会话没有已验证的 BOT 标记；已停止读取。",
    "STRUCTURE_CHANGED": "企业微信辅助功能结构发生变化；已停止读取。",
    "MALFORMED_OUTPUT": "桌面快照输出不完整或格式无效；已停止读取。",
    "PROCESS_FAILED": "桌面只读脚本执行失败；未输出原始诊断。",
    "READ_TIMEOUT": "桌面只读快照超时；请检查窗口和辅助功能状态。",
    "READ_FAILED": "桌面只读快照失败；请检查窗口和辅助功能状态。",
}


class DesktopReadError(InboxError):
    """Expose only a fixed safe error code and a credential-free explanation."""

    def __init__(self, code: str) -> None:
        """Refuse arbitrary subprocess messages as public error descriptions."""
        self.code = code if code in _ERRORS else "READ_FAILED"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class Rect:
    """Keep observed global AX coordinates, including negative screen positions."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class DesktopMessageRow:
    """Preserve text areas, paired rectangles and unparsed static-text values."""

    texts: list[str]
    display_times: list[str]
    rects: list[Rect]


@dataclass(frozen=True)
class DesktopSnapshot:
    """Describe only the gated message table, without guessing users or seconds."""

    title: str
    bot_tag: bool
    table_rect: Rect
    rows: list[DesktopMessageRow]


class SnapshotReader(Protocol):
    """Expose one read-only observation of the currently selected verified chat."""

    async def read_active_chat(self) -> DesktopSnapshot:
        """Return a complete gated snapshot or raise a safe error."""
        raise NotImplementedError


class SnapshotCommandRunner(Protocol):
    """Execute the fixed local script without shell interpolation or UI actions."""

    async def __call__(self, argv: list[str]) -> str:
        """Return protocol stdout only, excluding raw subprocess diagnostics."""
        raise NotImplementedError


def _command_valid(argv: list[str]) -> bool:
    """Allow only the packaged read-only script and one explicit title argument."""
    return (
        len(argv) == 3
        and argv[:2] == [OSASCRIPT_PATH, str(SCRIPT_PATH)]
        and isinstance(argv[2], str)
        and bool(argv[2].strip())
    )


class AppleScriptCommandRunner:
    """Run an owned osascript child and reap its private group after interruption."""

    async def __call__(self, argv: list[str]) -> str:
        """Read stdout without forwarding stderr, and clean up cancellation safely."""
        if not _command_valid(argv):
            raise DesktopReadError("PROCESS_FAILED")
        process: asyncio.subprocess.Process | None = None
        drained = False
        output: str | None = None
        failure = "PROCESS_FAILED"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=IS_POSIX,
            )
            stdout, _stderr = await process.communicate()
            drained = True
            if process.returncode == 0 and len(stdout) <= MAX_OUTPUT_BYTES:
                try:
                    output = stdout.decode("utf-8")
                except UnicodeError:
                    failure = "MALFORMED_OUTPUT"
            elif len(stdout) > MAX_OUTPUT_BYTES:
                failure = "MALFORMED_OUTPUT"
        finally:
            if process is not None and not drained:
                await stop_cli_process(process, isolated_session=IS_POSIX)
        if output is None:
            raise DesktopReadError(failure)
        return output


def _decode(value: str) -> str:
    """Require canonical base64 and strict UTF-8 without echoing encoded data."""
    decoded: str | None = None
    try:
        data = base64.b64decode(value, validate=True)
        if base64.b64encode(data).decode("ascii") == value:
            decoded = data.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        pass
    if decoded is None:
        raise DesktopReadError("MALFORMED_OUTPUT")
    return decoded


def _count(value: str, maximum: int) -> int:
    """Accept bounded canonical nonnegative counts, never floating-point sizes."""
    if not re.fullmatch(r"0|[1-9][0-9]*", value) or len(value) > 6:
        raise DesktopReadError("MALFORMED_OUTPUT")
    number = int(value)
    if number > maximum:
        raise DesktopReadError("MALFORMED_OUTPUT")
    return number


def _rect(values: list[str], *, table: bool = False) -> Rect:
    """Validate finite measurements while preserving truthful negative positions."""
    valid = len(values) == 4
    numbers: list[float] = []
    if valid:
        try:
            numbers = [float(value) for value in values]
        except (ValueError, OverflowError):
            valid = False
    if not valid or not all(math.isfinite(number) for number in numbers):
        raise DesktopReadError("MALFORMED_OUTPUT")
    rect = Rect(*numbers)
    if (
        rect.width < 0
        or rect.height < 0
        or (table and not (rect.width and rect.height))
    ):
        raise DesktopReadError("MALFORMED_OUTPUT")
    return rect


def _parse_snapshot(output: str, expected_title: str) -> DesktopSnapshot:
    """Reject partial, reordered or extended protocols before returning any body."""
    if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise DesktopReadError("MALFORMED_OUTPUT")
    lines = output.splitlines()
    if len(lines) == 1 and lines[0].startswith("ERROR\t"):
        fields = lines[0].split("\t")
        if len(fields) != 2 or fields[1] not in _ERRORS:
            raise DesktopReadError("MALFORMED_OUTPUT")
        raise DesktopReadError(fields[1])
    position = 0

    def take(tag: str, fields: int) -> list[str]:
        """Consume exactly one record of the required type and arity."""
        nonlocal position
        if position >= len(lines):
            raise DesktopReadError("MALFORMED_OUTPUT")
        record = lines[position].split("\t")
        position += 1
        if len(record) != fields or record[0] != tag:
            raise DesktopReadError("MALFORMED_OUTPUT")
        return record[1:]

    if take("WECOM_DESKTOP_SNAPSHOT", 2) != ["1"]:
        raise DesktopReadError("MALFORMED_OUTPUT")
    title = _decode(take("TITLE", 2)[0])
    if title != expected_title:
        raise DesktopReadError("TITLE_MISMATCH")
    bot = take("BOT", 2)[0]
    if bot != "1":
        raise DesktopReadError("NOT_BOT" if bot == "0" else "MALFORMED_OUTPUT")
    table_rect = _rect(take("TABLE", 5), table=True)
    rows: list[DesktopMessageRow] = []
    while position < len(lines) and lines[position].startswith("ROW\t"):
        if len(rows) >= MAX_ROWS:
            raise DesktopReadError("MALFORMED_OUTPUT")
        text_count, time_count = take("ROW", 3)
        texts: list[str] = []
        rects: list[Rect] = []
        times: list[str] = []
        for _ in range(_count(text_count, MAX_ROW_ITEMS)):
            text, *coordinates = take("TEXT", 6)
            texts.append(_decode(text))
            rects.append(_rect(coordinates))
        for _ in range(_count(time_count, MAX_ROW_ITEMS)):
            times.append(_decode(take("TIME", 2)[0]))
        take("ENDROW", 1)
        rows.append(DesktopMessageRow(texts, times, rects))
    if _count(take("END", 2)[0], MAX_ROWS) != len(rows) or position != len(lines):
        raise DesktopReadError("MALFORMED_OUTPUT")
    return DesktopSnapshot(title, True, table_rect, rows)


class AppleScriptSnapshotReader:
    """Read only an already selected target bot chat with a ten-second attempt budget."""

    def __init__(
        self, expected_title: str, runner: SnapshotCommandRunner | None = None
    ) -> None:
        """Pin the operator-verified title without deriving any user identity."""
        if not isinstance(expected_title, str) or not expected_title.strip():
            raise DesktopReadError("TARGET_REQUIRED")
        self._expected_title = expected_title
        self._runner = runner if runner is not None else AppleScriptCommandRunner()

    async def read_active_chat(self) -> DesktopSnapshot:
        """Retry one failed read, excluding cancellation and any UI mutations."""
        failure = DesktopReadError("READ_FAILED")
        for attempt in range(2):
            try:
                async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                    output = await self._runner(
                        [OSASCRIPT_PATH, str(SCRIPT_PATH), self._expected_title]
                    )
                    return _parse_snapshot(output, self._expected_title)
            except DesktopReadError as exc:
                failure = DesktopReadError(exc.code)
            except TimeoutError:
                failure = DesktopReadError("READ_TIMEOUT")
            except Exception:  # noqa: BLE001 -- External diagnostics are never public.
                failure = DesktopReadError("READ_FAILED")
            logger.warning(
                "wecom_desktop_snapshot_failed",
                msg_id=None,
                code=failure.code,
                attempt=attempt + 1,
                retry=attempt == 0,
            )
        raise failure
