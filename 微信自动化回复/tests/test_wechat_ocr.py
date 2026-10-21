"""Verify the OCR observation parser and inbox mapping without any desktop I/O."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters import wechat_ocr
from adapters.wechat_ocr import (
    DesktopWeChatOCRReader,
    OCRChatMessage,
    OCRObservation,
    WeChatOCRError,
    WeChatOCRInbox,
    WxCLIChangeProbe,
    chat_identity,
    normalize_text,
    parse_chat_observations,
    verify_own_bubble,
)

CONTACT = "孟女士 爱回收 人事"


def _ocr_test_png(*, color="white", marked=True):
    """Create a local PNG without retaining any private chat image."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 1000), color)
    if marked:
        ImageDraw.Draw(image).rectangle((20, 20, 80, 80), fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _rapid_output(rows):
    """Represent the installed RapidOCR 3.x output contract."""
    return SimpleNamespace(
        boxes=[
            [
                [o.x * 1000, o.y * 1000],
                [(o.x + o.w) * 1000, o.y * 1000],
                [(o.x + o.w) * 1000, (o.y + o.h) * 1000],
                [o.x * 1000, (o.y + o.h) * 1000],
            ]
            for o in rows
        ],
        txts=tuple(o.text for o in rows),
        scores=tuple(o.confidence for o in rows),
    )


def _mock_rapidocr(monkeypatch, result):
    """Inject the native inference boundary while keeping parsing real."""
    from unittest.mock import Mock

    engine = Mock(return_value=result)
    monkeypatch.setattr(wechat_ocr, "_load_rapidocr", lambda: engine)
    bitmap = SimpleNamespace(pixelsWide=lambda: 1000, pixelsHigh=lambda: 1000)
    monkeypatch.setattr(
        wechat_ocr,
        "_load_appkit",
        lambda: SimpleNamespace(
            NSBitmapImageRep=SimpleNamespace(imageRepWithData_=lambda png: bitmap)
        ),
    )
    monkeypatch.setattr(wechat_ocr, "_green_bubble", lambda bitmap, o: False)
    return engine


def test_rapidocr_boxes_reach_existing_message_parser(monkeypatch):
    """Pixel boxes retain title, original times, ownership and merged lines."""
    engine = _mock_rapidocr(monkeypatch, _rapid_output(chat_view()))
    png = _ocr_test_png()
    observations = wechat_ocr.ocr_png(png)
    parsed = parse_chat_observations(observations, contact=CONTACT)
    assert [m.text for m in parsed.messages] == [
        "我是爱回收人事孟女士",
        "今天下午都行",
        "好的稍等\n十分钟打给您",
    ]
    assert [m.is_self for m in parsed.messages] == [False, True, False]
    assert parsed.messages[0].time_display == "15:07"
    engine.assert_called_once_with(png)


def test_rapidocr_retains_pixel_verified_long_own_bubble(monkeypatch):
    """A long own bubble cannot become a peer question after backend replacement."""
    row = OCRObservation("本人回复", 0.48, 0.4, 0.43, 0.025, 0.97)
    _mock_rapidocr(monkeypatch, _rapid_output([row]))
    monkeypatch.setattr(wechat_ocr, "_green_bubble", lambda bitmap, o: True)
    observation = wechat_ocr.ocr_png(_ocr_test_png())[0]
    assert observation.bubble_side == "self"
    assert observation.confidence == 0.97
    assert observation.x == pytest.approx(0.48)
    assert observation.y == pytest.approx(0.4)


@pytest.mark.parametrize("color", ["white", "black", "gray"])
def test_uniform_capture_stops_before_inference(monkeypatch, color):
    """A blank capture is distinguished from a recognizer failure."""
    engine = _mock_rapidocr(monkeypatch, _rapid_output(chat_view()))
    with pytest.raises(WeChatOCRError) as error:
        wechat_ocr.ocr_png(_ocr_test_png(color=color, marked=False))
    assert error.value.code == "CAPTURE_EMPTY"
    engine.assert_not_called()


def test_invalid_png_does_not_reach_local_engine(monkeypatch):
    """The local OCR API accepts image bytes, never paths or URLs."""
    engine = _mock_rapidocr(monkeypatch, _rapid_output(chat_view()))
    with pytest.raises(WeChatOCRError) as error:
        wechat_ocr.ocr_png(b"https://example.com/private.png")
    assert error.value.code == "CAPTURE_FAILED"
    engine.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("boxes", None),
        ("txts", None),
        ("scores", None),
        ("boxes", []),
        ("scores", []),
        ("scores", [float("nan")]),
        ("scores", [1.1]),
        ("boxes", [[[0, 0], [100, 0], [100, 100]]]),
        ("boxes", [[[0, 0], [1100, 0], [1100, 100], [0, 100]]]),
        ("boxes", [[[0, 0], [0, 0], [0, 0], [0, 0]]]),
        ("txts", ("",)),
    ],
)
def test_rapidocr_invalid_results_fail_closed(monkeypatch, field, value):
    """Malformed boxes and scores cannot authorize contact selection or sending."""
    result = _rapid_output([obs("Ri", 0.46, 0.02, 0.1)])
    setattr(result, field, value)
    _mock_rapidocr(monkeypatch, result)
    with pytest.raises(WeChatOCRError) as error:
        wechat_ocr.ocr_png(_ocr_test_png())
    assert error.value.code == "OCR_FAILED"


