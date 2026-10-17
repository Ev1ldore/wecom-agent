"""Read the explicitly selected Ri personal history without guessing server identities."""

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from adapters.inbox import InboxError, Msg
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.logging import get_logger

PERSONAL_TITLE = "Ri@微信"
HISTORY_SENDER = "Ri@微信"
PERSONAL_CHAT = "desktop:wechat:Ri"
PERSONAL_IDENTITY = PERSONAL_CHAT
OSASCRIPT_PATH = "/usr/bin/osascript"
SCRIPT_PATH = Path(__file__).with_name("wecom_personal_snapshot.applescript").resolve()
READ_TIMEOUT_SECONDS = 10.0
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_ROW_ITEMS = 100
logger = get_logger(__name__)

_ERRORS = {
    "NO_WINDOW": "企业微信没有可读取的个人聊天窗口。",
    "TITLE_MISMATCH": "当前会话不是已确认的 Ri @微信；已停止读取。",
    "NOT_PERSONAL": "当前选中项不满足 Ri 个人微信单聊标记；已停止读取。",
    "HISTORY_CLOSED": "请本人先打开目标单聊的查找聊天记录面板。",
    "SEARCH_FILTERED": "聊天记录搜索条件不是空值；已停止读取。",
    "STRUCTURE_CHANGED": "个人聊天记录界面结构变化或存在歧义；已停止读取。",
    "PERMISSION_DENIED": "无法读取企业微信辅助功能；请检查本人授予的读取权限。",
    "MALFORMED_OUTPUT": "个人聊天记录快照不完整或格式无效。",
    "ROW_AMBIGUOUS": "同一聊天记录行的作者或正文存在歧义；已停止读取。",
    "TIME_UNVERIFIED": "目标消息行没有唯一完整的原始月日时分标签；已停止读取。",
    "READ_TIMEOUT": "个人聊天记录只读快照超时。",
    "PROCESS_FAILED": "个人聊天记录只读脚本执行失败。",
    "READ_FAILED": "个人聊天记录只读快照失败。",
}


class PersonalReadError(InboxError):
    """Expose only an allowlisted code and fixed explanation without raw AX data."""

    def __init__(self, code: str) -> None:
        """Discard arbitrary subprocess and accessibility diagnostics."""
        self.code: str = code if code in _ERRORS else "READ_FAILED"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class PersonalHistoryRow:
    """Preserve sender buttons, text areas and static labels within one AX row."""

    senders: tuple[str, ...]
    texts: tuple[str, ...]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class PersonalHistorySnapshot:
    """Describe one locally verified, open and unfiltered personal history pane."""

    title: str
    personal_verified: bool
    history_open: bool
    search_text: str
    rows: tuple[PersonalHistoryRow, ...]


class PersonalHistoryReader(Protocol):
    """Expose read-only observations without navigation or outbound messages."""

    async def read_active_chat(self) -> PersonalHistorySnapshot:
        """Return the selected Ri history or fail without changing the UI."""
        raise NotImplementedError


class PersonalCommandRunner(Protocol):
    """Execute the fixed personal-history reader with one explicit title argument."""

    async def __call__(self, argv: list[str]) -> str:
        """Return framed stdout without forwarding stderr or raw errors."""
        raise NotImplementedError


class PersonalAppleScriptRunner:
    """Run only the packaged read-only script and reap its owned child on interruption."""

    async def __call__(self, argv: list[str]) -> str:
        """Refuse arbitrary scripts, shell commands and other target titles."""
        if argv != [OSASCRIPT_PATH, str(SCRIPT_PATH), PERSONAL_TITLE]:
            raise PersonalReadError("PROCESS_FAILED")
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
            if len(stdout) > MAX_OUTPUT_BYTES:
                failure = "MALFORMED_OUTPUT"
            elif process.returncode == 0:
                try:
                    output = stdout.decode("utf-8")
                except UnicodeError:
                    failure = "MALFORMED_OUTPUT"
        finally:
            if process is not None and not drained:
                await stop_cli_process(process, isolated_session=IS_POSIX)
        if output is None:
            raise PersonalReadError(failure)
        return output


