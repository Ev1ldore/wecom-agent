"""Receive and reply in the verified wx2 Ri popout via native screenshot OCR."""

import asyncio
import base64
import io
import json
import sys
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from structlog.contextvars import get_contextvars

from adapters.inbox import InboxError, Msg
from adapters.wechat_ocr import (
    TIME_LABEL_RE,
    OCRChatMessage,
    OCRObservation,
    WeChatWindow,
    chat_identity,
    normalize_text,
    ocr_png,
)
from adapters.wecom_cli import IS_POSIX, stop_cli_process
from src.core.logging import get_logger

logger = get_logger(__name__)
IDENTITY = chat_identity("Ri")


class NativeError(InboxError):
    """Keep fixed failures private and retain uncertain writes without retrying."""

    def __init__(self, code: str, *, outcome_unknown: bool = False) -> None:
        allowed = {
            "TARGET_MISSING",
            "AX_UNAVAILABLE",
            "LAYOUT_UNSUPPORTED",
            "WINDOW_CHANGED",
            "HUMAN_TAKEOVER",
            "CAPTURE_BUSY",
            "CAPTURE_FAILED",
            "INVALID_DRAFT",
            "INVALID_OPERATION",
            "INTERRUPTED",
            "NATIVE_FAILED",
            "TITLE_MISMATCH",
            "NO_TIME_ANCHOR",
            "AMBIGUOUS_LAYOUT",
            "TIMEOUT",
            "COMPOSER_NOT_EMPTY",
            "COMPOSER_VERIFY_FAILED",
            "RECEIPT_UNCONFIRMED",
            "SENDING_DISABLED",
            "TARGET_MISMATCH",
            "HISTORY_INCOMPLETE",
        }
        self.code = code if code in allowed else "NATIVE_FAILED"
        self.outcome_unknown = outcome_unknown
        super().__init__("wx2 Ri 接入暂停：" + self.code)


class NativeBackend(Protocol):
    """Expose isolated, bounded native operations with an optional window binding."""

    async def request(
        self, op: str, *, window: WeChatWindow | None = None, text: str = ""
    ) -> dict:
        """Perform one operation; writes must never be retried by this layer."""
        raise NotImplementedError


class SubprocessNativeBackend:
    """Terminate timed-out native workers, allowing their screenshot cleanup."""

    async def request(
        self, op: str, *, window: WeChatWindow | None = None, text: str = ""
    ) -> dict:
        process = None
        drained = False
        try:
            async with asyncio.timeout(12):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "adapters.wechat_native_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=IS_POSIX,
                )
                payload = {"op": op, "text": text}
                if window is not None:
                    payload["window"] = asdict(window)
                stdout, _ = await process.communicate(json.dumps(payload).encode())
                drained = True
                if len(stdout) > 4_100_000:
                    raise NativeError("NATIVE_FAILED")
                result = json.loads(stdout)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise NativeError(
                        result.get("code", "NATIVE_FAILED")
                        if isinstance(result, dict)
                        else "NATIVE_FAILED"
                    )
                if process.returncode != 0:
                    raise NativeError("NATIVE_FAILED")
                return result
        except TimeoutError:
            raise NativeError("TIMEOUT") from None
        except (OSError, ValueError, TypeError):
            raise NativeError("NATIVE_FAILED") from None
        finally:
            if process is not None and not drained:
                # SIGKILL cannot run the worker's screenshot cleanup blocks.
                # First allow its SIGTERM handler to clean up the owned UI action.
                try:
                    if process.returncode is None:
                        process.terminate()
                    async with asyncio.timeout(8):
                        await process.communicate()
                except (OSError, TimeoutError, asyncio.CancelledError):
                    await stop_cli_process(process, isolated_session=IS_POSIX)


@dataclass(frozen=True)
class NativeFrame:
    """Keep one OCR snapshot and its verified popout geometry together."""

    window: WeChatWindow
    messages: tuple[OCRChatMessage, ...]
    composer: str
    send_button: bool


