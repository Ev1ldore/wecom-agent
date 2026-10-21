"""Verify iLink credential storage and the QR login state machine without network."""

import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from adapters.wechat_ilink import ILinkQRCode, ILinkQRStatus
from adapters.wechat_ilink_auth import (
    FileILinkCredentialStore,
    ILinkCredentials,
    ILinkLoginFlow,
    WeChatILinkAuthError,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-qr-image-bytes"
PNG_BASE64 = base64.b64encode(PNG_BYTES).decode("ascii")


class FakeAPI:
    """Script the official QR endpoints for deterministic login tests."""

    def __init__(self) -> None:
        """Start in the waiting state with one valid QR record."""
        self.qr_statuses: list[ILinkQRStatus] = []
        self.status_calls = 0
        self.qrcode = ILinkQRCode("qr-1", PNG_BASE64)

    async def get_qrcode(self) -> ILinkQRCode:
        """Replay one fixed QR record."""
        return self.qrcode

    async def get_qrcode_status(self, qrcode: str) -> ILinkQRStatus:
        """Consume scripted statuses, then keep returning the final one."""
        self.status_calls += 1
        if self.qr_statuses:
            return self.qr_statuses.pop(0)
        return ILinkQRStatus("wait", None, None)


class MemoryStore:
    """Capture saves while optionally replaying stored credentials."""

    def __init__(self, stored: ILinkCredentials | None = None) -> None:
        """Start empty or with one scripted credential record."""
        self.stored = stored
        self.saved: list[ILinkCredentials] = []

    def load(self) -> ILinkCredentials | None:
        """Return the scripted credential record."""
        return self.stored

    def save(self, credentials: ILinkCredentials) -> None:
        """Record one save and expose it on subsequent loads."""
        self.saved.append(credentials)
        self.stored = credentials


def make_flow(
    api: FakeAPI,
    store: MemoryStore,
    statuses: list[ILinkQRStatus],
    *,
    qr_sink=None,
    monotonic: Callable[[], float] | None = None,
    sleep=None,
) -> ILinkLoginFlow:
    """Assemble one login flow with a scripted status sequence."""
    api.qr_statuses = statuses
    return ILinkLoginFlow(
        api,
        store,
        qr_sink=qr_sink,
        poll_interval_seconds=0.0,
        timeout_seconds=30.0,
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or (lambda _seconds: _drain()),
    )


async def _drain() -> None:
    """Yield once so polling loops can advance without real delays."""
    await asyncio.sleep(0)


def test_credentials_model_requires_nonempty_fields() -> None:
    """Reject blank tokens, origins and timestamps at the model boundary."""
    with pytest.raises(ValueError):
        ILinkCredentials(bot_token="", base_url="https://x", saved_at=1)
    with pytest.raises(ValueError):
        ILinkCredentials(bot_token="tok", base_url="", saved_at=1)
    with pytest.raises(ValueError):
        ILinkCredentials(bot_token="tok", base_url="https://x", saved_at=0)


def test_file_credential_store_roundtrips_with_private_permissions(
    tmp_path: Path,
) -> None:
    """Persist validated credentials atomically with owner-only permissions."""
    path = tmp_path / ".wechat_ilink.json"
    store = FileILinkCredentialStore(path)
    assert store.load() is None
    credentials = ILinkCredentials(
        bot_token="tok-1",
        base_url="https://ilinkai.weixin.qq.com",
        saved_at=1_780_000_000,
    )
    store.save(credentials)
    assert store.load() == credentials
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_file_credential_store_treats_damage_as_logged_out(tmp_path: Path) -> None:
    """Return None for missing, malformed or wrongly typed credential files."""
    store = FileILinkCredentialStore(tmp_path / "creds.json")
    assert store.load() is None
    (tmp_path / "creds.json").write_text("not json", encoding="utf-8")
    assert store.load() is None
    (tmp_path / "creds.json").write_text(
        json.dumps({"bot_token": "tok", "base_url": "https://x", "saved_at": "no"}),
        encoding="utf-8",
    )
    assert store.load() is None
    (tmp_path / "creds.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )
    assert store.load() is None


@pytest.mark.asyncio
async def test_ensure_credentials_returns_stored_without_network() -> None:
    """Reuse confirmed credentials instead of starting a new QR login."""
    stored = ILinkCredentials(
        bot_token="tok-1", base_url="https://example.test", saved_at=1_780_000_000
    )
    store = MemoryStore(stored)
    api = FakeAPI()
    flow = ILinkLoginFlow(api, store)
    assert await flow.ensure_credentials() == stored
    assert api.status_calls == 0
    assert store.saved == []


@pytest.mark.asyncio
async def test_login_confirmed_saves_credentials_and_delivers_qr() -> None:
    """Run QR sink once, then persist the confirmed token and base URL."""
    api = FakeAPI()
    store = MemoryStore()
    delivered: list[bytes] = []

    async def qr_sink(image: bytes) -> Path:
        delivered.append(image)
        return Path("qr.png")

    flow = make_flow(
        api,
        store,
        [ILinkQRStatus("wait", None, None), ILinkQRStatus("confirmed", "tok-9", None)],
        qr_sink=qr_sink,
    )
    credentials = await flow.ensure_credentials()
    assert delivered == [PNG_BYTES]
    assert credentials.bot_token == "tok-9"
    assert credentials.base_url == "https://ilinkai.weixin.qq.com"
    assert credentials.saved_at > 0
    assert store.saved == [credentials]


@pytest.mark.asyncio
async def test_login_uses_server_provided_base_url_when_present() -> None:
    """Prefer the official base URL returned with the confirmed status."""
    api = FakeAPI()
    store = MemoryStore()
    flow = make_flow(
        api,
        store,
        [ILinkQRStatus("confirmed", "tok-9", "https://ilinkai.weixin.qq.com")],
    )
    credentials = await flow.ensure_credentials()
    assert credentials.base_url == "https://ilinkai.weixin.qq.com"


@pytest.mark.asyncio
async def test_login_expired_raises_fixed_error() -> None:
    """Surface one clear instruction when the QR code expires."""
    api = FakeAPI()
    store = MemoryStore()
    flow = make_flow(api, store, [ILinkQRStatus("expired", None, None)])
    with pytest.raises(WeChatILinkAuthError) as excinfo:
        await flow.ensure_credentials()
    assert excinfo.value.code == "QR_EXPIRED"
    assert store.saved == []


@pytest.mark.asyncio
async def test_login_timeout_raises_fixed_error() -> None:
    """Stop polling after the deadline with one bounded instruction."""
    api = FakeAPI()
    store = MemoryStore()
    clock = iter([0.0, 10.0, 31.0])
    flow = make_flow(
        api,
        store,
        [ILinkQRStatus("wait", None, None)],
        monotonic=lambda: next(clock),
    )
    with pytest.raises(WeChatILinkAuthError) as excinfo:
        await flow.ensure_credentials()
    assert excinfo.value.code == "LOGIN_TIMEOUT"


@pytest.mark.asyncio
async def test_login_rejects_unknown_status_and_missing_token() -> None:
    """Refuse ambiguous status transitions instead of guessing."""
    api = FakeAPI()
    store = MemoryStore()
    flow = make_flow(api, store, [ILinkQRStatus("weird", None, None)])
    with pytest.raises(WeChatILinkAuthError) as excinfo:
        await flow.ensure_credentials()
    assert excinfo.value.code == "LOGIN_REJECTED"

    api2 = FakeAPI()
    store2 = MemoryStore()
    flow2 = make_flow(api2, store2, [ILinkQRStatus("confirmed", None, None)])
    with pytest.raises(WeChatILinkAuthError) as excinfo2:
        await flow2.ensure_credentials()
    assert excinfo2.value.code == "LOGIN_REJECTED"


@pytest.mark.asyncio
async def test_login_rejects_malformed_qr_image() -> None:
    """Reject non-PNG or oversized QR payloads before any polling."""
    api = FakeAPI()
    api.qrcode = ILinkQRCode("qr-1", base64.b64encode(b"not a png").decode())
    store = MemoryStore()
    flow = ILinkLoginFlow(api, store, sleep=lambda _s: _drain())
    with pytest.raises(WeChatILinkAuthError) as excinfo:
        await flow.ensure_credentials()
    assert excinfo.value.code == "QR_CODE_INVALID"
    assert api.status_calls == 0
