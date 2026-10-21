"""Verify the OCR send channel with fully injected desktop dependencies."""

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from adapters import wechat_ocr_send
from adapters.wechat_ocr import (
    OCRChatMessage,
    OCRObservation,
    WeChatOCRError,
    WeChatWindow,
)
from adapters.wechat_ocr_send import (
    RealSendingDisabled,
    WeChatOCRReplyChannel,
    WeChatOCRReplyError,
    WeChatOCRSendCancelled,
    WeChatOCRSendError,
    WeChatOCRSendUnknown,
)

CONTACT = "Ri"
IDENTITY = "desktop:wechat-ocr:Ri"
DRAFT = "好的，稍等十分钟"
WINDOW = WeChatWindow(101, 42, 0, 0, 1000, 800)


def send_observations(window: WeChatWindow) -> list[OCRObservation]:
    """Expose one synthetic send button without opening any app."""
    assert window == WINDOW
    return [OCRObservation("发送", 0.92, 0.94, 0.04, 0.02)]


class FakeReader:
    """Return fixed OCR rows for preflight reads."""

    def __init__(self, rows: list[OCRChatMessage] | None = None) -> None:
        """Store rows or default to one peer message."""
        self._rows = rows or [
            OCRChatMessage(text="对方消息", is_self=False, time_display="15:07")
        ]
        self.calls = 0

    async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
        """Return the configured rows and count invocations."""
        assert contact == CONTACT
        self.calls += 1
        return list(self._rows)


def make_channel(
    *,
    live_enabled: bool = True,
    band_before: str = "",
    band_after: str = DRAFT,
    verifier_result: bool = True,
    verifier: Any = None,
    script_fail_at: int | None = None,
    observations_reader=send_observations,
    window_finder=lambda: WINDOW,
) -> tuple[WeChatOCRReplyChannel, dict[str, Any]]:
    """Build one channel with recording fakes and return the recordings."""
    recordings: dict[str, Any] = {
        "scripts": [],
        "clipboard": [],
        "restore": [],
    }
    calls = {"scripts": 0}

    async def script_runner(source: str) -> None:
        calls["scripts"] += 1
        if script_fail_at is not None and calls["scripts"] == script_fail_at:
            raise WeChatOCRSendError("SCRIPT_FAILED")
        recordings["scripts"].append(source)

    async def clipboard_setter(content: str) -> str:
        recordings["clipboard"].append(content)
        return "旧剪贴板"

    async def clipboard_restorer(content: str) -> None:
        recordings["restore"].append(content)

    bands = iter([band_before, band_after])

    async def band_reader() -> str:
        return next(bands)

    async def default_verifier(draft: str) -> bool:
        recordings["verified_draft"] = draft
        return verifier_result

    async def fake_sleeper(seconds: float) -> None:
        return None

    channel = WeChatOCRReplyChannel(
        contact=CONTACT,
        live_enabled=live_enabled,
        reader=FakeReader(),
        script_runner=script_runner,
        clipboard_setter=clipboard_setter,
        clipboard_restorer=clipboard_restorer,
        band_reader=band_reader,
        verifier=verifier or default_verifier,
        sleeper=fake_sleeper,
        window_finder=window_finder,
        observations_reader=observations_reader,
    )
    return channel, recordings


@pytest.mark.asyncio
async def test_prepare_reply_validates_verified_chat_without_sending() -> None:
    """Dry-run preview preflights the window and never touches clipboard or keys."""
    channel, recordings = make_channel(live_enabled=False)
    await channel.prepare_reply(IDENTITY, DRAFT)
    assert recordings["scripts"] == []
    assert recordings["clipboard"] == []


@pytest.mark.asyncio
async def test_prepare_reply_rejects_wrong_recipient_or_empty_draft() -> None:
    """Recipient and draft guards match the live send path exactly."""
    channel, _ = make_channel(live_enabled=False)
    with pytest.raises(WeChatOCRReplyError) as excinfo:
        await channel.prepare_reply("desktop:wechat-ocr:别人", DRAFT)
    assert excinfo.value.code == "TARGET_MISMATCH"
    with pytest.raises(WeChatOCRReplyError) as excinfo:
        await channel.prepare_reply(IDENTITY, "   ")
    assert excinfo.value.code == "EMPTY_CONTENT"


