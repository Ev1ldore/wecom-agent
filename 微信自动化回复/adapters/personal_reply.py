"""Guard one personal original-chat send, disabled unless explicitly enabled."""

import asyncio
import base64
from pathlib import Path
from typing import Protocol

from structlog.contextvars import get_contextvars

from adapters.inbox import InboxError
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from adapters.wecom_personal import OSASCRIPT_PATH, PERSONAL_IDENTITY, PERSONAL_TITLE
from adapters.wecom_personal import SCRIPT_PATH as SNAPSHOT_SCRIPT_PATH
from src.core.logging import get_logger

REPLY_SCRIPT_PATH = (
    Path(__file__).with_name("wecom_personal_reply.applescript").resolve()
)
SEND_SCRIPT_PATH = Path(__file__).with_name("wecom_personal_send.applescript").resolve()
PREFLIGHT_TIMEOUT_SECONDS = 10.0
SEND_TIMEOUT_SECONDS = 15.0
MAX_PREFLIGHT_OUTPUT_BYTES = 1024
logger = get_logger(__name__)

_ERRORS = {
    "TARGET_MISMATCH": "回复预检仅支持已确认的 Ri 个人单聊。",
    "EMPTY_CONTENT": "回复草稿不能为空。",
    "NO_WINDOW": "企业微信没有唯一可预检的主窗口。",
    "TITLE_MISMATCH": "当前会话不是已确认的 Ri@微信；回复预检已停止。",
    "NOT_PERSONAL": "当前选中项不是已确认的 Ri 外部单聊；回复预检已停止。",
    "STRUCTURE_CHANGED": "个人聊天窗口结构发生变化；回复预检已停止。",
    "COMPOSER_UNAVAILABLE": "没有唯一可编辑的聊天输入框；回复预检已停止。",
    "COMPOSER_AMBIGUOUS": "发现多个可编辑输入框；回复预检已停止。",
    "COMPOSER_NOT_EMPTY": "聊天输入框已有内容；回复预检不会覆盖现有草稿。",
    "COMPOSER_CHANGED": "聊天输入框在预检期间发生变化；回复预检已停止。",
    "PERMISSION_DENIED": "无法读取已授权的个人聊天输入框。",
    "PREFLIGHT_TIMEOUT": "个人回复通道只读预检超时。",
    "PROCESS_FAILED": "个人回复通道只读预检脚本失败。",
    "CLEANUP_FAILED": "无法确认预检子进程已清理；已停止继续预检。",
    "PREFLIGHT_FAILED": "个人回复通道只读预检失败。",
}
_RETRYABLE = {"PREFLIGHT_TIMEOUT", "PROCESS_FAILED", "PREFLIGHT_FAILED"}
_SEND_ERRORS = {
    **_ERRORS,
    "FOCUS_FAILED": "无法确认 Ri 原会话输入框获得焦点。",
    "DRAFT_WRITE_FAILED": "原会话草稿写入未能确认。",
    "DRAFT_MISMATCH": "原会话输入内容与待发送正文不一致。",
    "RECEIPT_UNCONFIRMED": "没有确认到新增的匹配出站气泡。",
    "SEND_TIMEOUT": "个人发送阶段超时。",
    "SEND_CANCELLED": "个人发送阶段被取消。",
    "SEND_FAILED": "个人发送阶段未能完成确认。",
}


class PersonalReplyError(InboxError):
    """Expose fixed preflight errors without draft content or native diagnostics."""

    def __init__(self, code: str) -> None:
        """Keep only allowlisted machine codes and static explanations."""
        self.code: str = code if code in _ERRORS else "PREFLIGHT_FAILED"
        super().__init__(_ERRORS[self.code])


class RealSendingDisabled(RuntimeError):
    """Reject all actual sending independently of preflight success."""

    def __init__(self) -> None:
        """Report the current delivery boundary without accepting private details."""
        super().__init__("本人账号真实发送未启用；当前通道仅支持只读预检。")


