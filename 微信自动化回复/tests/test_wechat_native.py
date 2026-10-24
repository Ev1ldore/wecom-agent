"""Exercise native receive/send guards with no UI, network or real messages."""

import asyncio
import base64
import io
import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from adapters.wechat_native import (
    IDENTITY,
    NativeError,
    NativeRiChat,
    SubprocessNativeBackend,
    join_history_views,
    mark_green_bubbles,
    parse_native_frame,
)
from adapters.wechat_ocr import OCRChatMessage, WeChatWindow
from adapters.wechat_ocr import OCRObservation as O

WINDOW = WeChatWindow(50, 60, 436, 158, 598, 640)
DRAFT = "测试回复。"


def rows(*, draft="", extra=False):
    result = [
        O("Ri", 0.025, 0.077, 0.04, 0.02),
        O("18:36", 0.47, 0.2, 0.06, 0.02),
        O("你好", 0.137, 0.3, 0.08, 0.025),
        O("发送", 0.90, 0.943, 0.05, 0.025),
    ]
    if draft:
        result.append(O(draft, 0.04, 0.81, 0.4, 0.03))
    if extra:
        result.append(O(DRAFT, 0.65, 0.65, 0.23, 0.025, bubble_side="self"))
    return result


class Backend:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.ops = []

    async def request(self, op, *, window=None, text=""):
        self.ops.append((op, text))
        result = {"ok": True, "window": asdict(WINDOW)}
        if op == "capture":
            value = next(self.frames)
            if isinstance(value, Exception):
                raise value
            result["png"] = base64.b64encode(value).decode()
        return result


def chat(frames, mapping, *, live=True):
    backend = Backend(frames)
    session = NativeRiChat(
        backend, live=live, recognizer=lambda b: mapping[b], color_marker=lambda b, r: r
    )
    return session, backend


def test_popout_parser_retains_peer_and_raw_minute():
    frame = parse_native_frame(WINDOW, rows())
    assert frame.messages[0].text == "你好"
    assert frame.messages[0].time_display == "18:36"
    assert not frame.messages[0].is_self
    assert frame.composer == ""
    assert frame.send_button


def test_verified_empty_composer_placeholder_is_not_a_human_draft():
    frame = parse_native_frame(
        WINDOW, [*rows(), O("按住鼠标语音输入文字", 0.04, 0.81, 0.4, 0.025)]
    )
    assert frame.composer == ""
    assert frame.send_button


def test_avatar_ocr_does_not_become_a_message_or_block_a_receipt():
    frame = parse_native_frame(
        WINDOW,
        [
            *rows(extra=True),
            O("面", 0.896, 0.452, 0.073, 0.066, 0.209),
            O("头像", 0.025, 0.3, 0.06, 0.04, 0.15),
        ],
    )
    assert [m.text for m in frame.messages] == ["你好", DRAFT]
    assert frame.messages[-1].is_self


def test_scroll_edge_partial_line_is_not_a_complete_peer_message():
    frame = parse_native_frame(
        WINDOW, [*rows(extra=True), O("4", 0.138, 0.766, 0.069, 0.013, 0.116)]
    )
    assert [m.text for m in frame.messages] == ["你好", DRAFT]


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda r: [replace(r[0], text="Other"), *r[1:]], "TITLE_MISMATCH"),
        (lambda r: [r[0], *r[2:]], "NO_TIME_ANCHOR"),
        (lambda r: [*r, O("系统提示", 0.4, 0.4, 0.2, 0.02)], "AMBIGUOUS_LAYOUT"),
        (lambda r: [*r, replace(r[2], confidence=0.2)], "AMBIGUOUS_LAYOUT"),
        (lambda r: [*r, O("越界", 1, 0.5, 0.2, 0.02)], "AMBIGUOUS_LAYOUT"),
        (lambda r: [*r, replace(r[3], x=0.86)], "LAYOUT_UNSUPPORTED"),
    ],
)
def test_ambiguous_or_wrong_layout_stops(change, code):
    with pytest.raises(NativeError) as error:
        parse_native_frame(WINDOW, change(rows()))
    assert error.value.code == code


def test_unanchored_old_content_never_gets_a_fabricated_timestamp():
    frame = parse_native_frame(WINDOW, [*rows(), O("旧消息", 0.13, 0.15, 0.1, 0.02)])
    assert [m.text for m in frame.messages] == ["你好"]


def test_clipped_old_bubble_and_footer_icons_are_not_chat_title_or_messages():
    frame = parse_native_frame(
        WINDOW,
        [
            *rows(),
            O("残缺旧气泡", 0.25, 0.122, 0.1, 0.016, bubble_side="self"),
            O("1", 0.83, 0.94, 0.02, 0.025, 0.27),
        ],
    )
    assert [m.text for m in frame.messages] == ["你好"]


def test_low_confidence_clipped_top_text_is_not_a_complete_message():
    frame = parse_native_frame(WINDOW, [*rows(), O("残", 0.25, 0.122, 0.1, 0.016, 0.1)])
    assert [m.text for m in frame.messages] == ["你好"]