def mark_green_bubbles(
    png: bytes, observations: list[OCRObservation]
) -> list[OCRObservation]:
    """Recognize green own bubbles even under the screenshot tool's dim overlay."""
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        image = image.convert("RGB")
        w, h = image.size
        result = []
        for o in observations:
            green = 0
            for fx in (0.05, 0.25, 0.5, 0.75, 0.95):
                for fy in (0.15, 0.5, 0.85):
                    x = min(w - 1, max(0, int((o.x + o.w * fx) * w)))
                    y = min(h - 1, max(0, int((o.y + o.h * fy) * h)))
                    red, g, blue = image.getpixel((x, y))
                    green += g > 55 and g - red > 14 and g - blue > 14
            result.append(replace(o, bubble_side="self") if green >= 7 else o)
    return result


def parse_native_frame(window: WeChatWindow, rows: list[OCRObservation]) -> NativeFrame:
    """Parse only the tested Ri popout layout, retaining original visible labels."""
    if not rows or len(rows) > 2000:
        raise NativeError("AMBIGUOUS_LAYOUT")

    def avatar(o: OCRObservation) -> bool:
        return 0.125 <= o.y < 0.78 and (o.x >= 0.89 or o.x + o.w <= 0.10)

    def clipped(o: OCRObservation) -> bool:
        return 0.125 <= o.y < 0.78 and o.h < 0.020 and (o.y < 0.145 or o.y + o.h > 0.75)

    for o in rows:
        if (
            not isinstance(o, OCRObservation)
            or not 0 <= o.x < o.x + o.w <= 1.01
            or not 0 <= o.y < o.y + o.h <= 1.01
            or not 0 <= o.confidence <= 1
            or (
                (0.045 <= o.y < 0.11 or 0.125 <= o.y < 0.925)
                and not avatar(o)
                and not clipped(o)
                and o.confidence < 0.3
            )
        ):
            raise NativeError("AMBIGUOUS_LAYOUT")
    titles = [o for o in rows if 0.045 <= o.y < 0.11 and o.x < 0.4]
    if len(titles) != 1 or titles[0].text.strip() != "Ri":
        raise NativeError("TITLE_MISMATCH")
    composer = "\n".join(
        o.text
        for o in sorted(rows, key=lambda o: (o.y, o.x))
        if 0.78 <= o.y < 0.925 and normalize_text(o.text) != "按住鼠标语音输入文字"
    )
    buttons = [
        o for o in rows if o.text.strip() == "发送" and o.x > 0.85 and o.y > 0.925
    ]
    if any(
        not (0.90 <= o.x_center <= 0.97 and 0.94 <= o.y + o.h / 2 <= 0.98)
        for o in buttons
    ):
        raise NativeError("LAYOUT_UNSUPPORTED")
    body = sorted(
        (
            o
            for o in rows
            if 0.125 <= o.y and o.y + o.h <= 0.78 and not avatar(o) and not clipped(o)
        ),
        key=lambda o: (o.y, o.x),
    )
    anchors = [
        o
        for o in body
        if TIME_LABEL_RE.fullmatch(o.text.replace(" ", ""))
        and 0.40 <= o.x_center <= 0.60
        and o.w < 0.35
    ]
    if not anchors:
        raise NativeError("NO_TIME_ANCHOR")
    bubbles: list[tuple[bool, str, list[OCRObservation]]] = []
    unanchored = 0
    for o in body:
        if o in anchors:
            continue
        preceding = [t for t in anchors if t.y + t.h <= o.y + 0.005]
        if not preceding:
            unanchored += 1
            continue
        label = preceding[-1].text.strip()
        if o.bubble_side == "self":
            own = True
        elif 0.08 <= o.x <= 0.20:
            own = False
        else:
            # Centered system rows and unknown layouts never become peer messages.
            raise NativeError("AMBIGUOUS_LAYOUT")
        if (
            bubbles
            and bubbles[-1][0] == own
            and bubbles[-1][1] == label
            and 0
            <= o.y - (bubbles[-1][2][-1].y + bubbles[-1][2][-1].h)
            <= max(0.012, bubbles[-1][2][-1].h * 0.6)
        ):
            bubbles[-1][2].append(o)
        else:
            bubbles.append((own, label, [o]))
    messages = tuple(
        OCRChatMessage("\n".join(o.text for o in lines), own, label)
        for own, label, lines in bubbles
    )
    if not messages:
        raise NativeError("NO_TIME_ANCHOR")
    logger.info(
        "wechat_native_parsed",
        msg_id=get_contextvars().get("msg_id"),
        messages=len(messages),
        unanchored_lines=unanchored,
        ignored_avatar_observations=sum(avatar(o) for o in rows),
        clipped_lines=sum(clipped(o) for o in rows),
    )
    return NativeFrame(window, messages, composer, len(buttons) == 1)