def _decode(value: str) -> str:
    """Require canonical base64 and UTF-8 before accepting native text fields."""
    decoded: str | None = None
    try:
        data = base64.b64decode(value, validate=True)
        if base64.b64encode(data).decode("ascii") == value:
            decoded = data.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        pass
    if decoded is None:
        raise PersonalReadError("MALFORMED_OUTPUT")
    return decoded


def _count(value: str, maximum: int) -> int:
    """Accept bounded canonical nonnegative decimal counts only."""
    if not re.fullmatch(r"0|[1-9][0-9]*", value) or len(value) > 6:
        raise PersonalReadError("MALFORMED_OUTPUT")
    number = int(value)
    if number > maximum:
        raise PersonalReadError("MALFORMED_OUTPUT")
    return number


def _validate_identity(snapshot: PersonalHistorySnapshot) -> None:
    """Enforce the same gates for native and injected snapshot readers."""
    if snapshot.title != PERSONAL_TITLE:
        raise PersonalReadError("TITLE_MISMATCH")
    if snapshot.personal_verified is not True:
        raise PersonalReadError("NOT_PERSONAL")
    if snapshot.history_open is not True:
        raise PersonalReadError("HISTORY_CLOSED")
    if snapshot.search_text != "":
        raise PersonalReadError("SEARCH_FILTERED")


def parse_personal_snapshot(output: str) -> PersonalHistorySnapshot:
    """Reject partial or extended framing before returning any personal text."""
    if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise PersonalReadError("MALFORMED_OUTPUT")
    lines = output.splitlines()
    if len(lines) == 1 and lines[0].startswith("ERROR\t"):
        fields = lines[0].split("\t")
        if len(fields) == 2 and fields[1] in _ERRORS:
            raise PersonalReadError(fields[1])
        raise PersonalReadError("MALFORMED_OUTPUT")
    position = 0

    def take(tag: str, fields: int) -> list[str]:
        """Consume exactly the expected record and its declared field count."""
        nonlocal position
        if position >= len(lines):
            raise PersonalReadError("MALFORMED_OUTPUT")
        values = lines[position].split("\t")
        position += 1
        if len(values) != fields or values[0] != tag:
            raise PersonalReadError("MALFORMED_OUTPUT")
        return values[1:]

    if take("WECOM_PERSONAL_HISTORY", 2) != ["1"]:
        raise PersonalReadError("MALFORMED_OUTPUT")
    title = _decode(take("TITLE", 2)[0])
    personal = take("PERSONAL", 2)[0]
    history = take("HISTORY", 2)[0]
    search = _decode(take("SEARCH", 2)[0])
    if personal not in {"0", "1"} or history not in {"0", "1"}:
        raise PersonalReadError("MALFORMED_OUTPUT")
    metadata = PersonalHistorySnapshot(
        title, personal == "1", history == "1", search, ()
    )
    _validate_identity(metadata)
    rows: list[PersonalHistoryRow] = []
    while position < len(lines) and lines[position].startswith("ROW\t"):
        if len(rows) >= MAX_ROWS:
            raise PersonalReadError("MALFORMED_OUTPUT")
        sender_count, text_count, label_count = take("ROW", 4)
        senders = tuple(
            _decode(take("SENDER", 2)[0])
            for _ in range(_count(sender_count, MAX_ROW_ITEMS))
        )
        texts = tuple(
            _decode(take("TEXT", 2)[0])
            for _ in range(_count(text_count, MAX_ROW_ITEMS))
        )
        labels = tuple(
            _decode(take("LABEL", 2)[0])
            for _ in range(_count(label_count, MAX_ROW_ITEMS))
        )
        take("ENDROW", 1)
        rows.append(PersonalHistoryRow(senders, texts, labels))
    if _count(take("END", 2)[0], MAX_ROWS) != len(rows) or position != len(lines):
        raise PersonalReadError("MALFORMED_OUTPUT")
    return PersonalHistorySnapshot(
        title, personal == "1", history == "1", search, tuple(rows)
    )