def test_rapidocr_engine_failure_hides_native_diagnostics(monkeypatch):
    """Inference failures expose no input text or library exception payload."""
    engine = _mock_rapidocr(monkeypatch, _rapid_output(chat_view()))
    engine.side_effect = RuntimeError("private chat content")
    with pytest.raises(WeChatOCRError) as error:
        wechat_ocr.ocr_png(_ocr_test_png())
    assert error.value.code == "OCR_FAILED"
    assert "private" not in str(error.value)


def test_rapidocr_requires_bundled_models_and_explicit_local_paths(
    monkeypatch, tmp_path
):
    """Missing models stop setup instead of triggering a runtime download."""
    import sys
    from unittest.mock import Mock

    models = tmp_path / "models"
    models.mkdir()
    factory = Mock()
    disable_telemetry = Mock()
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(disable_telemetry_events=disable_telemetry),
    )
    monkeypatch.setitem(
        sys.modules,
        "rapidocr",
        SimpleNamespace(__file__=str(tmp_path / "__init__.py"), RapidOCR=factory),
    )
    wechat_ocr._load_rapidocr.cache_clear()
    try:
        with pytest.raises(WeChatOCRError) as error:
            wechat_ocr._load_rapidocr()
        assert error.value.code == "OCR_SETUP"
        factory.assert_not_called()
        for name in (
            "PP-OCRv6_det_small.onnx",
            "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "PP-OCRv6_rec_small.onnx",
        ):
            (models / name).write_bytes(b"local model")
        assert wechat_ocr._load_rapidocr() is factory.return_value
        params = factory.call_args.kwargs["params"]
        for stage in ("Det", "Cls", "Rec"):
            assert Path(params[f"{stage}.model_path"]).is_file()
        assert params["EngineConfig.onnxruntime.use_coreml"] is False
        assert disable_telemetry.called
    finally:
        wechat_ocr._load_rapidocr.cache_clear()