@pytest.mark.asyncio
async def test_send_text_refuses_real_sending_unless_enabled() -> None:
    """The explicit live switch is the only way any real write can happen."""
    channel, recordings = make_channel(live_enabled=False)
    with pytest.raises(RealSendingDisabled):
        await channel.send_text(IDENTITY, DRAFT)
    assert recordings["scripts"] == []
    assert recordings["clipboard"] == []


@pytest.mark.asyncio
async def test_send_text_pastes_verifies_returns_and_confirms_receipt() -> None:
    """One paste, one Return, then an OCR receipt check mark the send sent."""
    channel, recordings = make_channel()
    await channel.send_text(IDENTITY, DRAFT)
    assert recordings["clipboard"] == [DRAFT]
    assert recordings["restore"] == ["旧剪贴板"]
    scripts = recordings["scripts"]
    assert len(scripts) == 2
    assert 'keystroke "v"' in scripts[0]
    assert 'keystroke "v"' not in scripts[1]
    assert all("com.tencent.xinWeChat2" in script for script in scripts)
    assert all("key code 36" not in script for script in scripts)
    assert recordings["verified_draft"] == DRAFT


def test_clipboard_preserves_formats_and_does_not_overwrite_new_user_copy(
    monkeypatch,
) -> None:
    """Native pasteboard generations protect a user's images and concurrent copies."""

    class Item:
        def __init__(self, values=None):
            self.values = values or {}

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def types(self):
            return list(self.values)

        def dataForType_(self, kind):
            return self.values[kind]

        def setData_forType_(self, data, kind):
            self.values[kind] = data
            return True

    class Board:
        def __init__(self):
            self.items = [
                Item({"public.png": b"image-bytes", "text/plain": b"original"})
            ]
            self.count = 10

        def changeCount(self):
            return self.count

        def pasteboardItems(self):
            return self.items

        def clearContents(self):
            self.count += 1
            self.items = []

        def setString_forType_(self, content, kind):
            self.count += 1
            self.items = [Item({kind: content.encode()})]
            return True

        def writeObjects_(self, items):
            self.count += 1
            self.items = items
            return True

    board = Board()
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board),
            NSPasteboardItem=Item,
            NSPasteboardTypeString="text/plain",
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "Foundation",
        SimpleNamespace(
            NSData=SimpleNamespace(dataWithBytes_length_=lambda data, length: data)
        ),
    )
    saved = wechat_ocr_send._replace_clipboard("draft")
    assert board.items[0].values == {"text/plain": b"draft"}
    wechat_ocr_send._restore_clipboard(saved)
    assert board.items[0].values == {
        "public.png": b"image-bytes",
        "text/plain": b"original",
    }
    saved = wechat_ocr_send._replace_clipboard("second draft")
    board.setString_forType_("new user copy", "text/plain")
    wechat_ocr_send._restore_clipboard(saved)
    assert board.items[0].values == {"text/plain": b"new user copy"}


@pytest.mark.asyncio
async def test_send_text_collapses_multiline_drafts_to_one_line() -> None:
    """Return always sends in the composer, so drafts must be single-line."""
    channel, recordings = make_channel(band_after="第一行 第二行")
    await channel.send_text(IDENTITY, "第一行\n第二行")
    assert recordings["clipboard"] == ["第一行 第二行"]


@pytest.mark.asyncio
async def test_send_text_aborts_before_return_when_paste_unverified() -> None:
    """No confirmed composer content means no Return; outcome stays certain."""
    channel, recordings = make_channel(band_after="别的内容")
    with pytest.raises(WeChatOCRSendUnknown) as excinfo:
        await channel.send_text(IDENTITY, DRAFT)
    assert excinfo.value.code == "COMPOSER_VERIFY_FAILED"
    assert excinfo.value.outcome_unknown is True
    assert len(recordings["scripts"]) == 1  # Paste only; Return never pressed.
    assert all("key code 36" not in script for script in recordings["scripts"])


@pytest.mark.asyncio
async def test_send_text_marks_unconfirmed_bubble_as_unknown() -> None:
    """A pressed Return without an observed bubble is unknown and never retried."""
    channel, recordings = make_channel(verifier_result=False)
    with pytest.raises(WeChatOCRSendUnknown) as excinfo:
        await channel.send_text(IDENTITY, DRAFT)
    assert excinfo.value.code == "RECEIPT_UNCONFIRMED"
    assert len(recordings["scripts"]) == 2


