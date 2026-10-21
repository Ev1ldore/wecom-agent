"""Persist iLink bot credentials and run the official QR-code login flow."""

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from adapters.wechat_ilink import (
    DEFAULT_BASE_URL,
    ILinkAPIClient,
)

LOGIN_POLL_INTERVAL_SECONDS = 2.0
LOGIN_TIMEOUT_SECONDS = 180.0
MAX_QR_PNG_BYTES = 256 * 1024

_ERRORS = {
    "CREDENTIALS_INVALID": "iLink 登录凭据缺失或无效；请先运行 --wechat-login 扫码登录。",
    "QR_CODE_INVALID": "iLink 登录二维码数据无效；请重试 --wechat-login。",
    "QR_EXPIRED": "iLink 登录二维码已过期；请重新运行 --wechat-login。",
    "LOGIN_TIMEOUT": "等待扫码确认超时；请重新运行 --wechat-login 并在时限内完成扫码。",
    "LOGIN_REJECTED": "iLink 登录未被确认；请重新运行 --wechat-login。",
    "STORE_FAILED": "iLink 凭据保存失败；请检查本地目录权限后重试。",
}


class WeChatILinkAuthError(Exception):
    """Expose only an allowlisted machine code and a fixed safe explanation."""

    def __init__(self, code: str) -> None:
        """Discard transport details, QR payloads and tokens from error text."""
        self.code: str = code if code in _ERRORS else "LOGIN_REJECTED"
        super().__init__(_ERRORS[self.code])


class ILinkCredentials(BaseModel):
    """Describe one confirmed iLink bot login with strict local validation."""

    model_config = {"frozen": True}

    bot_token: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    saved_at: int = Field(gt=0)


class ILinkCredentialStore(Protocol):
    """Persist and load one confirmed iLink bot login locally."""

    def load(self) -> ILinkCredentials | None:
        """Return validated stored credentials or None when absent or invalid."""
        raise NotImplementedError

    def save(self, credentials: ILinkCredentials) -> None:
        """Persist credentials with owner-only file permissions."""
        raise NotImplementedError


class FileILinkCredentialStore:
    """Store iLink bot credentials in one local JSON file with 0600 perms."""

    def __init__(self, path: Path) -> None:
        """Remember the target path without creating the file yet."""
        self._path = path

    def load(self) -> ILinkCredentials | None:
        """Parse stored credentials strictly; any problem means 'not logged in'."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return ILinkCredentials(
                bot_token=raw["bot_token"],
                base_url=raw["base_url"],
                saved_at=raw["saved_at"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, credentials: ILinkCredentials) -> None:
        """Atomically rewrite the credential file with owner-only permissions."""
        if not isinstance(credentials, ILinkCredentials):
            raise WeChatILinkAuthError("STORE_FAILED")
        payload = json.dumps(credentials.model_dump(), ensure_ascii=False)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.chmod(0o600)
            tmp_path.replace(self._path)
        except OSError as exc:
            raise WeChatILinkAuthError("STORE_FAILED") from exc


class ILinkLoginFlow:
    """Run the official QR login and persist the confirmed bot credentials."""

    def __init__(
        self,
        api: ILinkAPIClient,
        store: ILinkCredentialStore,
        *,
        qr_sink: Callable[[bytes], Awaitable[Path]] | None = None,
        poll_interval_seconds: float = LOGIN_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Inject the API, store and pacing without starting any network call."""
        self._api = api
        self._store = store
        self._qr_sink = qr_sink
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    async def ensure_credentials(self) -> ILinkCredentials:
        """Return stored credentials or run one interactive QR login to obtain them."""
        stored = self._store.load()
        if stored is not None:
            return stored
        qr = await self._api.get_qrcode()
        image = self._decode_qr_png(qr.image_png_base64)
        if self._qr_sink is not None:
            await self._qr_sink(image)
        deadline = self._monotonic() + self._timeout
        while True:
            if self._monotonic() >= deadline:
                raise WeChatILinkAuthError("LOGIN_TIMEOUT")
            status = await self._api.get_qrcode_status(qr.qrcode)
            if status.status == "confirmed":
                if status.bot_token is None:
                    raise WeChatILinkAuthError("LOGIN_REJECTED")
                credentials = ILinkCredentials(
                    bot_token=status.bot_token,
                    base_url=status.base_url or DEFAULT_BASE_URL,
                    saved_at=int(time.time()),
                )
                self._store.save(credentials)
                return credentials
            if status.status == "expired":
                raise WeChatILinkAuthError("QR_EXPIRED")
            if status.status not in {"wait", "scaned"}:
                raise WeChatILinkAuthError("LOGIN_REJECTED")
            await self._sleep(self._poll_interval)

    @staticmethod
    def _decode_qr_png(image_base64: str) -> bytes:
        """Require canonical base64 PNG bytes before handing them to the caller."""
        try:
            data = base64.b64decode(image_base64.strip(), validate=True)
        except ValueError as exc:
            raise WeChatILinkAuthError("QR_CODE_INVALID") from exc
        if len(data) > MAX_QR_PNG_BYTES or not data.startswith(b"\x89PNG"):
            raise WeChatILinkAuthError("QR_CODE_INVALID")
        return data


__all__ = [
    "FileILinkCredentialStore",
    "ILinkCredentialStore",
    "ILinkCredentials",
    "ILinkLoginFlow",
    "WeChatILinkAuthError",
]