def test_page_overlap_requires_a_unique_exact_anchor():
    a, b, c = (OCRChatMessage(text, False, "18:36") for text in ("a", "b", "c"))
    assert join_history_views((a, b), (b, c)) == (a, b, c)
    assert join_history_views((a,), (c,)) is None
    assert join_history_views((a, b, b), (b, c)) is None
    assert join_history_views((a, b), (b, a, b)) is None


@pytest.mark.asyncio
async def test_truncated_view_is_restored_before_returning_new_tail():
    old = rows(extra=True)
    latest = [r for r in old if r.text != "你好"] + [O("新增", 0.13, 0.73, 0.1, 0.025)]
    s, b = chat(
        [b"old", b"latest", b"old", b"latest"], {b"old": old, b"latest": latest}
    )
    original = await s.snapshot()
    restored = await s.snapshot()
    assert restored.messages[: len(original.messages)] == original.messages
    assert restored.messages[-1].text == "新增"
    assert [op for op, _ in b.ops] == [
        "capture",
        "capture",
        "scroll_up",
        "capture",
        "scroll_bottom",
        "capture",
    ]


@pytest.mark.asyncio
async def test_missing_saved_prefix_never_advances_history():
    old = rows(extra=True)
    latest = [r for r in rows() if r.text != "你好"] + [
        O("新增", 0.13, 0.7, 0.1, 0.025)
    ]
    s, b = chat(
        [b"old", b"latest", *([b"latest"] * 10)], {b"old": old, b"latest": latest}
    )
    original = await s.snapshot()
    with pytest.raises(NativeError) as error:
        await s.snapshot()
    assert error.value.code == "HISTORY_INCOMPLETE"
    assert s._history == original.messages
    assert b.ops[-1][0] == "scroll_bottom"
    assert "type" not in [op for op, _ in b.ops]


def test_wrapped_lines_join_but_distinct_bubbles_remain_distinct():
    frame = parse_native_frame(
        WINDOW,
        [
            *rows(),
            O("第二行", 0.137, 0.335, 0.15, 0.025),
            O("下一条", 0.137, 0.45, 0.15, 0.025),
        ],
    )
    assert [m.text for m in frame.messages] == ["你好\n第二行", "下一条"]


def test_dark_green_bubble_is_self_and_gray_is_not():
    picture = Image.new("RGB", (200, 200), (118, 118, 118))
    for x in range(40, 180):
        for y in range(40, 80):
            picture.putpixel((x, y), (72, 123, 72))
    image = io.BytesIO()
    picture.save(image, format="PNG")
    output = mark_green_bubbles(
        image.getvalue(),
        [O("自己", 0.25, 0.23, 0.5, 0.12), O("对方", 0.13, 0.5, 0.2, 0.1)],
    )
    assert output[0].bubble_side == "self"
    assert output[1].bubble_side is None


@pytest.mark.asyncio
async def test_live_single_type_and_send_require_new_own_bubble():
    s, b = chat(
        [b"before", b"draft", b"after"],
        {b"before": rows(), b"draft": rows(draft=DRAFT), b"after": rows(extra=True)},
    )
    await s.send_text(IDENTITY, DRAFT)
    assert [op for op, _ in b.ops] == ["capture", "type", "capture", "send", "capture"]
    assert b.ops[1][1] == DRAFT


@pytest.mark.asyncio
async def test_existing_draft_prevents_type_and_send():
    s, b = chat([b"before"], {b"before": rows(draft="人工草稿")})
    with pytest.raises(NativeError) as error:
        await s.send_text(IDENTITY, DRAFT)
    assert error.value.code == "COMPOSER_NOT_EMPTY"
    assert not error.value.outcome_unknown
    assert b.ops == [("capture", "")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination,live,code",
    [
        (IDENTITY, False, "SENDING_DISABLED"),
        ("other", True, "TARGET_MISMATCH"),
    ],
)
async def test_unauthorized_send_never_touches_ui(destination, live, code):
    s, b = chat([], {}, live=live)
    with pytest.raises(NativeError) as error:
        await s.send_text(destination, DRAFT)
    assert error.value.code == code
    assert b.ops == []


@pytest.mark.asyncio
async def test_type_mismatch_is_unknown_and_never_submits():
    s, b = chat([b"before", b"bad"], {b"before": rows(), b"bad": rows(draft="错误")})
    with pytest.raises(NativeError) as error:
        await s.send_text(IDENTITY, DRAFT)
    assert error.value.outcome_unknown
    assert error.value.code == "COMPOSER_VERIFY_FAILED"
    assert "send" not in [op for op, _ in b.ops]


@pytest.mark.asyncio
async def test_old_identical_bubble_does_not_count_as_receipt():
    s, b = chat(
        [b"old", b"draft", b"old"],
        {b"old": rows(extra=True), b"draft": rows(extra=True, draft=DRAFT)},
    )
    with pytest.raises(NativeError) as error:
        await s.send_text(IDENTITY, DRAFT)
    assert error.value.outcome_unknown
    assert error.value.code == "RECEIPT_UNCONFIRMED"
    assert [op for op, _ in b.ops].count("send") == 1