@pytest.mark.asyncio
async def test_send_text_refuses_to_overwrite_existing_composer_content() -> None:
    """A composer that is not clearly empty stops the send before any write."""
    channel, recordings = make_channel(band_before="用户写了一半的话")
    with pytest.raises(WeChatOCRSendError) as excinfo:
        await channel.send_text(IDENTITY, DRAFT)
    assert excinfo.value.code == "COMPOSER_NOT_EMPTY"
    assert excinfo.value.outcome_unknown is False
    assert recordings["clipboard"] == []
    assert recordings["scripts"] == []


@pytest.mark.asyncio
async def test_send_text_reports_preflight_mismatch_without_side_effects() -> None:
    """A chat title mismatch stops the send before clipboard or keys are touched."""

    async def bad_band() -> str:
        raise AssertionError("band must not be read after a failed preflight")

    async def failing_preflight(*, contact: str) -> list[OCRChatMessage]:
        raise WeChatOCRError("TITLE_MISMATCH")

    class FailingReader:
        async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
            return await failing_preflight(contact=contact)

    recordings: dict[str, Any] = {"scripts": [], "clipboard": []}

    async def script_runner(source: str) -> None:
        recordings["scripts"].append(source)

    async def clipboard_setter(content: str) -> str:
        recordings["clipboard"].append(content)
        return ""

    async def clipboard_restorer(content: str) -> None:
        recordings["restore"].append(content)

    channel = WeChatOCRReplyChannel(
        contact=CONTACT,
        live_enabled=True,
        reader=FailingReader(),
        script_runner=script_runner,
        clipboard_setter=clipboard_setter,
        clipboard_restorer=clipboard_restorer,
        band_reader=bad_band,
        verifier=lambda draft: None,  # type: ignore[return-value]
        sleeper=lambda seconds: None,
    )
    with pytest.raises(WeChatOCRError) as excinfo:
        await channel.send_text(IDENTITY, DRAFT)
    assert excinfo.value.code == "TITLE_MISMATCH"
    assert recordings["scripts"] == []
    assert recordings["clipboard"] == []


@pytest.mark.asyncio
async def test_send_text_cancellation_is_unknown_and_not_replayed() -> None:
    """Cancellation after the paste keeps an uncertain outcome claim."""

    async def cancelling_scripts(source: str) -> None:
        raise asyncio.CancelledError()

    recordings: dict[str, Any] = {"clipboard": [], "restore": []}

    async def clipboard_setter(content: str) -> str:
        recordings["clipboard"].append(content)
        return "旧剪贴板"

    async def clipboard_restorer(content: str) -> None:
        recordings["restore"].append(content)

    async def empty_band() -> str:
        return ""

    async def fake_sleeper(seconds: float) -> None:
        return None

    channel = WeChatOCRReplyChannel(
        contact=CONTACT,
        live_enabled=True,
        reader=FakeReader(),
        script_runner=cancelling_scripts,
        clipboard_setter=clipboard_setter,
        clipboard_restorer=clipboard_restorer,
        band_reader=empty_band,
        verifier=lambda draft: None,  # type: ignore[return-value]
        sleeper=fake_sleeper,
        window_finder=lambda: WINDOW,
        observations_reader=send_observations,
    )
    with pytest.raises((WeChatOCRSendCancelled, asyncio.CancelledError)):
        await channel.send_text(IDENTITY, DRAFT)
    assert recordings["clipboard"] == [DRAFT]
    assert recordings["restore"] == ["旧剪贴板"]


@pytest.mark.asyncio
async def test_composer_with_send_word_is_still_a_user_draft() -> None:
    """The visible send label cannot excuse text already present in the editor."""
    channel, recordings = make_channel(band_before="这段发送给客户的草稿")
    with pytest.raises(WeChatOCRSendError) as error:
        await channel.send_text(IDENTITY, DRAFT)
    assert error.value.code == "COMPOSER_NOT_EMPTY"
    assert recordings["clipboard"] == recordings["scripts"] == []