class PersonalSendError(InboxError):
    """Keep a safe send cause and whether any native write may have occurred."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        """Discard raw native diagnostics and mark uncertain outcomes explicitly."""
        self.code: str = code if code in _SEND_ERRORS else "SEND_FAILED"
        self.outcome_unknown: bool = outcome_unknown
        message = _SEND_ERRORS[self.code]
        if outcome_unknown:
            message += " 结果未知，禁止自动重发。"
        super().__init__(message)


class PersonalSendUnknown(PersonalSendError):
    """Retain a claim when native writing or Return may already have happened."""

    def __init__(self, code: str = "SEND_FAILED") -> None:
        """Expose an allowlisted cause without suggesting a retry."""
        super().__init__(code, outcome_unknown=True)


class PersonalSendCancelled(asyncio.CancelledError):
    """Preserve cancellation while warning the caller not to replay the claim."""

    def __init__(self) -> None:
        """Keep a fixed cancellation cause for a potentially started native send."""
        self.code: str = "SEND_CANCELLED"
        self.outcome_unknown: bool = True
        super().__init__("个人发送被取消；结果未知，禁止自动重发。")


class ReplyPreview(Protocol):
    """Check a draft's original-chat channel without entering or sending text."""

    async def prepare_reply(self, user_id: str, content: str) -> None:
        """Validate the fixed recipient and empty composer, or raise safely."""
        raise NotImplementedError


class PersonalReplyRunner(Protocol):
    """Run only the packaged native preflight with no caller-provided arguments."""

    async def __call__(self) -> str:
        """Return one fixed OK/error record without private native stderr."""
        raise NotImplementedError


class PersonalSendRunner(Protocol):
    """Execute one guarded native send and return a fixed receipt/error record."""

    async def __call__(self, content: str) -> str:
        """Attempt at most one Return; never retry a native send internally."""
        raise NotImplementedError


def _script_source() -> bytes:
    """Reuse read-only snapshot identity guards with a separate fixed entry point."""
    snapshot = SNAPSHOT_SCRIPT_PATH.read_text(encoding="utf-8")
    parts = snapshot.split("\non run argv\n")
    if len(parts) != 2:
        raise PersonalReplyError("PROCESS_FAILED")
    entry = REPLY_SCRIPT_PATH.read_text(encoding="utf-8")
    return (parts[0] + "\n" + entry).encode("utf-8")


def _send_script_source(content: str) -> bytes:
    """Transport draft data as strict base64 inside stdin, never raw code or argv."""
    if not isinstance(content, str) or not content.strip():
        raise PersonalSendError("EMPTY_CONTENT")
    snapshot = SNAPSHOT_SCRIPT_PATH.read_text(encoding="utf-8").split("\non run argv\n")
    preflight = REPLY_SCRIPT_PATH.read_text(encoding="utf-8").split("\non run argv\n")
    if len(snapshot) != 2 or len(preflight) != 2:
        raise PersonalSendError("PROCESS_FAILED")
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    entry = SEND_SCRIPT_PATH.read_text(encoding="utf-8")
    return (
        snapshot[0]
        + "\n"
        + preflight[0]
        + f'\nproperty encodedReply : "{encoded}"\n'
        + entry
    ).encode("utf-8")


class PersonalAppleScriptReplyRunner:
    """Observe only fixed native controls and reap the owned child on interruption."""

    def __init__(self) -> None:
        """Keep cleanup uncertainty visible to the retry gate."""
        self.cleanup_failed: bool = False

    async def __call__(self) -> str:
        """Pass no reply content, dynamic title or dynamic script to the subprocess."""
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
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=IS_POSIX,
            )
            stdout, _stderr = await process.communicate(input=source)
            drained = True
            if process.returncode == 0 and len(stdout) <= MAX_PREFLIGHT_OUTPUT_BYTES:
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
                except Exception:  # noqa: BLE001 -- Native cleanup diagnostics stay private.
                    self.cleanup_failed = True
                    logger.error(
                        "personal_reply_cleanup_failed",
                        msg_id=get_contextvars().get("msg_id"),
                    )
                    if not cancelled:
                        raise PersonalReplyError("CLEANUP_FAILED") from None
        if result is None:
            raise PersonalReplyError("PROCESS_FAILED")
        return result