class AppleScriptPersonalHistoryReader:
    """Observe the already opened Ri history pane with two bounded attempts."""

    def __init__(self, runner: PersonalCommandRunner | None = None) -> None:
        """Construct no subprocess until an explicit read is requested."""
        self._runner = runner if runner is not None else PersonalAppleScriptRunner()

    async def read_active_chat(self) -> PersonalHistorySnapshot:
        """Retry a failed read once; propagate cancellation after process cleanup."""
        failure = PersonalReadError("READ_FAILED")
        for attempt in range(2):
            try:
                async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                    output = await self._runner(
                        [OSASCRIPT_PATH, str(SCRIPT_PATH), PERSONAL_TITLE]
                    )
                    return parse_personal_snapshot(output)
            except PersonalReadError as exc:
                failure = PersonalReadError(exc.code)
            except TimeoutError:
                failure = PersonalReadError("READ_TIMEOUT")
            except Exception:  # noqa: BLE001 -- AX text and diagnostics stay private.
                failure = PersonalReadError("READ_FAILED")
            logger.warning(
                "wecom_personal_history_read_failed",
                msg_id=None,
                code=failure.code,
                attempt=attempt + 1,
                retry=attempt == 0,
            )
        raise failure


def _minute_label(labels: tuple[str, ...]) -> str:
    """Validate one original MM/DD HH:MM and an optional matching date separator."""
    times: list[str] = []
    dates: list[str] = []
    for label in labels:
        if re.fullmatch(r"[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}", label):
            times.append(label)
        elif re.fullmatch(r"[0-9]{2}/[0-9]{2}", label):
            dates.append(label)
        else:
            raise PersonalReadError("TIME_UNVERIFIED")
    if len(times) != 1 or len(dates) > 1 or (dates and dates[0] != times[0][:5]):
        raise PersonalReadError("TIME_UNVERIFIED")
    value = times[0]
    month, day = int(value[:2]), int(value[3:5])
    hour, minute = int(value[6:8]), int(value[9:11])
    month_days = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not (
        1 <= month <= 12
        and 1 <= day <= month_days[month - 1]
        and 0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        raise PersonalReadError("TIME_UNVERIFIED")
    return value


class PersonalHistoryInbox:
    """Map only exact Ri-authored rows to a local identity with minute precision."""

    def __init__(self, reader: PersonalHistoryReader | None = None) -> None:
        """Do not infer a server user ID or open/navigate any conversation."""
        self._reader = (
            reader if reader is not None else AppleScriptPersonalHistoryReader()
        )

    async def poll_unread(self) -> list[Msg]:
        """Read visible history rows; durable deduplication belongs to the loop."""
        snapshot = await self._reader.read_active_chat()
        _validate_identity(snapshot)
        messages: list[Msg] = []
        for row in snapshot.rows:
            if not row.senders:
                if row.labels:
                    raise PersonalReadError("ROW_AMBIGUOUS")
                # System-only rows have no observed sender or timestamp.
                continue
            if len(row.senders) != 1:
                raise PersonalReadError("ROW_AMBIGUOUS")
            if row.senders[0] != HISTORY_SENDER:
                continue
            if len(row.texts) != 1:
                raise PersonalReadError("ROW_AMBIGUOUS")
            time_display = _minute_label(row.labels)
            messages.append(
                Msg(
                    chat=PERSONAL_IDENTITY,
                    sender=PERSONAL_IDENTITY,
                    content=row.texts[0],
                    ts=None,
                    time_precision="minute",
                    time_display=time_display,
                    chat_type="single",
                    is_self=False,
                )
            )
        return messages