@pytest.mark.asyncio
async def test_changed_history_after_type_never_submits():
    s, b = chat(
        [b"before", b"changed"],
        {b"before": rows(), b"changed": rows(extra=True, draft=DRAFT)},
    )
    with pytest.raises(NativeError):
        await s.send_text(IDENTITY, DRAFT)
    assert "send" not in [op for op, _ in b.ops]


@pytest.mark.asyncio
async def test_capture_failure_is_not_retried():
    s, b = chat([NativeError("CAPTURE_FAILED"), b"ok"], {b"ok": rows()})
    with pytest.raises(NativeError, match="CAPTURE_FAILED"):
        await s.snapshot()
    assert len(b.ops) == 1


@pytest.mark.asyncio
async def test_human_takeover_is_never_retried():
    s, b = chat([NativeError("HUMAN_TAKEOVER")], {})
    with pytest.raises(NativeError):
        await s.snapshot()
    assert len(b.ops) == 1


@pytest.mark.asyncio
async def test_cancellation_during_write_preserves_unknown():
    s, b = chat([b"before"], {b"before": rows()})
    original = b.request

    async def cancel(op, **kwargs):
        if op == "type":
            raise asyncio.CancelledError()
        return await original(op, **kwargs)

    b.request = cancel
    with pytest.raises(asyncio.CancelledError) as error:
        await s.send_text(IDENTITY, DRAFT)
    assert error.value.outcome_unknown


@pytest.mark.asyncio
async def test_inbox_preserves_minute_time_and_filters_self_identity():
    s, _ = chat([b"ok"], {b"ok": rows(extra=True)})
    messages = await s.poll_unread()
    assert messages[0].ts is None and messages[0].time_display == "18:36"
    assert messages[0].sender == IDENTITY
    assert messages[1].is_self and messages[1].sender == "desktop:self"


@pytest.mark.asyncio
async def test_backend_rejects_private_worker_error(monkeypatch):
    process = AsyncMock()
    process.communicate.return_value = (b'{"ok":false,"code":"SECRET"}', b"")
    process.returncode = 1
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    with pytest.raises(NativeError) as error:
        await SubprocessNativeBackend().request("capture")
    assert error.value.code == "NATIVE_FAILED"
    assert "SECRET" not in str(error.value)


@pytest.mark.asyncio
async def test_backend_serializes_binding_without_putting_draft_in_argv(monkeypatch):
    process = AsyncMock()
    process.communicate.return_value = (b'{"ok":true}', b"")
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await SubprocessNativeBackend().request("type", window=WINDOW, text=DRAFT)
    assert DRAFT not in str(spawn.call_args)
    assert json.loads(process.communicate.call_args.args[0])["text"] == DRAFT
    assert b'"op": "type"' in process.communicate.call_args.args[0]


@pytest.mark.asyncio
async def test_backend_cancellation_allows_owned_ui_cleanup_before_hard_kill(
    monkeypatch,
):
    process = Mock()
    process.returncode = None
    process.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")])
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    hard_kill = AsyncMock()
    monkeypatch.setattr("adapters.wechat_native.stop_cli_process", hard_kill)
    with pytest.raises(asyncio.CancelledError):
        await SubprocessNativeBackend().request("capture")
    process.terminate.assert_called_once()
    assert process.communicate.await_count == 2
    hard_kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_no_history_recovery_avoids_scroll_and_capture_burst():
    s, backend = chat(
        [b"before", b"after"], {b"before": rows(), b"after": rows(extra=True)}
    )
    s.recover_history = False
    await s.snapshot()
    s._history = (*s._history, OCRChatMessage("missing old message", False, "18:36"))
    with pytest.raises(NativeError, match="HISTORY_INCOMPLETE"):
        await s.snapshot()
    assert [op for op, _ in backend.ops] == ["capture", "capture"]


def test_tall_composer_hint_does_not_become_peer_or_ambiguous_layout():
    observations = [
        *rows(),
        O("13:18", 0.47, 0.5867, 0.06, 0.023),
        O("低频验收1024，请只回复你好", 0.135, 0.6594, 0.32, 0.026),
        O("按住鼠标语音输入文字", 0.032, 0.7422, 0.25, 0.026),
    ]
    frame = parse_native_frame(WINDOW, observations)
    assert frame.composer == ""
    assert frame.messages[-1].text == "低频验收1024，请只回复你好"
    assert frame.messages[-1].time_display == "13:18"
    assert not frame.messages[-1].is_self


@pytest.mark.asyncio
async def test_verified_tall_composer_boundary_is_reused_after_typing():
    before = [*rows(), O("按住鼠标语音输入文字", 0.032, 0.7422, 0.25, 0.026)]
    typed = [*rows(), O(DRAFT, 0.032, 0.7422, 0.25, 0.026)]
    session, backend = chat([b"before", b"typed"], {b"before": before, b"typed": typed})
    initial = await session.snapshot()
    result = await session.snapshot()
    assert initial.composer == ""
    assert result.composer == DRAFT
    assert result.messages == initial.messages
    assert [op for op, _ in backend.ops] == ["capture", "capture"]
