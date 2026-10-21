"""Guard one personal-WeChat original-window send driven by OCR verification.

WeChat's desktop client does not expose its composer through the
accessibility APIs, so this channel sends through the real UI: activate
the verified window, paste the draft from the clipboard, verify with OCR
that the composer actually contains the draft, click Send once, then
verify with a fresh OCR read that a matching own bubble appeared.  Any
uncertain outcome is reported as unknown and never retried automatically.
Live sending is disabled unless explicitly enabled.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from structlog.contextvars import get_contextvars

from adapters.inbox import InboxError
from adapters.wechat_ocr import (
    COMPOSER_BAND_TOP,
    MIN_TEXT_CONFIDENCE,
    TITLE_BAND_BOTTOM,
    WECHAT_BUNDLE_ID,
    OCRChatMessage,
    OCRObservation,
    WeChatOCRChatReader,
    WeChatWindow,
    capture_window_png,
    chat_identity,
    find_wechat_window,
    normalize_text,
    ocr_png,
    read_composer_band_text,
    verify_own_bubble,
)
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.logging import get_logger

OSASCRIPT_PATH = "/usr/bin/osascript"
ACTIVATE_DELAY_SECONDS = 0.8
PASTE_VERIFY_DELAY_SECONDS = 0.8
POST_SEND_VERIFY_DELAY_SECONDS = 1.5
SCRIPT_TIMEOUT_SECONDS = 10.0
CLIPBOARD_TIMEOUT_SECONDS = 5.0

_DESKTOP_LOCK = asyncio.Lock()

logger = get_logger(__name__)

_ERRORS = {
    "TARGET_MISMATCH": "OCR 回复通道仅支持已确认的微信单聊。",
    "EMPTY_CONTENT": "回复草稿不能为空。",
    "NO_WINDOW": "微信没有可发送的聊天主窗口。",
    "TITLE_MISMATCH": "当前打开的会话不是已确认的 WECHAT_OCR_CONTACT 单聊；发送已停止。",
    "COMPOSER_NOT_EMPTY": "微信输入区域已有内容；不会覆盖现有草稿。",
    "COMPOSER_VERIFY_FAILED": "无法确认草稿已粘贴到微信输入区域；未按回车。",
    "SCRIPT_FAILED": "微信前台激活或键盘事件脚本失败。",
    "CLIPBOARD_FAILED": "无法读写系统剪贴板；未发送。",
    "SEND_TIMEOUT": "微信发送阶段超时。",
    "SEND_CANCELLED": "微信发送阶段被取消。",
    "SEND_FAILED": "微信发送阶段未能完成确认。",
    "RECEIPT_UNCONFIRMED": "没有确认到新增的匹配出站气泡。",
    "WINDOW_CHANGED": "wx2 的窗口或进程发生变化；操作已停止。",
    "CONTACT_AMBIGUOUS": "无法唯一定位 Ri 单聊；未选择其他联系人。",
    "SEND_BUTTON_MISSING": "无法唯一定位发送按钮；未提交。",
}
_SEND_UNKNOWN_SUFFIX = " 结果未知，禁止自动重发。"


class WeChatOCRReplyError(InboxError):
    """Expose fixed preflight errors without draft content or raw diagnostics."""

    def __init__(self, code: str) -> None:
        """Keep only allowlisted machine codes and static explanations."""
        self.code: str = code if code in _ERRORS else "SCRIPT_FAILED"
        super().__init__(_ERRORS[self.code])


class RealSendingDisabled(RuntimeError):
    """Reject all actual sending independently of preflight success."""

    def __init__(self) -> None:
        """Report the current delivery boundary without accepting private details."""
        super().__init__("本人微信真实发送未启用；当前通道仅支持只读预检。")


class WeChatOCRSendError(InboxError):
    """Keep a safe send cause and whether any native write may have occurred."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        """Discard raw native diagnostics and mark uncertain outcomes explicitly."""
        self.code: str = code if code in _ERRORS else "SEND_FAILED"
        self.outcome_unknown: bool = outcome_unknown
        message = _ERRORS[self.code]
        if outcome_unknown:
            message += _SEND_UNKNOWN_SUFFIX
        super().__init__(message)


class WeChatOCRSendUnknown(WeChatOCRSendError):
    """Retain a claim when pasting or Return may already have happened."""

    def __init__(self, code: str = "SEND_FAILED") -> None:
        """Expose an allowlisted cause without suggesting a retry."""
        super().__init__(code, outcome_unknown=True)


class WeChatOCRSendCancelled(asyncio.CancelledError):
    """Preserve cancellation while warning the caller not to replay the claim."""

    def __init__(self) -> None:
        """Keep a fixed cancellation cause for a potentially started native send."""
        self.code: str = "SEND_CANCELLED"
        self.outcome_unknown: bool = True
        super().__init__("微信发送被取消；结果未知，禁止自动重发。")


ScriptRunner = Callable[[str], Awaitable[None]]
ClipboardSetter = Callable[[str], Awaitable[object]]
ClipboardRestorer = Callable[[object], Awaitable[None]]
BandReader = Callable[[], Awaitable[str]]
ReplyVerifier = Callable[[str], Awaitable[bool]]
Sleeper = Callable[[float], Awaitable[None]]


class WeChatReplyChannelProtocol(Protocol):
    """Bind contact selection, preview and delivery to the same wx2 window."""

    async def select_contact(self) -> None:
        """Select only Ri, verifying the result before extracting questions."""
        raise NotImplementedError

    async def prepare_reply(self, user_id: str, content: str) -> None:
        """Perform a read-only preview preflight."""
        raise NotImplementedError

    async def send_text(self, user_id: str, content: str) -> None:
        """Submit once and require a new local outgoing bubble."""
        raise NotImplementedError


def _window_observations(window: WeChatWindow) -> list[OCRObservation]:
    """OCR only the already bound window."""
    return ocr_png(capture_window_png(window.window_id))


def _action_script(
    window: WeChatWindow,
    x: float,
    y: float,
    *,
    paste: bool = False,
    select_all: bool = False,
) -> str:
    """Generate fixed wx2-only actions; only numeric geometry enters script text."""
    point_x, point_y = (
        round(window.x + x * window.width),
        round(window.y + y * window.height),
    )
    keys = ('keystroke "a" using command down\n' if select_all else "") + (
        'keystroke "v" using command down\n' if paste else ""
    )
    return f'''tell application id "{WECHAT_BUNDLE_ID}" to activate
tell application "System Events"
    set targetProcess to first application process whose unix id is {window.pid}
    if (unix id of (first application process whose frontmost is true)) is not {window.pid} then error "WRONG_PROCESS"
    tell targetProcess
        if count of windows is not 1 then error "WINDOW_AMBIGUOUS"
        set p to position of window 1
        set s to size of window 1
        if (item 1 of p) < {window.x - 2} or (item 1 of p) > {window.x + 2} or (item 2 of p) < {window.y - 2} or (item 2 of p) > {window.y + 2} then error "WINDOW_MOVED"
        if (item 1 of s) < {window.width - 2} or (item 1 of s) > {window.width + 2} or (item 2 of s) < {window.height - 2} or (item 2 of s) > {window.height + 2} then error "WINDOW_RESIZED"
    end tell
    click at {{{point_x}, {point_y}}}
    {keys}
end tell'''


async def _default_script_runner(source: str) -> None:
    """Run one fixed AppleScript snippet and discard its private diagnostics."""
    process = None
    drained = False
    try:
        async with asyncio.timeout(SCRIPT_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                OSASCRIPT_PATH,
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=IS_POSIX,
            )
            await process.communicate(input=source.encode("utf-8"))
        drained = True
    finally:
        if not drained and process is not None:
            await stop_cli_process(process, isolated_session=IS_POSIX)
    if process.returncode != 0:
        raise WeChatOCRSendError("SCRIPT_FAILED")


@dataclass(frozen=True)
class ClipboardState:
    """Retain all original pasteboard formats and our replacement generation."""

    items: tuple[tuple[tuple[str, bytes], ...], ...]
    change_count: int


def _replace_clipboard(content: str) -> ClipboardState:
    """Keep images and rich formats, refusing a concurrent clipboard change."""
    import AppKit

    board = AppKit.NSPasteboard.generalPasteboard()
    count = int(board.changeCount())
    items = tuple(
        tuple(
            (str(kind), bytes(item.dataForType_(kind)))
            for kind in item.types()
            if item.dataForType_(kind) is not None
        )
        for item in board.pasteboardItems() or []
    )
    if int(board.changeCount()) != count:
        raise WeChatOCRSendError("CLIPBOARD_FAILED")
    board.clearContents()
    if not board.setString_forType_(content, AppKit.NSPasteboardTypeString):
        raise WeChatOCRSendError("CLIPBOARD_FAILED")
    return ClipboardState(items, int(board.changeCount()))


def _restore_clipboard(state: ClipboardState) -> None:
    """Restore only if nobody has copied new content since our replacement."""
    import AppKit
    from Foundation import NSData

    board = AppKit.NSPasteboard.generalPasteboard()
    if int(board.changeCount()) != state.change_count:
        return
    restored = []
    for formats in state.items:
        item = AppKit.NSPasteboardItem.alloc().init()
        for kind, data in formats:
            if not item.setData_forType_(
                NSData.dataWithBytes_length_(data, len(data)), kind
            ):
                raise WeChatOCRSendError("CLIPBOARD_FAILED")
        restored.append(item)
    if int(board.changeCount()) != state.change_count:
        return
    board.clearContents()
    if restored and not board.writeObjects_(restored):
        raise WeChatOCRSendError("CLIPBOARD_FAILED")


async def _default_clipboard_setter(content: str) -> object:
    """Bound the native clipboard operation without exposing clipboard content."""
    try:
        async with asyncio.timeout(CLIPBOARD_TIMEOUT_SECONDS):
            return await asyncio.to_thread(_replace_clipboard, content)
    except Exception:  # noqa: BLE001 - Native diagnostics may contain clipboard content.
        raise WeChatOCRSendError("CLIPBOARD_FAILED") from None


async def _default_clipboard_restorer(state: object) -> None:
    """Keep restoration failures separate from an uncertain send result."""
    try:
        if not isinstance(state, ClipboardState):
            raise WeChatOCRSendError("CLIPBOARD_FAILED")
        async with asyncio.timeout(CLIPBOARD_TIMEOUT_SECONDS):
            await asyncio.to_thread(_restore_clipboard, state)
    except Exception:  # noqa: BLE001 - Restoration must not mask the send outcome.
        logger.warning(
            "wechat_ocr_clipboard_restore_failed",
            msg_id=get_contextvars().get("msg_id"),
        )


class WeChatOCRReplyChannel:
    """Preflight one verified chat window and allow explicit single-attempt sends.

    A successful live send proves only that a matching own bubble was
    observed after one Return press; remote delivery is never verified.
    """

    def __init__(
        self,
        *,
        contact: str,
        live_enabled: bool = False,
        reader: WeChatOCRChatReader | None = None,
        script_runner: ScriptRunner = _default_script_runner,
        clipboard_setter: ClipboardSetter = _default_clipboard_setter,
        clipboard_restorer: ClipboardRestorer = _default_clipboard_restorer,
        band_reader: BandReader = read_composer_band_text,
        verifier: ReplyVerifier | None = None,
        sleeper: Sleeper = asyncio.sleep,
        window_finder: Callable[[], WeChatWindow] = find_wechat_window,
        observations_reader: Callable[
            [WeChatWindow], list[OCRObservation]
        ] = _window_observations,
    ) -> None:
        """Require an explicit contact identity and an explicit live switch."""
        self._identity = chat_identity(contact)
        self._contact = contact.strip()
        if type(live_enabled) is not bool:
            raise ValueError("live_enabled must be an explicit boolean")
        self._live_enabled = live_enabled
        self._reader: WeChatOCRChatReader | None = reader
        self._script_runner = script_runner
        self._clipboard_setter = clipboard_setter
        self._clipboard_restorer = clipboard_restorer
        self._band_reader = band_reader
        self._sleeper = sleeper
        self._window_finder = window_finder
        self._observations_reader = observations_reader
        self._window: WeChatWindow | None = None
        self._before: list[OCRChatMessage] | None = None
        self._verifier: ReplyVerifier = (
            verifier if verifier is not None else self._default_verifier
        )
        self._lock = _DESKTOP_LOCK

    async def _bound_window(self) -> WeChatWindow:
        """Revalidate PID, window ID, position and size before every operation."""
        async with asyncio.timeout(SCRIPT_TIMEOUT_SECONDS):
            window = await asyncio.to_thread(self._window_finder)
        if self._window is not None and window != self._window:
            raise WeChatOCRReplyError("WINDOW_CHANGED")
        self._window = window
        return window

    async def _observations(self) -> list[OCRObservation]:
        """Bound one OCR observation and verify geometry remains unchanged."""
        window = await self._bound_window()
        async with asyncio.timeout(SCRIPT_TIMEOUT_SECONDS):
            observations = await asyncio.to_thread(self._observations_reader, window)
        await self._bound_window()
        if any(o.confidence < MIN_TEXT_CONFIDENCE for o in observations):
            raise WeChatOCRReplyError("CONTACT_AMBIGUOUS")
        return observations

    async def select_contact(self) -> None:
        """Use exact sidebar labels, or search Ri; never select the first ambiguous hit."""
        if not self._live_enabled or self._contact != "Ri":
            raise RealSendingDisabled()
        async with self._lock:
            observations = await self._observations()
            title = "".join(
                o.text
                for o in sorted(observations, key=lambda o: o.x)
                if o.y < TITLE_BAND_BOTTOM and o.x_center > 0.35
            )
            if normalize_text(title) == normalize_text(self._contact):
                await self._preflight_locked()
                return
            rows = [
                o
                for o in observations
                if o.text.strip() == self._contact
                and o.x_center < 0.35
                and o.y >= TITLE_BAND_BOTTOM
            ]
            if not rows:
                search = [
                    o
                    for o in observations
                    if o.text.strip() == "搜索" and o.x_center < 0.35 and o.y < 0.12
                ]
                if len(search) != 1:
                    raise WeChatOCRReplyError("CONTACT_AMBIGUOUS")
                previous = await self._clipboard_setter(self._contact)
                try:
                    window = await self._bound_window()
                    await self._script_runner(
                        _action_script(
                            window,
                            search[0].x_center,
                            search[0].y + search[0].h / 2,
                            paste=True,
                            select_all=True,
                        )
                    )
                finally:
                    await self._clipboard_restorer(previous)
                await self._sleeper(PASTE_VERIFY_DELAY_SECONDS)
                observations = await self._observations()
                rows = [
                    o
                    for o in observations
                    if o.text.strip() == self._contact
                    and o.x_center < 0.35
                    and o.y >= 0.12
                ]
            if len(rows) != 1:
                raise WeChatOCRReplyError("CONTACT_AMBIGUOUS")
            window = await self._bound_window()
            await self._script_runner(
                _action_script(window, rows[0].x_center, rows[0].y + rows[0].h / 2)
            )
            await self._sleeper(ACTIVATE_DELAY_SECONDS)
            await self._preflight_locked()

    async def _default_verifier(self, draft: str) -> bool:
        """Confirm the bubble through the shared OCR reader."""
        reader = self._reader
        if reader is None:
            from adapters.wechat_ocr import DesktopWeChatOCRReader

            reader = DesktopWeChatOCRReader()
        return await verify_own_bubble(
            reader, contact=self._contact, draft=draft, before=self._before
        )

    @staticmethod
    def _one_line(content: str) -> str:
        """Collapse newlines because Return always sends in the composer."""
        return " ".join(content.splitlines()).strip()

    def _validate_draft(self, user_id: str, content: str) -> None:
        """Keep recipient and draft validation identical for preview and live."""
        if user_id != self._identity:
            raise WeChatOCRReplyError("TARGET_MISMATCH")
        if not isinstance(content, str) or not content.strip():
            raise WeChatOCRReplyError("EMPTY_CONTENT")

    async def _preflight_locked(self) -> None:
        """Verify the open chat through one read; raise safely on mismatch."""
        reader = self._reader
        if reader is None:
            from adapters.wechat_ocr import DesktopWeChatOCRReader

            reader = DesktopWeChatOCRReader()
        await reader.read_chat(contact=self._contact)
        await self._bound_window()
        logger.info(
            "wechat_ocr_preflight_ready",
            msg_id=get_contextvars().get("msg_id"),
            stage="preflight",
            dry_run=not self._live_enabled,
            delivery_verified=False,
        )

    async def prepare_reply(self, user_id: str, content: str) -> None:
        """Validate the verified chat window without typing or sending text."""
        self._validate_draft(user_id, content)
        async with self._lock:
            await self._preflight_locked()

    async def send_text(self, user_id: str, content: str) -> None:
        """Focus and paste, verify, click Send once, and prove a new own bubble."""
        if not self._live_enabled:
            raise RealSendingDisabled()
        self._validate_draft(user_id, content)
        draft = self._one_line(content)
        if not draft:
            raise WeChatOCRSendError("EMPTY_CONTENT")
        async with self._lock:
            await self._preflight_locked()
            reader = self._reader
            if reader is None:
                from adapters.wechat_ocr import DesktopWeChatOCRReader

                reader = DesktopWeChatOCRReader()
            self._before = await reader.read_chat(contact=self._contact)
            band_before = await self._read_band_safe()
            if band_before.strip():
                raise WeChatOCRSendError("COMPOSER_NOT_EMPTY")
            previous_clipboard = await self._clipboard_setter(draft)
            cancelled = False
            failure: WeChatOCRSendError | None = None
            try:
                window = await self._bound_window()
                await self._script_runner(
                    _action_script(
                        window, 0.65, (COMPOSER_BAND_TOP + 0.92) / 2, paste=True
                    )
                )
                await self._sleeper(PASTE_VERIFY_DELAY_SECONDS)
                band_after = await self._read_band_safe()
                if normalize_text(draft) != normalize_text(band_after):
                    raise WeChatOCRSendUnknown("COMPOSER_VERIFY_FAILED")
                if await reader.read_chat(contact=self._contact) != self._before:
                    raise WeChatOCRSendUnknown("RECEIPT_UNCONFIRMED")
                observations = await self._observations()
                buttons = [
                    o
                    for o in observations
                    if o.text.strip() == "发送"
                    and o.x > 0.85
                    and o.y > COMPOSER_BAND_TOP
                ]
                if len(buttons) != 1:
                    raise WeChatOCRSendUnknown("SEND_BUTTON_MISSING")
                window = await self._bound_window()
                await self._script_runner(
                    _action_script(
                        window, buttons[0].x_center, buttons[0].y + buttons[0].h / 2
                    )
                )
                await self._sleeper(POST_SEND_VERIFY_DELAY_SECONDS)
                if not await self._verifier(draft):
                    raise WeChatOCRSendUnknown("RECEIPT_UNCONFIRMED")
            except asyncio.CancelledError:
                cancelled = True
            except WeChatOCRSendError as exc:
                failure = (
                    WeChatOCRSendUnknown(exc.code)
                    if exc.outcome_unknown
                    else WeChatOCRSendError(exc.code)
                )
            except TimeoutError:
                failure = WeChatOCRSendUnknown("SEND_TIMEOUT")
            except Exception:  # noqa: BLE001 - Every unclassified failure is uncertain.
                failure = WeChatOCRSendUnknown("SEND_FAILED")
            finally:
                await self._clipboard_restorer(previous_clipboard)
            if cancelled:
                logger.error(
                    "wechat_ocr_send_cancelled",
                    msg_id=get_contextvars().get("msg_id"),
                    outcome_unknown=True,
                    retry=False,
                )
                raise WeChatOCRSendCancelled()
            if failure is not None:
                logger.error(
                    "wechat_ocr_send_failed",
                    msg_id=get_contextvars().get("msg_id"),
                    code=failure.code,
                    outcome_unknown=failure.outcome_unknown,
                    retry=False,
                )
                raise failure
            logger.info(
                "wechat_ocr_send_local_receipt",
                msg_id=get_contextvars().get("msg_id"),
                stage="local_outgoing_bubble",
                remote_delivery_verified=False,
            )

    async def _read_band_safe(self) -> str:
        """Read the composer band, converting any failure into a safe error."""
        try:
            return await self._band_reader()
        except InboxError:
            raise
        except Exception:  # noqa: BLE001 - Band text stays out of error surfaces.
            raise WeChatOCRSendUnknown("SEND_FAILED") from None