class PersonalAppleScriptSendRunner:
    """Own a single native-send child with bounded cancellation cleanup."""

    def __init__(self) -> None:
        """Create no native process before an explicit live send request."""
        self.cleanup_failed: bool = False

    async def __call__(self, content: str) -> str:
        """Run once and treat unreadable results after spawn as unknown outcomes."""
        self.cleanup_failed = False
        source = _send_script_source(content)
        process: asyncio.subprocess.Process | None = None
        drained = False
        cancelled = False
        result: str | None = None
        failed = False
        try:
            process = await asyncio.create_subprocess_exec(
                OSASCRIPT_PATH,
                "-",
                PERSONAL_TITLE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=IS_POSIX,
            )
            stdout, _stderr = await process.communicate(input=source)
            drained = True
            if process.returncode == 0 and len(stdout) <= MAX_PREFLIGHT_OUTPUT_BYTES:
                try:
                    result = stdout.decode("utf-8")
                except UnicodeError:
                    pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:  # noqa: BLE001 -- Do not retain body-bearing native exceptions.
            failed = True
        finally:
            if process is not None and not drained:
                try:
                    await stop_cli_process(process, isolated_session=IS_POSIX)
                except Exception:  # noqa: BLE001 -- Preserve cancellation without private cleanup errors.
                    self.cleanup_failed = True
                    if not cancelled:
                        raise PersonalSendUnknown("CLEANUP_FAILED") from None
        if failed and process is None:
            raise PersonalSendError("PROCESS_FAILED")
        if result is None:
            raise PersonalSendUnknown("SEND_FAILED")
        return result


def _check_result(output: str) -> None:
    """Require exactly one allowlisted native record before confirming preflight."""
    if output in {"OK", "OK\n"}:
        return
    fields = output.removesuffix("\n").split("\t")
    if len(fields) == 2 and fields[0] == "ERROR" and fields[1] in _ERRORS:
        raise PersonalReplyError(fields[1])
    raise PersonalReplyError("PROCESS_FAILED")


def _check_send_result(output: str) -> None:
    """Distinguish a local outgoing receipt, a pre-write failure and uncertainty."""
    if output in {"SENT", "SENT\n"}:
        return
    fields = output.removesuffix("\n").split("\t")
    if len(fields) == 2 and fields[1] in _SEND_ERRORS:
        if fields[0] == "UNKNOWN":
            raise PersonalSendUnknown(fields[1])
        if fields[0] == "ERROR":
            raise PersonalSendError(fields[1])
    raise PersonalSendUnknown("SEND_FAILED")


