"""Verify the official iLink inbox, sender and message mapping without network."""

import base64
import json
from typing import Any

import httpx
import pytest

from adapters.inbox import Msg
from adapters.wechat_ilink import (
    FileILinkCursorStore,
    ILinkHTTPClient,
    ILinkInbound,
    ILinkQRCode,
    ILinkQRStatus,
    ILinkUpdates,
    WeChatILinkError,
    WeChatILinkInbox,
    WeChatILinkSender,
    WeChatILinkSendError,
    extract_inbound,
)

PEER = "o9cq800kum_demo@im.wechat"
OTHER = "other_user@im.wechat"


def raw_message(
    *,
    from_user_id: str = PEER,
    text: str = "你好",
    create_time: int = 1_780_000_000,
    client_msg_id: str = "cmid-1",
    context_token: str = "ctx-1",
    message_type: int = 1,
    item_type: int = 1,
    group_id: str = "",
) -> dict[str, Any]:
    """Build one raw official message with explicit defaults and no surprises."""
    message: dict[str, Any] = {
        "from_user_id": from_user_id,
        "to_user_id": "bot@im.bot",
        "message_type": message_type,
        "message_state": 2,
        "context_token": context_token,
        "create_time": create_time,
        "client_msg_id": client_msg_id,
        "item_list": [{"type": item_type, "text_item": {"text": text}}],
    }
    if group_id:
        message["group_id"] = group_id
    return message


class FakeAPI:
    """Record calls and replay scripted responses for the iLink protocol."""

    def __init__(self) -> None:
        """Start with one scripted long-poll cycle and no sends."""
        self.update_cycles: list[ILinkUpdates] = [ILinkUpdates((), None)]
        self.sent: list[tuple[str, str, str]] = []
        self.qrcode = ILinkQRCode(
            "qr-1", base64.b64encode(b"\x89PNG fake png bytes").decode()
        )
        self.qr_status = ILinkQRStatus("wait", None, None)
        self.tokenless_rejected = False

    async def get_qrcode(self) -> ILinkQRCode:
        """Replay the scripted QR record."""
        return self.qrcode

    async def get_qrcode_status(self, qrcode: str) -> ILinkQRStatus:
        """Replay the scripted QR status record."""
        return self.qr_status

    async def get_updates(self, cursor: str) -> ILinkUpdates:
        """Consume scripted cycles, repeating the final cycle when exhausted."""
        if self.update_cycles:
            return self.update_cycles.pop(0)
        return ILinkUpdates((), None)

    async def send_text_message(
        self, to_user_id: str, content: str, context_token: str
    ) -> None:
        """Record one send without any network."""
        self.sent.append((to_user_id, content, context_token))


class MemoryCursorStore:
    """Hold the cursor in memory while keeping the file store contract."""

    def __init__(self, cursor: str = "") -> None:
        """Start from one explicit cursor value."""
        self.cursor = cursor
        self.saved: list[str] = []

    def load(self) -> str:
        """Return the in-memory cursor."""
        return self.cursor

    def save(self, cursor: str) -> None:
        """Record and hold the latest cursor."""
        self.saved.append(cursor)
        self.cursor = cursor


def make_inbox(
    api: FakeAPI, cursor_store: MemoryCursorStore, allowed_user: str = PEER
) -> WeChatILinkInbox:
    """Assemble one inbox with injected test doubles."""
    return WeChatILinkInbox(api, cursor_store, allowed_user=allowed_user)


def test_extract_inbound_maps_user_text_with_time_and_ids() -> None:
    """Keep sender, text, context, original time and client ID in one mapping."""
    inbound = extract_inbound(raw_message())
    assert inbound == ILinkInbound(
        from_user_id=PEER,
        text="你好",
        context_token="ctx-1",
        create_time=1_780_000_000,
        client_msg_id="cmid-1",
    )


def test_extract_inbound_skips_bot_group_image_and_empty_text() -> None:
    """Return None for out-of-scope messages instead of raising or inventing data."""
    assert extract_inbound(raw_message(message_type=2)) is None  # bot-originated
    assert extract_inbound(raw_message(group_id="g-1")) is None  # group chat
    assert extract_inbound(raw_message(item_type=2)) is None  # non-text item
    assert extract_inbound(raw_message(text="  ")) is None  # empty text