def test_fully_transparent_capture_stops_even_with_varied_rgb(monkeypatch):
    """Hidden RGB values in transparent pixels cannot authorize an OCR read."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    engine = _mock_rapidocr(monkeypatch, _rapid_output(chat_view()))
    image = Image.new("RGBA", (1000, 1000), (255, 255, 255, 0))
    ImageDraw.Draw(image).rectangle((20, 20, 80, 80), fill=(0, 0, 0, 0))
    data = BytesIO()
    image.save(data, format="PNG")
    with pytest.raises(WeChatOCRError) as error:
        wechat_ocr.ocr_png(data.getvalue())
    assert error.value.code == "CAPTURE_EMPTY"
    engine.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["CAPTURE_EMPTY", "OCR_SETUP"])
async def test_unusable_capture_or_missing_engine_stops_without_retry(monkeypatch, code):
    """Permanent capture/setup errors never trigger further desktop reads."""
    from unittest.mock import Mock

    capture = Mock(return_value=b"injected capture")
    recognize = Mock(side_effect=WeChatOCRError(code))
    reader = DesktopWeChatOCRReader(
        window_finder=lambda: 1, capture=capture, ocr=recognize
    )
    with pytest.raises(WeChatOCRError) as error:
        await reader.read_chat(contact="Ri")
    assert error.value.code == code
    capture.assert_called_once()
    recognize.assert_called_once()


def obs(text: str, x: float, y: float, w: float, h: float = 0.02) -> OCRObservation:
    """Build one normalized top-left-origin observation."""
    return OCRObservation(text=text, x=x, y=y, w=w, h=h)


def chat_view() -> list[OCRObservation]:
    """Reproduce one realistic WeChat window layout in normalized coordinates."""
    return [
        obs("搜索", 0.10, 0.02, 0.04),  # Sidebar search box.
        obs(CONTACT, 0.46, 0.02, 0.18),  # Chat title band.
        obs("腾讯新闻", 0.10, 0.30, 0.10),  # Sidebar row.
        obs("未 anchored", 0.40, 0.09, 0.12),  # Above every separator.
        obs("15:07", 0.665, 0.14, 0.04),  # Time separator (center 0.65).
        obs("我是爱回收人事孟女士", 0.48, 0.18, 0.20),  # Peer bubble.
        obs("今天下午都行", 0.80, 0.24, 0.09),  # Own bubble (right edge 0.89).
        obs("你已添加了对方", 0.52, 0.30, 0.26),  # Centered system line.
        obs("14:57", 0.665, 0.36, 0.04),
        obs("好的稍等", 0.48, 0.40, 0.10),  # Peer bubble, first line.
        obs("十分钟打给您", 0.48, 0.423, 0.11),  # Same bubble, merged line.
        obs("发送", 0.92, 0.94, 0.03),  # Composer button, excluded zone.
    ]


def test_parse_classifies_sides_and_merges_multiline_bubbles() -> None:
    """Peer, own and multiline bubbles map to verified chat messages."""
    result = parse_chat_observations(chat_view(), contact=CONTACT)
    assert [m.text for m in result.messages] == [
        "我是爱回收人事孟女士",
        "今天下午都行",
        "好的稍等\n十分钟打给您",
    ]
    sides = {m.text: m.is_self for m in result.messages}
    assert sides["我是爱回收人事孟女士"] is False
    assert sides["今天下午都行"] is True
    assert sides["好的稍等\n十分钟打给您"] is False


def test_parse_skips_unanchored_bubbles_without_fabricating_time() -> None:
    """A bubble above every visible separator is counted, never time-stamped."""
    result = parse_chat_observations(chat_view(), contact=CONTACT)
    assert result.stats.unanchored == 1
    assert all(m.time_display for m in result.messages)
    assert "未 anchored" not in [m.text for m in result.messages]


def test_parse_uses_visible_separator_label_as_minute_anchor() -> None:
    """The original on-screen label is preserved for every anchored bubble."""
    result = parse_chat_observations(chat_view(), contact=CONTACT)
    anchored = next(m for m in result.messages if "稍等" in m.text)
    assert anchored.time_display == "14:57"


def test_parse_skips_sidebar_system_and_composer_lines_visibly() -> None:
    """Sidebar rows, system lines and the composer zone never become messages."""
    result = parse_chat_observations(chat_view(), contact=CONTACT)
    texts = [m.text for m in result.messages]
    assert "腾讯新闻" not in texts
    assert "你已添加了对方" not in texts
    assert "搜索" not in texts
    assert "发送" not in texts
    assert result.stats.sidebar == 1  # Sidebar row; 搜索 sits in the title band.
    assert result.stats.system == 1
    assert result.stats.ambiguous == 0


def test_parse_rejects_title_mismatch() -> None:
    """A different open chat stops the read instead of replying to a stranger."""
    with pytest.raises(WeChatOCRError) as excinfo:
        parse_chat_observations(chat_view(), contact="另一个人")
    assert excinfo.value.code == "TITLE_MISMATCH"


def test_parse_fails_closed_without_time_anchors() -> None:
    """No visible separator means no time reference; nothing is read."""
    view = [o for o in chat_view() if o.text not in {"15:07", "14:57"}]
    with pytest.raises(WeChatOCRError) as excinfo:
        parse_chat_observations(view, contact=CONTACT)
    assert excinfo.value.code == "NO_TIME_ANCHOR"


def test_parse_rejects_malformed_or_oversized_input() -> None:
    """Malformed OCR output fails closed instead of yielding partial messages."""
    with pytest.raises(WeChatOCRError):
        parse_chat_observations(["not-an-observation"], contact=CONTACT)  # type: ignore[list-item]
    with pytest.raises(WeChatOCRError):
        parse_chat_observations(
            [OCRObservation("x", 0.5, 0.5, 0.1, 0.02)] * 5000, contact=CONTACT
        )


def test_normalize_text_preserves_punctuation_and_case() -> None:
    """Only whitespace is ignored; critical punctuation and identifiers survive."""
    assert normalize_text("你好， 世界！") == "你好，世界！"
    assert normalize_text("Ri") != normalize_text("ri")
    assert normalize_text("") == ""


def test_chat_identity_requires_verified_contact() -> None:
    """The chat identity is derived from the configured display name only."""
    assert chat_identity(" Ri ") == "desktop:wechat-ocr:Ri"
    with pytest.raises(ValueError):
        chat_identity("   ")


class FakeReader:
    """Return fixed OCR rows without touching the window server."""

    def __init__(self, rows: list[OCRChatMessage]) -> None:
        """Store the deterministic read result."""
        self._rows = rows
        self.calls = 0

    async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
        """Return the configured rows and count invocations."""
        assert contact == "Ri"
        self.calls += 1
        return list(self._rows)


@pytest.mark.asyncio
async def test_inbox_maps_own_and_peer_bubbles_to_normalized_messages() -> None:
    """Own bubbles keep the self identity for recovery anchoring in the tail."""
    reader = FakeReader(
        [
            OCRChatMessage(text="对方消息", is_self=False, time_display="15:07"),
            OCRChatMessage(text="本人消息", is_self=True, time_display="15:07"),
        ]
    )
    inbox = WeChatOCRInbox(contact="Ri", reader=reader)  # type: ignore[arg-type]
    messages = await inbox.baseline()
    assert messages == await inbox.poll_unread()
    peer, own = messages
    assert peer.chat == peer.sender == "desktop:wechat-ocr:Ri"
    assert peer.time_precision == "minute"
    assert peer.time_display == "15:07"
    assert peer.is_self is False
    assert own.sender == "desktop:self"
    assert own.is_self is True
    assert reader.calls == 2


class FlakyCapture:
    """Fail once with a transient error, then return a fixed PNG marker."""

    def __init__(self) -> None:
        """Start in the failing state."""
        self.calls = 0

    def __call__(self, window_id: int) -> bytes:
        """Raise CAPTURE_FAILED once, then succeed."""
        self.calls += 1
        if self.calls == 1:
            raise WeChatOCRError("CAPTURE_FAILED")
        return b"png"


@pytest.mark.asyncio
async def test_reader_retries_transient_failures_once() -> None:
    """Capture/OCR errors retry once and then observe the verified chat."""
    capture = FlakyCapture()
    reader = DesktopWeChatOCRReader(
        window_finder=lambda: 1,
        capture=capture,
        ocr=lambda png: chat_view(),
    )
    messages = await reader.read_chat(contact=CONTACT)
    assert len(messages) == 3
    assert capture.calls == 3


@pytest.mark.asyncio
async def test_reader_never_retries_layout_guards() -> None:
    """A missing window or wrong chat fails immediately without a second pass."""

    def missing_window() -> int:
        raise WeChatOCRError("NO_WINDOW")

    reader = DesktopWeChatOCRReader(
        window_finder=missing_window,
        capture=lambda window_id: b"png",
        ocr=lambda png: chat_view(),
    )
    with pytest.raises(WeChatOCRError) as excinfo:
        await reader.read_chat(contact=CONTACT)
    assert excinfo.value.code == "NO_WINDOW"

    def wrong_title(*, contact: str) -> list[OCRChatMessage]:
        raise WeChatOCRError("TITLE_MISMATCH")

    class RawFakeReader:
        async def read_chat(self, *, contact: str) -> list[OCRChatMessage]:
            return wrong_title(contact=contact)

    inbox = WeChatOCRInbox(contact=CONTACT, reader=RawFakeReader())  # type: ignore[arg-type]
    with pytest.raises(WeChatOCRError) as excinfo:
        await inbox.poll_unread()
    assert excinfo.value.code == "TITLE_MISMATCH"


@pytest.mark.asyncio
async def test_verify_own_bubble_matches_normalized_draft() -> None:
    """A receipt requires a new bubble, exact punctuation and a pre-send snapshot."""
    reader = FakeReader(
        [OCRChatMessage(text="好的，稍等 十分钟", is_self=True, time_display="15:07")]
    )
    assert (
        await verify_own_bubble(
            reader, contact="Ri", draft="好的，稍等十分钟", before=[]
        )
        is True
    )
    assert (
        await verify_own_bubble(reader, contact="Ri", draft="完全不同", before=[])
        is False
    )
    assert not await verify_own_bubble(
        reader, contact="Ri", draft="好的，稍等十分钟", before=reader._rows
    )
    assert not await verify_own_bubble(
        reader, contact="Ri", draft="好的稍等十分钟", before=[]
    )
    assert not await verify_own_bubble(reader, contact="Ri", draft="好的，稍等十分钟")


@pytest.mark.parametrize("title", ["Ri2", "Ri(3)", "ri", "Ri的群"])
def test_title_must_match_the_complete_contact(title: str) -> None:
    """Partial names, differently cased names and group titles are rejected."""
    view = chat_view()
    view[1] = obs(title, 0.46, 0.02, 0.18)
    with pytest.raises(WeChatOCRError, match="单聊"):
        parse_chat_observations(view, contact="Ri")


@pytest.mark.parametrize("confidence", [0.29, float("nan")])
def test_low_confidence_never_becomes_an_incoming_question(confidence: float) -> None:
    """Uncertain recognition pauses instead of silently dropping critical text."""
    view = chat_view()
    view[4] = OCRObservation("15:07", 0.665, 0.14, 0.04, 0.02, confidence)
    with pytest.raises(WeChatOCRError):
        parse_chat_observations(view, contact=CONTACT)


def test_window_finder_binds_the_exact_wx2_bundle_and_path(monkeypatch) -> None:
    """A larger main-WeChat window never wins over the bound wx2 process."""

    def app(bundle: str, path: str, pid: int):
        return SimpleNamespace(
            bundleIdentifier=lambda: bundle,
            bundleURL=lambda: SimpleNamespace(path=lambda: path),
            processIdentifier=lambda: pid,
        )

    apps = [
        app("com.tencent.xinWeChat", "/Applications/WeChat.app", 1),
        app(wechat_ocr.WECHAT_BUNDLE_ID, wechat_ocr.WECHAT_APP_PATH, 2),
    ]
    windows = [
        {
            "kCGWindowOwnerPID": pid,
            "kCGWindowLayer": 0,
            "kCGWindowIsOnscreen": True,
            "kCGWindowNumber": pid * 10,
            "kCGWindowBounds": {"Width": width, "Height": 800, "X": 0, "Y": 0},
        }
        for pid, width in [(1, 2000), (2, 1000)]
    ]
    monkeypatch.setattr(
        wechat_ocr,
        "_load_appkit",
        lambda: SimpleNamespace(
            NSWorkspace=SimpleNamespace(
                sharedWorkspace=lambda: SimpleNamespace(
                    runningApplications=lambda: apps
                )
            )
        ),
    )
    monkeypatch.setattr(
        wechat_ocr,
        "_load_quartz",
        lambda: SimpleNamespace(
            kCGWindowListOptionAll=0,
            kCGNullWindowID=0,
            CGWindowListCopyWindowInfo=lambda *args: windows,
        ),
    )
    assert wechat_ocr.find_wechat_window().pid == 2
    windows[1]["kCGWindowSharingState"] = 0
    assert wechat_ocr.find_wechat_window().pid == 2
    del windows[1]["kCGWindowSharingState"]
    windows.append(dict(windows[1], kCGWindowNumber=21))
    with pytest.raises(WeChatOCRError):
        wechat_ocr.find_wechat_window()


def test_cli_probe_requires_config_and_verified_account(tmp_path, monkeypatch) -> None:
    """An installed binary alone never proves the wx2 account mapping."""
    monkeypatch.setattr(wechat_ocr.shutil, "which", lambda binary: "/test/wx")
    for verified in [False, True]:
        with pytest.raises(WeChatOCRError) as error:
            WxCLIChangeProbe(
                contact="Ri", workdir=str(tmp_path), account_verified=verified
            )
        assert error.value.code == "CLI_SETUP"
    write_cli_config(tmp_path, monkeypatch)
    WxCLIChangeProbe(contact="Ri", workdir=str(tmp_path), account_verified=True)


def write_cli_config(tmp_path, monkeypatch):
    """Create synthetic wx2 source paths without reading any real account data."""
    root = tmp_path / "wx2-data"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(wechat_ocr, "WECHAT_DATA_ROOT", root)
    (tmp_path / "keys.json").write_text("{}")
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "db_dir": str(root),
                "keys_file": "keys.json",
                "your_wxid": "fake-wx2",
            }
        )
    )


def test_main_wechat_config_is_never_accepted_for_wx2(tmp_path, monkeypatch) -> None:
    """A manually set verified flag cannot override a wrong source directory."""
    monkeypatch.setattr(wechat_ocr.shutil, "which", lambda binary: "/test/wx")
    write_cli_config(tmp_path, monkeypatch)
    config = json.loads((tmp_path / "config.json").read_text())
    config["db_dir"] = str(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(config))
    with pytest.raises(WeChatOCRError) as error:
        WxCLIChangeProbe(contact="Ri", workdir=str(tmp_path), account_verified=True)
    assert error.value.code == "CLI_SETUP"


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"status": "possibly_stale"},
        {"status": "windowed"},
        {"status": "ok", "unknown_shards": ["message_2.db"]},
    ],
)
def test_cli_probe_refuses_stale_or_unverified_metadata(meta) -> None:
    """No silent OCR fallback when source freshness cannot be proved."""
    with pytest.raises(WeChatOCRError) as error:
        WxCLIChangeProbe.payload_token({"messages": [], "meta": meta})
    assert error.value.code == "CLI_STALE"


def test_cli_token_excludes_only_metadata() -> None:
    """Metadata refreshes are no-ops; new history data changes the trigger."""
    old = {"messages": [{"content": "old"}], "meta": {"status": "ok", "checked": 1}}
    same = {"messages": [{"content": "old"}], "meta": {"status": "ok", "checked": 2}}
    new = {"messages": [{"content": "new"}], "meta": {"status": "ok"}}
    assert WxCLIChangeProbe.payload_token(old) == WxCLIChangeProbe.payload_token(same)
    assert WxCLIChangeProbe.payload_token(old) != WxCLIChangeProbe.payload_token(new)


@pytest.mark.asyncio
async def test_cli_probe_retries_read_once_without_consuming_incremental_cursor(
    tmp_path, monkeypatch
) -> None:
    """Only idempotent history reads retry, with explicit cwd and safe diagnostics."""
    write_cli_config(tmp_path, monkeypatch)
    monkeypatch.setattr(wechat_ocr.shutil, "which", lambda binary: "/test/wx")
    process = SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b"private-error", b""))
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(wechat_ocr.asyncio, "create_subprocess_exec", spawn)
    probe = WxCLIChangeProbe(contact="Ri", workdir=str(tmp_path), account_verified=True)
    with pytest.raises(WeChatOCRError) as error:
        await probe.read_token()
    assert error.value.code == "CLI_FAILED" and "private" not in str(error.value)
    assert spawn.await_count == 2
    assert all(call.args[1] == "history" for call in spawn.await_args_list)
    assert spawn.await_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_cli_change_probe_avoids_ocr_until_history_changes() -> None:
    """The CLI token commits only together with a valid OCR snapshot."""
    probe = SimpleNamespace(read_token=AsyncMock(side_effect=["old", "old", "new"]))
    captures = []
    reader = DesktopWeChatOCRReader(
        probe=probe,
        window_finder=lambda: 1,
        capture=lambda window: captures.append(window) or b"png",
        ocr=lambda png: chat_view(),
    )
    first = await reader.read_chat(contact=CONTACT)
    assert await reader.read_chat(contact=CONTACT) == first
    assert len(captures) == 2
    assert await reader.read_chat(contact=CONTACT) == first
    assert len(captures) == 4


@pytest.mark.asyncio
async def test_cli_query_success_and_config_change_guard(tmp_path, monkeypatch) -> None:
    """Only one successful read runs; changed account configuration blocks further reads."""
    write_cli_config(tmp_path, monkeypatch)
    monkeypatch.setattr(wechat_ocr.shutil, "which", lambda name: "/test/wx")
    payload = {"messages": [], "meta": {"status": "ok"}}
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(json.dumps(payload).encode(), b"")),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(wechat_ocr.asyncio, "create_subprocess_exec", spawn)
    probe = WxCLIChangeProbe(contact="Ri", workdir=str(tmp_path), account_verified=True)
    assert await probe.read_token() == WxCLIChangeProbe.payload_token(payload)
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(WeChatOCRError) as error:
        await probe.read_token()
    assert error.value.code == "CLI_SETUP"
    assert spawn.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_cli_read_reaps_only_its_owned_child(
    tmp_path, monkeypatch
) -> None:
    """Cancellation never consumes another read or leaves the CLI subprocess running."""
    write_cli_config(tmp_path, monkeypatch)
    monkeypatch.setattr(wechat_ocr.shutil, "which", lambda name: "/test/wx")

    async def hang():
        await asyncio.Event().wait()

    process = SimpleNamespace(communicate=hang)
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(wechat_ocr.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(wechat_ocr, "stop_cli_process", cleanup)
    probe = WxCLIChangeProbe(contact="Ri", workdir=str(tmp_path), account_verified=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(probe.read_token(), timeout=0.01)
    assert spawn.await_count == 1
    cleanup.assert_awaited_once_with(process, isolated_session=wechat_ocr.IS_POSIX)


@pytest.mark.asyncio
async def test_failed_ocr_does_not_commit_the_cli_change_token() -> None:
    """The same source event is still readable after the user restores the correct view."""
    wrong = chat_view()
    wrong[1] = obs("Other", 0.46, 0.02, 0.18)
    views = iter([wrong, chat_view(), chat_view()])
    reader = DesktopWeChatOCRReader(
        probe=SimpleNamespace(read_token=AsyncMock(return_value="same")),
        window_finder=lambda: 1,
        capture=lambda window: b"png",
        ocr=lambda png: next(views),
    )
    with pytest.raises(WeChatOCRError):
        await reader.read_chat(contact=CONTACT)
    assert len(await reader.read_chat(contact=CONTACT)) == 3


def test_green_background_keeps_a_long_own_bubble_out_of_peer_input() -> None:
    """A long own line may reach the peer column, so pixel ownership takes priority."""
    view = chat_view()
    view.append(OCRObservation("本人很长的回复", 0.48, 0.5, 0.43, 0.02, 0.5, "self"))
    result = parse_chat_observations(view, contact=CONTACT)
    assert result.messages[-1].is_self


@pytest.mark.parametrize(
    "rgb, expected", [((0.55, 0.95, 0.53), True), ((0.92, 0.92, 0.92), False)]
)
def test_bubble_color_sampler_uses_normalized_image_coordinates(rgb, expected) -> None:
    """The ownership sampler operates on bitmap data, without any desktop API."""
    color = SimpleNamespace(
        redComponent=lambda: rgb[0],
        greenComponent=lambda: rgb[1],
        blueComponent=lambda: rgb[2],
    )
    points = []

    def pixel(x, y):
        points.append((x, y))
        return color

    bitmap = SimpleNamespace(
        pixelsWide=lambda: 1600, pixelsHigh=lambda: 1300, colorAtX_y_=pixel
    )
    assert (
        wechat_ocr._green_bubble(bitmap, OCRObservation("text", 0.5, 0.2, 0.4, 0.02))
        is expected
    )
    assert len(points) == 15
    assert all(800 <= x < 1440 and 260 <= y < 286 for x, y in points)


@pytest.mark.asyncio
async def test_inconsistent_ocr_frames_never_produce_a_question() -> None:
    """A changing screen leaves the source event uncommitted for a later stable read."""
    second = chat_view()
    second[5] = obs("变化中的问题", 0.48, 0.18, 0.20)
    views = iter([chat_view(), second])
    reader = DesktopWeChatOCRReader(
        window_finder=lambda: 1,
        capture=lambda window: b"png",
        ocr=lambda png: next(views),
    )
    with pytest.raises(WeChatOCRError) as error:
        await reader.read_chat(contact=CONTACT)
    assert error.value.code == "AMBIGUOUS_LAYOUT"