class PersonalDesktopReplyChannel:
    """Preflight one original personal chat and allow explicit single-attempt sends.

    Qt may omit AXEnabled. Its absence remains unknown and is allowed only for
    preflight; explicit False is rejected. Live sending is disabled by
    default. A successful live send proves only a newly observed local outgoing
    bubble, never remote delivery. The caller owns persistent one-time claims.
    """

    def __init__(
        self,
        runner: PersonalReplyRunner | None = None,
        *,
        live_enabled: bool = False,
        send_runner: PersonalSendRunner | None = None,
    ) -> None:
        """Construct no native process until a validated draft requests preflight."""
        self._runner: PersonalReplyRunner = (
            runner if runner is not None else PersonalAppleScriptReplyRunner()
        )
        if type(live_enabled) is not bool:
            raise ValueError("live_enabled must be an explicit boolean")
        self._live_enabled: bool = live_enabled
        self._send_runner: PersonalSendRunner = (
            send_runner if send_runner is not None else PersonalAppleScriptSendRunner()
        )
        self._lock = asyncio.Lock()

    @staticmethod
    def _validate_draft(user_id: str, content: str) -> None:
        """Keep recipient and draft validation identical for preview and live mode."""
        if user_id != PERSONAL_IDENTITY:
            raise PersonalReplyError("TARGET_MISMATCH")
        if not isinstance(content, str) or not content.strip():
            raise PersonalReplyError("EMPTY_CONTENT")

    async def prepare_reply(self, user_id: str, content: str) -> None:
        """Apply two bounded attempts only for transient read/process failures."""
        self._validate_draft(user_id, content)
        async with self._lock:
            await self._prepare_locked()

    async def _prepare_locked(self) -> None:
        """Share preflight under the caller-held lock without recursive locking."""
        failure = PersonalReplyError("PREFLIGHT_FAILED")
        for attempt in range(2):
            try:
                async with asyncio.timeout(PREFLIGHT_TIMEOUT_SECONDS):
                    output = await self._runner()
                    _check_result(output)
                logger.info(
                    "personal_reply_preflight_ready",
                    msg_id=get_contextvars().get("msg_id"),
                    stage="preflight",
                    dry_run=not self._live_enabled,
                    enabled_unknown_allowed=True,
                    delivery_verified=False,
                )
                return
            except PersonalReplyError as exc:
                failure = PersonalReplyError(exc.code)
            except TimeoutError:
                failure = PersonalReplyError("PREFLIGHT_TIMEOUT")
            except Exception:  # noqa: BLE001 -- Do not expose native or draft diagnostics.
                failure = PersonalReplyError("PREFLIGHT_FAILED")
            if getattr(self._runner, "cleanup_failed", False) is True:
                failure = PersonalReplyError("CLEANUP_FAILED")
            retry = attempt == 0 and failure.code in _RETRYABLE
            logger.warning(
                "personal_reply_preflight_failed",
                msg_id=get_contextvars().get("msg_id"),
                code=failure.code,
                attempt=attempt + 1,
                retry=retry,
            )
            if not retry:
                break
        raise failure

    async def send_text(self, user_id: str, content: str) -> None:
        """Preflight then attempt one native send only when explicitly enabled."""
        if not self._live_enabled:
            raise RealSendingDisabled()
        self._validate_draft(user_id, content)
        async with self._lock:
            await self._prepare_locked()
            failure: PersonalSendError | None = None
            cancelled = False
            try:
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    output = await self._send_runner(content)
                    _check_send_result(output)
            except asyncio.CancelledError:
                cancelled = True
            except PersonalSendError as exc:
                failure = (
                    PersonalSendUnknown(exc.code)
                    if exc.outcome_unknown
                    else PersonalSendError(exc.code)
                )
            except TimeoutError:
                failure = PersonalSendUnknown("SEND_TIMEOUT")
            except Exception:  # noqa: BLE001 -- Every unclassified send failure is uncertain.
                failure = PersonalSendUnknown("SEND_FAILED")
            if cancelled:
                logger.error(
                    "personal_send_cancelled",
                    msg_id=get_contextvars().get("msg_id"),
                    outcome_unknown=True,
                    retry=False,
                )
                raise PersonalSendCancelled()
            if getattr(self._send_runner, "cleanup_failed", False) is True:
                failure = PersonalSendUnknown("CLEANUP_FAILED")
            if failure is not None:
                logger.error(
                    "personal_send_failed",
                    msg_id=get_contextvars().get("msg_id"),
                    code=failure.code,
                    outcome_unknown=failure.outcome_unknown,
                    retry=False,
                )
                raise failure
            logger.info(
                "personal_send_local_receipt",
                msg_id=get_contextvars().get("msg_id"),
                stage="local_outgoing_bubble",
                remote_delivery_verified=False,
            )