class NativeChatProtocol(Protocol):
    """Bind capture, normalized receive and once-only sending to one Ri window."""

    async def snapshot(self) -> NativeFrame:
        raise NotImplementedError

    async def send_text(self, user_id: str, content: str) -> None:
        raise NotImplementedError


def join_history_views(
    upper: tuple[OCRChatMessage, ...], lower: tuple[OCRChatMessage, ...]
) -> tuple[OCRChatMessage, ...] | None:
    """Join captured pages only through an exact, unique overlapping sequence."""
    for size in range(min(len(upper), len(lower)), 0, -1):
        anchor = upper[-size:]
        if lower[:size] != anchor:
            continue
        for page in (upper, lower):
            if (
                sum(page[i : i + size] == anchor for i in range(len(page) - size + 1))
                != 1
            ):
                return None
        return upper + lower[size:]
    return None


class NativeRiChat:
    """Serialize all screenshot and write operations; never retry a native write."""

    def __init__(
        self,
        backend: NativeBackend | None = None,
        *,
        live: bool = False,
        recognizer=ocr_png,
        color_marker=mark_green_bubbles,
    ) -> None:
        self.backend = backend or SubprocessNativeBackend()
        self.live = live
        self.recognizer = recognizer
        self.color_marker = color_marker
        self.window: WeChatWindow | None = None
        self.lock = asyncio.Lock()
        self._history: tuple[OCRChatMessage, ...] | None = None
        self._view: tuple[OCRChatMessage, ...] | None = None

    async def activate(self) -> None:
        async with self.lock:
            result = await self.backend.request("activate", window=self.window)
            self.window = WeChatWindow(**result["window"])

    async def _read_frame(self) -> NativeFrame:
        for attempt in range(2):
            try:
                result = await self.backend.request("capture", window=self.window)
                window = WeChatWindow(**result["window"])
                if self.window is not None and self.window != window:
                    raise NativeError("WINDOW_CHANGED")
                png = base64.b64decode(result["png"], validate=True)
                async with asyncio.timeout(15):
                    rows = await asyncio.to_thread(self.recognizer, png)
                    rows = await asyncio.to_thread(self.color_marker, png, rows)
                frame = parse_native_frame(window, rows)
                self.window = window
                return frame
            except NativeError as exc:
                if (
                    exc.code not in {"CAPTURE_FAILED", "TIMEOUT", "NATIVE_FAILED"}
                    or attempt
                ):
                    logger.warning(
                        "wechat_native_read_blocked",
                        msg_id=get_contextvars().get("msg_id"),
                        code=exc.code,
                    )
                    raise
            except Exception:  # noqa: BLE001 - OCR diagnostics may contain private text.
                if attempt:
                    raise NativeError("CAPTURE_FAILED") from None
            logger.warning(
                "wechat_native_capture_retry", msg_id=get_contextvars().get("msg_id")
            )
        raise NativeError("CAPTURE_FAILED")

    async def _snapshot(self) -> NativeFrame:
        frame = await self._read_frame()
        old = self._history
        if old is None:
            self._history, self._view = frame.messages, frame.messages
            return frame
        if frame.messages == self._view:
            return replace(frame, messages=old)
        if frame.messages[: len(old)] == old:
            self._history, self._view = frame.messages, frame.messages
            return frame
        # A truncated viewport is never accepted as a new history. Read upwards
        # until the complete saved prefix is actually observed across overlapping
        # pages, then return to the bottom and require a unique matching anchor.
        combined = frame.messages
        restored = False
        try:
            for _ in range(10):
                await self.backend.request("scroll_up", window=self.window)
                try:
                    upper = await self._read_frame()
                except NativeError as exc:
                    if exc.code == "NO_TIME_ANCHOR":
                        continue
                    raise
                joined = join_history_views(upper.messages, combined)
                if joined is not None:
                    combined = joined
                starts = [
                    i for i in range(len(combined)) if combined[i : i + len(old)] == old
                ]
                if len(starts) == 1:
                    combined = combined[starts[0] :]
                    restored = True
                    break
        finally:
            await self.backend.request("scroll_bottom", window=self.window)
        if not restored:
            raise NativeError("HISTORY_INCOMPLETE")
        bottom = await self._read_frame()
        joined = join_history_views(combined, bottom.messages)
        if joined is None or joined[: len(old)] != old:
            raise NativeError("HISTORY_INCOMPLETE")
        self._history, self._view = joined, bottom.messages
        return replace(bottom, messages=joined)

    async def snapshot(self) -> NativeFrame:
        async with self.lock:
            return await self._snapshot()

    async def poll_unread(self) -> list[Msg]:
        frame = await self.snapshot()
        return [
            Msg(
                chat=IDENTITY,
                sender="desktop:self" if m.is_self else IDENTITY,
                content=m.text,
                ts=None,
                time_precision="minute",
                time_display=m.time_display,
                is_self=m.is_self,
            )
            for m in frame.messages
        ]

    async def prepare_reply(self, user_id: str, content: str) -> None:
        if user_id != IDENTITY:
            raise NativeError("TARGET_MISMATCH")
        frame = await self.snapshot()
        if frame.composer or not frame.send_button:
            raise NativeError("COMPOSER_NOT_EMPTY")

    async def send_text(self, user_id: str, content: str) -> None:
        if not self.live:
            raise NativeError("SENDING_DISABLED")
        if user_id != IDENTITY:
            raise NativeError("TARGET_MISMATCH")
        draft = " ".join(content.splitlines()).strip()
        if not draft or len(draft) > 500:
            raise NativeError("INVALID_DRAFT")
        async with self.lock:
            before = await self._snapshot()
            if before.composer or not before.send_button:
                raise NativeError("COMPOSER_NOT_EMPTY")
            writing = False
            try:
                writing = True
                await self.backend.request("type", window=self.window, text=draft)
                typed = await self._snapshot()
                if (
                    normalize_text(typed.composer) != normalize_text(draft)
                    or typed.messages != before.messages
                    or not typed.send_button
                ):
                    raise NativeError("COMPOSER_VERIFY_FAILED")
                await self.backend.request("send", window=self.window)
                after = await self._snapshot()
                count = len(before.messages)
                if (
                    after.composer
                    or after.messages[:count] != before.messages
                    or len(after.messages) <= count
                    or not any(
                        m.is_self and normalize_text(m.text) == normalize_text(draft)
                        for m in after.messages[count:]
                    )
                ):
                    raise NativeError("RECEIPT_UNCONFIRMED")
            except asyncio.CancelledError as exc:
                exc.outcome_unknown = writing
                raise
            except Exception as exc:  # noqa: BLE001 - Preserve all uncertain native writes.
                raise NativeError(
                    exc.code if isinstance(exc, NativeError) else "NATIVE_FAILED",
                    outcome_unknown=writing,
                ) from None
            logger.info(
                "wechat_native_local_submit_confirmed",
                msg_id=get_contextvars().get("msg_id"),
            )