@pytest.mark.asyncio
async def test_similar_pasted_text_cannot_authorize_submit() -> None:
    """Extra content or changed punctuation stops before the send-button click."""
    for content in [DRAFT + "多余文字", "好的稍等十分钟"]:
        channel, recordings = make_channel(band_after=content)
        with pytest.raises(WeChatOCRSendUnknown):
            await channel.send_text(IDENTITY, DRAFT)
        assert len(recordings["scripts"]) == 1


@pytest.mark.asyncio
async def test_missing_send_button_never_submits() -> None:
    """Typing is not followed by a guessed click when the button is missing."""
    channel, recordings = make_channel(observations_reader=lambda window: [])
    with pytest.raises(WeChatOCRSendUnknown) as error:
        await channel.send_text(IDENTITY, DRAFT)
    assert error.value.code == "SEND_BUTTON_MISSING"
    assert len(recordings["scripts"]) == 1


@pytest.mark.asyncio
async def test_changed_geometry_stops_before_paste() -> None:
    """A moved wx2 window cannot redirect the queued mouse action."""
    windows = iter([WINDOW, WeChatWindow(101, 42, 50, 0, 1000, 800)])
    channel, recordings = make_channel(window_finder=lambda: next(windows))
    with pytest.raises(WeChatOCRSendUnknown):
        await channel.send_text(IDENTITY, DRAFT)
    assert recordings["scripts"] == []


@pytest.mark.asyncio
async def test_select_contact_rejects_duplicate_sidebar_names() -> None:
    """Multiple Ri rows never become a first-result click."""
    observations = [
        OCRObservation("Other", 0.5, 0.02, 0.1, 0.02),
        OCRObservation("Ri", 0.1, 0.2, 0.03, 0.02),
        OCRObservation("Ri", 0.1, 0.4, 0.03, 0.02),
    ]
    channel, recordings = make_channel(observations_reader=lambda window: observations)
    with pytest.raises(WeChatOCRReplyError) as error:
        await channel.select_contact()
    assert error.value.code == "CONTACT_AMBIGUOUS"
    assert recordings["scripts"] == recordings["clipboard"] == []


@pytest.mark.asyncio
async def test_select_contact_clicks_only_the_unique_exact_sidebar_row() -> None:
    """Selecting the bound peer is followed by the existing exact-chat preflight."""
    observations = [
        OCRObservation("Other", 0.5, 0.02, 0.1, 0.02),
        OCRObservation("Ri", 0.1, 0.2, 0.03, 0.02),
    ]
    channel, recordings = make_channel(observations_reader=lambda window: observations)
    await channel.select_contact()
    assert len(recordings["scripts"]) == 1
    assert "click at {115, 168}" in recordings["scripts"][0]
    assert recordings["clipboard"] == []


@pytest.mark.asyncio
async def test_dry_run_never_searches_or_selects_contacts() -> None:
    """The preview boundary stays read-only even when selection is requested."""
    channel, recordings = make_channel(live_enabled=False)
    with pytest.raises(RealSendingDisabled):
        await channel.select_contact()
    assert recordings["scripts"] == recordings["clipboard"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("append_receipt", [False, True])
async def test_default_receipt_requires_new_outgoing_bubble(
    append_receipt: bool,
) -> None:
    """An identical historical own bubble is insufficient; only an append proves submission."""
    old = [OCRChatMessage(DRAFT, True, "15:07")]

    class ReceiptReader:
        def __init__(self):
            self.calls = 0

        async def read_chat(self, *, contact):
            self.calls += 1
            return old + (
                [OCRChatMessage(DRAFT, True, "15:08")]
                if append_receipt and self.calls >= 4
                else []
            )

    bands = iter(["", DRAFT])
    scripts = []

    async def run(source):
        scripts.append(source)

    async def band():
        return next(bands)

    async def put(text):
        return "original"

    async def restore(state):
        return None

    async def sleep(seconds):
        return None

    channel = WeChatOCRReplyChannel(
        contact="Ri",
        live_enabled=True,
        reader=ReceiptReader(),
        script_runner=run,
        clipboard_setter=put,
        clipboard_restorer=restore,
        band_reader=band,
        window_finder=lambda: WINDOW,
        observations_reader=send_observations,
        sleeper=sleep,
    )
    if append_receipt:
        await channel.send_text(IDENTITY, DRAFT)
    else:
        with pytest.raises(WeChatOCRSendUnknown):
            await channel.send_text(IDENTITY, DRAFT)
    assert len(scripts) == 2
