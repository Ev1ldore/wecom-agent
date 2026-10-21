"""Bound native UI operations to the already opened wx2 Ri popout window.

Invoked in an isolated subprocess, with JSON on stdin/stdout. No client files,
database keys or client code are accessed. Only capture retries are permitted
by the caller; typing and send each represent a single potentially lasting act.
"""

import base64
import json
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from adapters.wechat_ocr import WECHAT_APP_PATH, WECHAT_BUNDLE_ID, WeChatWindow


class NativeFailure(Exception):
    """Carry only a fixed machine code, never native diagnostics or chat text."""


class NativeSessionProtocol(Protocol):
    """Define guarded window discovery and one native operation."""

    def guard(self, *, foreground: bool = True) -> WeChatWindow:
        """Validate exact app, unique Ri window, geometry and foreground title."""
        raise NotImplementedError

    def execute(self, op: str, text: str = "") -> dict:
        """Execute one bounded capture, activation, typing or send click."""
        raise NotImplementedError


class NativeSession:
    """Use the same native events and screenshot shortcut verified on this Mac."""

    def __init__(self, expected: dict | None = None) -> None:
        import AppKit
        import Quartz

        self.a = AppKit
        self.q = Quartz
        self.expected = WeChatWindow(**expected) if expected is not None else None
        self.app = None

    def _infos(self) -> list:
        return list(
            self.q.CGWindowListCopyWindowInfo(
                self.q.kCGWindowListOptionOnScreenOnly, self.q.kCGNullWindowID
            )
            or []
        )

    def _ax_title(self, pid: int, *, raise_window: bool = False) -> str:
        action = (
            'perform action "AXRaise" of window "Ri" of p\n' if raise_window else ""
        )
        source = (
            f'tell application "System Events"\n'
            f"set p to first application process whose unix id is {pid}\n"
            f"{action}return name of first window of p\nend tell"
        )
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", source],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode:
            raise NativeFailure("AX_UNAVAILABLE")
        return result.stdout.strip()

    def guard(self, *, foreground: bool = True) -> WeChatWindow:
        apps = [
            a
            for a in self.a.NSWorkspace.sharedWorkspace().runningApplications()
            if a.bundleURL() is not None
            and str(a.bundleURL().path()) == WECHAT_APP_PATH
            and str(a.bundleIdentifier()) == WECHAT_BUNDLE_ID
        ]
        if len(apps) != 1:
            raise NativeFailure("TARGET_MISSING")
        self.app = apps[0]
        pid = int(self.app.processIdentifier())
        infos = [w for w in self._infos() if w.get("kCGWindowOwnerPID") == pid]
        candidates = [
            w
            for w in infos
            if w.get("kCGWindowName") == "Ri" and w.get("kCGWindowLayer") == 0
        ]
        if len(candidates) != 1:
            raise NativeFailure("TARGET_MISSING")
        info = candidates[0]
        b = info["kCGWindowBounds"]
        window = WeChatWindow(
            int(info["kCGWindowNumber"]),
            pid,
            *[float(b[k]) for k in ("X", "Y", "Width", "Height")],
        )
        if not (560 <= window.width <= 900 and 600 <= window.height <= 1000):
            raise NativeFailure("LAYOUT_UNSUPPORTED")
        if self.expected is not None and window != self.expected:
            raise NativeFailure("WINDOW_CHANGED")
        if foreground:
            front = self.a.NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is None or int(front.processIdentifier()) != pid:
                raise NativeFailure("HUMAN_TAKEOVER")
            if self._ax_title(pid) != "Ri":
                raise NativeFailure("HUMAN_TAKEOVER")
        return window

    def _overlays(self, pid: int) -> list:
        return [
            w
            for w in self._infos()
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 27
        ]

    def _wait_overlay_state(self, pid: int, *, present: bool) -> bool:
        """Wait for the asynchronous Qt screenshot window transition, bounded."""
        for _ in range(25):
            if bool(self._overlays(pid)) == present:
                return True
            time.sleep(0.1)
        return False

    def _key(self, code: int, down: bool, flags: int = 0) -> None:
        event = self.q.CGEventCreateKeyboardEvent(None, code, down)
        self.q.CGEventSetFlags(event, flags)
        self.q.CGEventPost(self.q.kCGHIDEventTap, event)
        time.sleep(0.025)

    def _chord(self, code: int, *, control: bool = False) -> None:
        ctrl, cmd = self.q.kCGEventFlagMaskControl, self.q.kCGEventFlagMaskCommand
        try:
            if control:
                self._key(59, True, ctrl)
            self._key(55, True, cmd | (ctrl if control else 0))
            self._key(code, True, cmd | (ctrl if control else 0))
            self._key(code, False, cmd | (ctrl if control else 0))
        finally:
            self._key(55, False, ctrl if control else 0)
            if control:
                self._key(59, False, 0)

    def _click(self, window: WeChatWindow, x: float, y: float) -> None:
        self.guard()
        for kind in (self.q.kCGEventLeftMouseDown, self.q.kCGEventLeftMouseUp):
            event = self.q.CGEventCreateMouseEvent(
                None,
                kind,
                (window.x + x * window.width, window.y + y * window.height),
                self.q.kCGMouseButtonLeft,
            )
            self.q.CGEventSetFlags(event, 0)
            self.q.CGEventSetIntegerValueField(event, self.q.kCGMouseEventClickState, 1)
            self.q.CGEventPost(self.q.kCGHIDEventTap, event)
            time.sleep(0.05)

    def _capture(self, window: WeChatWindow) -> bytes:
        if self._overlays(window.pid):
            raise NativeFailure("CAPTURE_BUSY")
        # Keep the screenshot magnifier outside the Ri window, on its own display.
        displays = self.q.CGGetActiveDisplayList(16, None, None)[1]
        display = None
        for display_id in displays:
            r = self.q.CGDisplayBounds(display_id)
            if (
                r.origin.x <= window.x
                and r.origin.y <= window.y
                and window.x + window.width <= r.origin.x + r.size.width
                and window.y + window.height <= r.origin.y + r.size.height
            ):
                display = r
        if display is None:
            raise NativeFailure("LAYOUT_UNSUPPORTED")
        point = (display.origin.x + display.size.width - 5, display.origin.y + 45)
        if window.x + window.width > point[0] - 180:
            raise NativeFailure("LAYOUT_UNSUPPORTED")
        move = self.q.CGEventCreateMouseEvent(None, self.q.kCGEventMouseMoved, point, 0)
        self.q.CGEventSetFlags(move, 0)
        self.q.CGEventPost(self.q.kCGHIDEventTap, move)
        owned = False
        try:
            owned = True
            self._chord(0, control=True)
            if not self._wait_overlay_state(window.pid, present=True):
                raise NativeFailure("CAPTURE_FAILED")
            time.sleep(0.15)
            if self.guard(foreground=False) != window:
                raise NativeFailure("WINDOW_CHANGED")
            with tempfile.TemporaryDirectory(prefix="wx2-ri-") as folder:
                path = Path(folder) / "frame.png"
                rect = ",".join(
                    str(round(v))
                    for v in (window.x, window.y, window.width, window.height)
                )
                subprocess.run(
                    ["/usr/sbin/screencapture", "-x", "-R" + rect, str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=3,
                )
                image = path.read_bytes()
                if not image or len(image) > 3_000_000:
                    raise NativeFailure("CAPTURE_FAILED")
                return image
        finally:
            if owned:
                for _ in range(3):
                    if not self._overlays(window.pid):
                        break
                    self._key(53, True)
                    self._key(53, False)
                    if self._wait_overlay_state(window.pid, present=False):
                        break
                if self._overlays(window.pid):
                    raise NativeFailure("CAPTURE_BUSY")
                self._restore_capture_focus(window)

    def _restore_capture_focus(self, window: WeChatWindow) -> None:
        """Restore Ri if our screenshot dialog returned focus to the main window."""
        front = self.a.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is None or int(front.processIdentifier()) != window.pid:
            return
        if self.guard(foreground=False) != window:
            raise NativeFailure("WINDOW_CHANGED")
        title = self._ax_title(window.pid)
        if title in {"Ri", "微信", "WeChat"}:
            self._ax_title(window.pid, raise_window=True)

    def execute(self, op: str, text: str = "") -> dict:
        window = self.guard(foreground=op != "activate")
        if op == "activate":
            if self._overlays(window.pid):
                raise NativeFailure("CAPTURE_BUSY")
            self.app.activateWithOptions_(self.a.NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.2)
            # Establish the tested layout once at startup. Later moves are rejected.
            display = self.q.CGDisplayBounds(self.q.CGMainDisplayID())
            x = round(display.origin.x + (display.size.width - 598) / 2)
            y = round(display.origin.y + (display.size.height - 640) / 2)
            source = (
                f'tell application "System Events"\n'
                f"set p to first application process whose unix id is {window.pid}\n"
                'set size of window "Ri" of p to {598, 640}\n'
                f'set position of window "Ri" of p to {{{x}, {y}}}\nend tell'
            )
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", source],
                capture_output=True,
                timeout=3,
                check=False,
            )
            if result.returncode:
                raise NativeFailure("AX_UNAVAILABLE")
            for _ in range(25):
                settled = self.guard(foreground=False)
                if all(
                    abs(actual - desired) <= 2
                    for actual, desired in (
                        (settled.x, x),
                        (settled.y, y),
                        (settled.width, 598),
                        (settled.height, 640),
                    )
                ):
                    time.sleep(0.1)
                    if self.guard(foreground=False) == settled:
                        break
                time.sleep(0.1)
            else:
                raise NativeFailure("WINDOW_CHANGED")
            if self._ax_title(window.pid, raise_window=True) != "Ri":
                raise NativeFailure("TARGET_MISSING")
            window = self.guard()
        elif op == "capture":
            image = self._capture(window)
            self.guard()
            return {"window": asdict(window), "png": base64.b64encode(image).decode()}
        elif op == "type":
            if not text.strip() or len(text) > 500 or "\n" in text or "\r" in text:
                raise NativeFailure("INVALID_DRAFT")
            if self._overlays(window.pid):
                raise NativeFailure("CAPTURE_BUSY")
            self._click(window, 0.42, 0.84)
            chunks = []
            chunk = ""
            for character in text:
                if len((chunk + character).encode("utf-16-le")) > 32:
                    chunks.append(chunk)
                    chunk = ""
                chunk += character
            if chunk:
                chunks.append(chunk)
            for chunk in chunks:
                self.guard()
                for down in (True, False):
                    event = self.q.CGEventCreateKeyboardEvent(None, 0, down)
                    self.q.CGEventSetFlags(event, 0)
                    self.q.CGEventKeyboardSetUnicodeString(
                        event, len(chunk.encode("utf-16-le")) // 2, chunk
                    )
                    self.q.CGEventPost(self.q.kCGHIDEventTap, event)
                    time.sleep(0.025)
        elif op == "send":
            if self._overlays(window.pid):
                raise NativeFailure("CAPTURE_BUSY")
            self._click(window, 0.925, 0.955)
            time.sleep(0.4)
        elif op in {"scroll_up", "scroll_bottom"}:
            if self._overlays(window.pid):
                raise NativeFailure("CAPTURE_BUSY")
            self.guard()
            point = (window.x + window.width * 0.5, window.y + window.height * 0.45)
            event = self.q.CGEventCreateScrollWheelEvent(
                None,
                self.q.kCGScrollEventUnitPixel,
                1,
                60 if op == "scroll_up" else -10000,
            )
            self.q.CGEventSetLocation(event, point)
            self.q.CGEventSetFlags(event, 0)
            self.q.CGEventPost(self.q.kCGHIDEventTap, event)
            time.sleep(0.25)
        else:
            raise NativeFailure("INVALID_OPERATION")
        self.guard()
        return {"window": asdict(window)}


def main() -> int:
    """Read one JSON command, fail without printing private native diagnostics."""

    def interrupted(signum, frame):
        raise NativeFailure("INTERRUPTED")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        request = json.loads(sys.stdin.read(8_000))
        if not isinstance(request, dict):
            raise NativeFailure("INVALID_OPERATION")
        result = NativeSession(request.get("window")).execute(
            request.get("op", ""), request.get("text", "")
        )
        print(json.dumps({"ok": True, **result}))
        return 0
    except BaseException as exc:  # noqa: BLE001 - Worker never exposes raw UI diagnostics.
        codes = {
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
        }
        code = (
            str(exc)
            if isinstance(exc, NativeFailure) and str(exc) in codes
            else "NATIVE_FAILED"
        )
        print(json.dumps({"ok": False, "code": code, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
