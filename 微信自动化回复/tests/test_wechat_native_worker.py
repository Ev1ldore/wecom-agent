"""Verify native event sequences and target guards without posting UI events."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from adapters.wechat_native_worker import NativeFailure, NativeSession
from adapters.wechat_ocr import WECHAT_APP_PATH, WECHAT_BUNDLE_ID, WeChatWindow


def session(*, title="Ri", front=1, path=WECHAT_APP_PATH, bundle=WECHAT_BUNDLE_ID):
    s = NativeSession.__new__(NativeSession)
    app = NS(
        bundleURL=lambda: NS(path=lambda: path),
        bundleIdentifier=lambda: bundle,
        processIdentifier=lambda: 1,
    )
    workspace = NS(
        runningApplications=lambda: [app],
        frontmostApplication=lambda: NS(processIdentifier=lambda: front),
    )
    s.a = NS(NSWorkspace=NS(sharedWorkspace=lambda: workspace))
    s.q = NS(
        kCGWindowListOptionOnScreenOnly=1,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda *args: [
            {
                "kCGWindowOwnerPID": 1,
                "kCGWindowName": "Ri",
                "kCGWindowLayer": 0,
                "kCGWindowNumber": 2,
                "kCGWindowBounds": {"X": 436, "Y": 158, "Width": 598, "Height": 640},
            }
        ],
    )
    s.expected = None
    s._ax_title = Mock(return_value=title)
    return s


def test_guard_accepts_exact_clone_and_unique_ri_window():
    assert session().guard() == WeChatWindow(2, 1, 436, 158, 598, 640)


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"front": 5}, "HUMAN_TAKEOVER"),
        ({"title": "Other"}, "HUMAN_TAKEOVER"),
        ({"path": "/Applications/WeChat.app"}, "TARGET_MISSING"),
        ({"bundle": "com.tencent.xinWeChat"}, "TARGET_MISSING"),
    ],
)
def test_other_app_or_chat_is_rejected(kwargs, code):
    with pytest.raises(NativeFailure, match=code):
        session(**kwargs).guard()


def test_bound_geometry_changes_are_rejected():
    s = session()
    s.expected = WeChatWindow(2, 1, 400, 158, 598, 640)
    with pytest.raises(NativeFailure, match="WINDOW_CHANGED"):
        s.guard()


def test_shortcut_releases_both_modifier_keys_even_if_keydown_fails():
    s = session()
    s.q.kCGEventFlagMaskControl = 1
    s.q.kCGEventFlagMaskCommand = 2
    events = []

    def key(code, down, flags):
        events.append((code, down, flags))
        if code == 0:
            raise NativeFailure("NATIVE_FAILED")

    s._key = key
    with pytest.raises(NativeFailure):
        s._chord(0, control=True)
    assert events[-2:] == [(55, False, 1), (59, False, 0)]


def test_single_send_click_uses_guard_and_no_keyboard_submission(monkeypatch):
    s = session()
    s._overlays = lambda pid: []
    s._click = Mock()
    monkeypatch.setattr("adapters.wechat_native_worker.time.sleep", lambda t: None)
    s.execute("send")
    assert s._click.call_count == 1
    assert s._click.call_args.args[1:] == (0.925, 0.955)


def test_activate_establishes_tested_layout_once(monkeypatch):
    s = session()
    s._overlays = lambda pid: []
    s.a.NSApplicationActivateIgnoringOtherApps = 1
    original = s.a.NSWorkspace.sharedWorkspace().runningApplications()[0]
    original.activateWithOptions_ = Mock()
    s.q.CGMainDisplayID = lambda: 1
    s.q.CGDisplayBounds = lambda _: NS(
        origin=NS(x=0, y=0), size=NS(width=1470, height=956)
    )
    runner = Mock(return_value=NS(returncode=0))
    monkeypatch.setattr("adapters.wechat_native_worker.subprocess.run", runner)
    monkeypatch.setattr("adapters.wechat_native_worker.time.sleep", lambda t: None)
    result = s.execute("activate")
    script = runner.call_args.args[0][-1]
    assert 'set size of window "Ri"' in script
    assert "{436, 158}" in script
    assert result["window"]["x"] == 436


def test_owned_screenshot_can_restore_ri_from_main_window_only():
    s = session(title="微信")
    s._restore_capture_focus(WeChatWindow(2, 1, 436, 158, 598, 640))
    assert s._ax_title.call_args.kwargs == {"raise_window": True}


def test_owned_screenshot_never_steals_focus_from_another_app():
    s = session(front=5)
    s._restore_capture_focus(WeChatWindow(2, 1, 436, 158, 598, 640))
    s._ax_title.assert_not_called()


def test_overlay_transition_waits_for_actual_close(monkeypatch):
    s = session()
    states = iter([[1], [1], []])
    s._overlays = lambda pid: next(states)
    sleep = Mock()
    monkeypatch.setattr("adapters.wechat_native_worker.time.sleep", sleep)
    assert s._wait_overlay_state(1, present=False)
    assert sleep.call_count == 2


def test_overlay_transition_timeout_is_bounded(monkeypatch):
    s = session()
    s._overlays = lambda pid: [1]
    sleep = Mock()
    monkeypatch.setattr("adapters.wechat_native_worker.time.sleep", sleep)
    assert not s._wait_overlay_state(1, present=False)
    assert sleep.call_count == 25


def test_native_unicode_input_preserves_surrogates_without_clipboard(monkeypatch):
    s = session()
    s._overlays = lambda pid: []
    s._click = Mock()
    events = []
    s.q.kCGHIDEventTap = 0
    s.q.CGEventCreateKeyboardEvent = lambda source, code, down: {"down": down}
    s.q.CGEventSetFlags = lambda event, flags: None
    s.q.CGEventKeyboardSetUnicodeString = lambda event, units, text: event.update(
        units=units, text=text
    )
    s.q.CGEventPost = lambda tap, event: events.append(event)
    monkeypatch.setattr("adapters.wechat_native_worker.time.sleep", lambda t: None)
    text = "中文🙂原生输入测试，保持完整。" * 2
    s.execute("type", text)
    down = [e for e in events if e["down"]]
    assert "".join(e["text"] for e in down) == text
    assert all(
        e["units"] == len(e["text"].encode("utf-16-le")) // 2 <= 16 for e in down
    )
