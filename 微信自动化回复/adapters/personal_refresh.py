"""Refresh only the verified Ri history pane before a separate read-only snapshot."""

import asyncio
from pathlib import Path
from typing import Protocol

from adapters.inbox import InboxError
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from adapters.wecom_personal import (
    OSASCRIPT_PATH,
    PERSONAL_TITLE,
    AppleScriptPersonalHistoryReader,
    PersonalHistoryReader,
    PersonalHistorySnapshot,
)
from adapters.wecom_personal import (
    SCRIPT_PATH as SNAPSHOT_SCRIPT_PATH,
)
from src.core.logging import get_logger

REFRESH_SCRIPT_PATH = (
    Path(__file__).with_name("wecom_personal_refresh.applescript").resolve()
)
REFRESH_TIMEOUT_SECONDS = 10.0
MAX_REFRESH_OUTPUT_BYTES = 1024
logger = get_logger(__name__)

_ERRORS = {
    "NO_WINDOW": "企业微信没有唯一可刷新的主窗口。",
    "TITLE_MISMATCH": "当前会话不是已确认的 Ri @微信；已停止刷新。",
    "NOT_PERSONAL": "当前选中项不满足 Ri 个人微信单聊标记；已停止刷新。",
    "HISTORY_CLOSED": "请本人先打开 Ri 单聊的查找聊天记录面板。",
    "SEARCH_FILTERED": "聊天记录搜索或筛选条件不是默认值；已停止刷新。",
    "STRUCTURE_CHANGED": "个人聊天记录刷新控件存在歧义；已停止刷新。",
    "PERMISSION_DENIED": "无法操作已授权的聊天记录刷新控件。",
    "REFRESH_INCOMPLETE": "聊天记录面板刷新尚未完成。",
    "REFRESH_TIMEOUT": "个人聊天记录刷新超时。",
    "PROCESS_FAILED": "个人聊天记录刷新脚本执行失败。",
    "CLEANUP_FAILED": "无法确认刷新子进程已清理；已停止继续操作。",
    "REFRESH_FAILED": "个人聊天记录刷新失败。",
}
_RETRYABLE = {
    "REFRESH_INCOMPLETE",
    "REFRESH_TIMEOUT",
    "PROCESS_FAILED",
    "REFRESH_FAILED",
}


class PersonalRefreshError(InboxError):
    """Expose fixed refresh diagnostics without AX content or raw exceptions."""

    def __init__(self, code: str) -> None:
        """Discard unknown error text instead of displaying private diagnostics."""
        self.code = code if code in _ERRORS else "REFRESH_FAILED"
        super().__init__(_ERRORS[self.code])


class PersonalRefresher(Protocol):
    """Refresh the verified history pane without typing or sending messages."""

    async def refresh(self) -> None:
        """Leave the Ri history open and unfiltered, or raise safely."""
        raise NotImplementedError


class PersonalRefreshRunner(Protocol):
    """Execute the packaged refresh action with a bounded recovery flag only."""

    async def __call__(self, *, recover_closed: bool) -> str:
        """Return a fixed completion/error record without exposing native stderr."""
        raise NotImplementedError


def _script_source() -> bytes:
    """Reuse fixed snapshot guards and replace only their packaged entry point."""
    snapshot = SNAPSHOT_SCRIPT_PATH.read_text(encoding="utf-8")
    parts = snapshot.split("\non run argv\n")
    if len(parts) != 2:
        raise PersonalRefreshError("PROCESS_FAILED")
    refresh = REFRESH_SCRIPT_PATH.read_text(encoding="utf-8")
    return (parts[0] + "\n" + refresh).encode("utf-8")