def test_extract_inbound_rejects_malformed_shapes() -> None:
    """Refuse ambiguous structures rather than guessing missing fields."""
    with pytest.raises(WeChatILinkError):
        extract_inbound("not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(WeChatILinkError):
        extract_inbound(raw_message(create_time="not-an-int"))
    with pytest.raises(WeChatILinkError):
        extract_inbound({**raw_message(), "from_user_id": ""})
    with pytest.raises(WeChatILinkError):
        extract_inbound({**raw_message(), "item_list": []})


@pytest.mark.asyncio
async def test_inbox_emits_authorized_peer_with_stable_dedupe_key() -> None:
    """Map only the allowed peer and persist the confirmed cursor."""
    api = FakeAPI()
    api.update_cycles = [
        ILinkUpdates(
            (
                raw_message(),
                raw_message(from_user_id=OTHER, text="hi", create_time=1_780_000_001),
                raw_message(text="no time", create_time=None),
                raw_message(message_type=2),
            ),
            "cursor-2",
        )
    ]
    cursors = MemoryCursorStore("cursor-1")
    inbox = make_inbox(api, cursors)
    messages = await inbox.poll_unread()
    assert cursors.saved == ["cursor-2"]
    assert len(messages) == 1
    message = messages[0]
    assert message.chat == f"wxilink:{PEER}"
    assert message.sender == PEER
    assert message.content == "你好"
    assert message.ts == 1_780_000_000
    assert message.time_precision == "second"
    assert message.dedupe_key == "wxilink:msg:cmid-1"
    assert message.chat_type == "single"
    assert message.is_self is False
    assert isinstance(message, Msg)
    assert inbox.context_for(PEER) == "ctx-1"
    assert inbox.context_for(OTHER) == "ctx-1"  # observed but not authorized


@pytest.mark.asyncio
async def test_inbox_without_original_time_skips_instead_of_fabricating() -> None:
    """Never stamp a local receipt time onto a message the server did not time."""
    api = FakeAPI()
    api.update_cycles = [ILinkUpdates((raw_message(create_time=None),), "cursor-x")]
    inbox = make_inbox(api, MemoryCursorStore())
    assert await inbox.poll_unread() == []


@pytest.mark.asyncio
async def test_inbox_keeps_cursor_when_response_advances_nothing() -> None:
    """Tolerate long-poll timeouts that return no cursor and no messages."""
    api = FakeAPI()
    api.update_cycles = [ILinkUpdates((), None)]
    cursors = MemoryCursorStore("cursor-keep")
    inbox = make_inbox(api, cursors)
    assert await inbox.poll_unread() == []
    assert cursors.saved == []


@pytest.mark.asyncio
async def test_sender_uses_latest_context_token() -> None:
    """Bind one reply to the freshest observed conversation token."""
    api = FakeAPI()
    api.update_cycles = [
        ILinkUpdates(
            (raw_message(context_token="ctx-a"), raw_message(context_token="ctx-b")),
            None,
        )
    ]
    inbox = make_inbox(api, MemoryCursorStore())
    await inbox.poll_unread()
    sender = WeChatILinkSender(api, inbox)
    await sender.send_text(PEER, "收到")
    assert api.sent == [(PEER, "收到", "ctx-b")]


@pytest.mark.asyncio
async def test_sender_rejects_missing_context_and_blank_content() -> None:
    """Fail fixed and safely when no conversation token has been observed."""
    api = FakeAPI()
    inbox = make_inbox(api, MemoryCursorStore())
    sender = WeChatILinkSender(api, inbox)
    with pytest.raises(WeChatILinkSendError) as missing:
        await sender.send_text(PEER, "收到")
    assert missing.value.code == "NO_CONTEXT_TOKEN"
    api.update_cycles = [ILinkUpdates((raw_message(),), None)]
    await inbox.poll_unread()
    with pytest.raises(WeChatILinkSendError):
        await sender.send_text(PEER, "  ")


def test_file_cursor_store_roundtrips_and_resets_on_corruption(
    tmp_path,
) -> None:
    """Persist the cursor atomically and treat any damage as an empty cursor."""
    store = FileILinkCursorStore(tmp_path / "state.json")
    assert store.load() == ""
    store.save("cursor-9")
    assert store.load() == "cursor-9"
    (tmp_path / "state.json").write_text("not json", encoding="utf-8")
    assert store.load() == ""
    (tmp_path / "state.json").write_text(json.dumps({"get_updates_buf": 3}))
    assert store.load() == ""


def test_file_cursor_store_rejects_non_string_cursor(tmp_path) -> None:
    """Refuse to persist a cursor that is not a plain string."""
    store = FileILinkCursorStore(tmp_path / "state.json")
    with pytest.raises(WeChatILinkError):
        store.save(123)  # type: ignore[arg-type]


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Replay one fixed JSON response for the HTTP client contract tests."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        """Bind one response payload and status code."""
        self.payload = payload
        self.status = status
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request and return the scripted response."""
        self.requests.append(request)
        return httpx.Response(
            self.status,
            json=self.payload,
            request=request,
        )


@pytest.mark.asyncio
async def test_http_client_builds_fixed_headers_without_token() -> None:
    """Send the official replay nonce headers and omit Bearer before login."""
    transport = _ScriptedTransport({"qrcode": "qr", "qrcode_img_content": "aGk="})
    async with (
        httpx.AsyncClient(transport=transport) as http,
        ILinkHTTPClient("https://ilinkai.weixin.qq.com", http_client=http) as client,
    ):
        qr = await client.get_qrcode()
    assert qr.qrcode == "qr"
    request = transport.requests[0]
    assert request.headers["AuthorizationType"] == "ilink_bot_token"
    assert request.headers["Content-Type"] == "application/json"
    assert "Authorization" not in request.headers
    uin = request.headers["X-WECHAT-UIN"]
    assert base64.b64decode(uin).decode("ascii").isdigit()
    assert str(request.url).startswith(
        "https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3"
    )


@pytest.mark.asyncio
async def test_http_client_rejects_message_calls_before_login() -> None:
    """Guard message endpoints on a pre-login client with a fixed error."""
    transport = _ScriptedTransport({"ret": 0})
    async with (
        httpx.AsyncClient(transport=transport) as http,
        ILinkHTTPClient("https://ilinkai.weixin.qq.com", http_client=http) as client,
    ):
        with pytest.raises(WeChatILinkError) as excinfo:
            await client.get_updates("")
        assert excinfo.value.code == "CREDENTIALS_INVALID"
        assert transport.requests == []


@pytest.mark.asyncio
async def test_http_client_validates_getupdates_payload_and_cursor() -> None:
    """Persist only confirmed cursors and reject malformed envelopes."""
    transport = _ScriptedTransport(
        {"ret": 0, "msgs": [raw_message()], "get_updates_buf": "next"}
    )
    async with (
        httpx.AsyncClient(transport=transport) as http,
        ILinkHTTPClient(
            "https://ilinkai.weixin.qq.com", "tok", http_client=http
        ) as client,
    ):
        updates = await client.get_updates("prev")
        assert updates.cursor == "next"
        assert len(updates.messages) == 1
        request = transport.requests[0]
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["get_updates_buf"] == "prev"
        assert body["base_info"]["channel_version"] == "1.0.2"

    bad_transport = _ScriptedTransport({"ret": 0, "msgs": "not-a-list"})
    async with (
        httpx.AsyncClient(transport=bad_transport) as http,
        ILinkHTTPClient(
            "https://ilinkai.weixin.qq.com", "tok", http_client=http
        ) as client,
    ):
        with pytest.raises(WeChatILinkError) as excinfo:
            await client.get_updates("")
        assert excinfo.value.code == "PAYLOAD_INVALID"


@pytest.mark.asyncio
async def test_http_client_send_message_envelope() -> None:
    """Emit the official send envelope with context token and text item."""
    transport = _ScriptedTransport({"ret": 0})
    async with (
        httpx.AsyncClient(transport=transport) as http,
        ILinkHTTPClient(
            "https://ilinkai.weixin.qq.com", "tok", http_client=http
        ) as client,
    ):
        await client.send_text_message(PEER, "收到", "ctx")
    request = transport.requests[0]
    body = json.loads(request.content)
    msg = body["msg"]
    assert msg["to_user_id"] == PEER
    assert msg["message_type"] == 2
    assert msg["message_state"] == 2
    assert msg["context_token"] == "ctx"
    assert msg["item_list"] == [{"type": 1, "text_item": {"text": "收到"}}]


@pytest.mark.asyncio
async def test_http_client_non_200_becomes_safe_error_without_retry_storm() -> None:
    """Convert HTTP failures into the allowlisted error after one retry."""
    transport = _ScriptedTransport({}, status=500)
    async with (
        httpx.AsyncClient(transport=transport) as http,
        ILinkHTTPClient(
            "https://ilinkai.weixin.qq.com", "tok", http_client=http
        ) as client,
    ):
        with pytest.raises(WeChatILinkError) as excinfo:
            await client.get_qrcode_status("qr")
        assert excinfo.value.code == "HTTP_FAILED"
    assert len(transport.requests) == 2  # one bounded retry


def test_http_client_rejects_credential_bearing_base_url() -> None:
    """Refuse origins with userinfo, query or fragment before any network."""
    with pytest.raises(WeChatILinkError) as excinfo:
        ILinkHTTPClient("https://user:pass@ilinkai.weixin.qq.com", "tok")
    assert excinfo.value.code == "CONFIG_INVALID"
    with pytest.raises(WeChatILinkError):
        ILinkHTTPClient("ftp://ilinkai.weixin.qq.com", "tok")
    with pytest.raises(WeChatILinkError):
        ILinkHTTPClient("https://ilinkai.weixin.qq.com?x=1", "tok")


@pytest.mark.asyncio
async def test_http_client_closes_only_owned_client() -> None:
    """Leave an injected client open for its owner after scope exit."""
    http = httpx.AsyncClient(transport=_ScriptedTransport({}))
    async with ILinkHTTPClient(
        "https://ilinkai.weixin.qq.com", "tok", http_client=http
    ):
        pass
    assert not http.is_closed
    await http.aclose()