class PersonalAppleScriptRefreshRunner:
    """Run only the two packaged sources, with no external title or script input."""

    def __init__(self) -> None:
        """Retain a safe cleanup outcome so a timeout cannot start a second child."""
        self.cleanup_failed: bool = False

    async def __call__(self, *, recover_closed: bool) -> str:
        """Use stdin for fixed source and reap the owned process on interruption."""
        if type(recover_closed) is not bool:
            raise PersonalRefreshError("PROCESS_FAILED")
        self.cleanup_failed = False
        source = _script_source()
        process: asyncio.subprocess.Process | None = None
        drained = False
        cancelled = False
        result: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                OSASCRIPT_PATH,
                "-",
                PERSONAL_TITLE,
                "recover" if recover_closed else "refresh",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=IS_POSIX,
            )
            stdout, _stderr = await process.communicate(input=source)
            drained = True
            if process.returncode == 0 and len(stdout) <= MAX_REFRESH_OUTPUT_BYTES:
                try:
                    result = stdout.decode("utf-8")
                except UnicodeError:
                    pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if process is not None and not drained:
                try:
                    await stop_cli_process(process, isolated_session=IS_POSIX)
                except Exception:  # noqa: BLE001 -- Preserve cancellation and suppress native diagnostics.
                    self.cleanup_failed = True
                    logger.error("wecom_personal_refresh_cleanup_failed", msg_id=None)
                    if not cancelled:
                        raise PersonalRefreshError("CLEANUP_FAILED") from None
        if result is None:
            raise PersonalRefreshError("PROCESS_FAILED")
        return result


def _check_result(output: str) -> None:
    """Require one complete allowlisted record before considering refresh done."""
    if output in {"OK", "OK\n"}:
        return
    fields = output.removesuffix("\n").split("\t")
    if len(fields) == 2 and fields[0] == "ERROR" and fields[1] in _ERRORS:
        raise PersonalRefreshError(fields[1])
    raise PersonalRefreshError("PROCESS_FAILED")


class AppleScriptPersonalRefresher:
    """Apply one ten-second attempt and at most one idempotent recovery attempt."""

    def __init__(self, runner: PersonalRefreshRunner | None = None) -> None:
        """Defer native process creation until an explicit refresh request."""
        self._runner = (
            runner if runner is not None else PersonalAppleScriptRefreshRunner()
        )
        self._lock = asyncio.Lock()
        self._recover_closed: bool = False

    async def refresh(self) -> None:
        """Retry uncertain actions once; never retry failed identity/filter guards."""
        async with self._lock:
            failure = PersonalRefreshError("REFRESH_FAILED")
            recover_closed = self._recover_closed
            for attempt in range(2):
                try:
                    async with asyncio.timeout(REFRESH_TIMEOUT_SECONDS):
                        output = await self._runner(recover_closed=recover_closed)
                        _check_result(output)
                    self._recover_closed = False
                    return
                except PersonalRefreshError as exc:
                    failure = PersonalRefreshError(exc.code)
                except TimeoutError:
                    failure = PersonalRefreshError("REFRESH_TIMEOUT")
                except Exception:  # noqa: BLE001 -- Native errors can contain private AX text.
                    failure = PersonalRefreshError("REFRESH_FAILED")
                if getattr(self._runner, "cleanup_failed", False) is True:
                    failure = PersonalRefreshError("CLEANUP_FAILED")
                retry = attempt == 0 and failure.code in _RETRYABLE
                # This code is emitted only after the validated open pane's
                # close/open action has begun. A spawn/read/timeout failure
                # alone must never authorize opening an initially closed pane.
                if failure.code == "REFRESH_INCOMPLETE":
                    self._recover_closed = True
                elif failure.code not in _RETRYABLE:
                    self._recover_closed = False
                recover_closed = self._recover_closed
                logger.warning(
                    "wecom_personal_history_refresh_failed",
                    msg_id=None,
                    code=failure.code,
                    attempt=attempt + 1,
                    retry=retry,
                )
                if not retry:
                    break
            raise failure


class PersonalRefreshingReader:
    """Serialize a refresh and its following read-only Ri history observation."""

    def __init__(
        self,
        refresher: PersonalRefresher | None = None,
        reader: PersonalHistoryReader | None = None,
    ) -> None:
        """Keep refreshing separate from immutable snapshots and message mapping."""
        self._refresher = (
            refresher if refresher is not None else AppleScriptPersonalRefresher()
        )
        self._reader = (
            reader if reader is not None else AppleScriptPersonalHistoryReader()
        )
        self._lock = asyncio.Lock()

    async def read_active_chat(self) -> PersonalHistorySnapshot:
        """Never read a stale panel after a failed or cancelled refresh."""
        async with self._lock:
            await self._refresher.refresh()
            return await self._reader.read_active_chat()
