"""Receive and send personal-WeChat text through Tencent's official iLink Bot API.

This backend talks to ``https://ilinkai.weixin.qq.com`` only. It never reads the
WeChat client UI or local databases, and it never invents message times: an
inbound message without an original positive-integer ``create_time`` is skipped
with a visible counter instead of being stamped with a receipt time.
"""

import asyncio
import base64
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import urlsplit

import httpx

from adapters.inbox import Inbox, InboxError, Msg
from src.core.logging import get_logger

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
CHANNEL_VERSION = "1.0.2"
REQUEST_TIMEOUT_SECONDS = 10.0
LONG_POLL_TIMEOUT_SECONDS = 40.0
USER_MESSAGE_TYPE = 1
BOT_MESSAGE_TYPE = 2
MESSAGE_STATE_FINISH = 2
TEXT_ITEM_TYPE = 1

logger = get_logger(__name__)

_ERRORS = {
    "CONFIG_INVALID": "iLink 接口地址配置无效；仅支持无凭据的 HTTP(S) 地址。",
    "HTTP_FAILED": "iLink 官方接口请求失败；未读取或发送任何消息。",
    "PAYLOAD_INVALID": "iLink 官方接口返回了无法确认的数据；本轮消息未处理。",
    "CREDENTIALS_INVALID": "iLink 登录凭据缺失或无效；请先运行 --wechat-login 扫码登录。",
    "MESSAGE_MALFORMED": "iLink 消息结构无法确认；该条消息已跳过。",
    "NO_CONTEXT_TOKEN": "没有可用于该联系人的会话令牌；本轮不发送，等待对方新消息后重试。",
    "SEND_REJECTED": "iLink 官方接口拒绝了发送请求；本轮不自动重发。",
}


class WeChatILinkError(InboxError):
    """Expose only an allowlisted machine code and a fixed safe explanation."""

    def __init__(self, code: str) -> None:
        """Discard transport details, tokens and message bodies from errors."""
        self.code: str = code if code in _ERRORS else "HTTP_FAILED"
        super().__init__(_ERRORS[self.code])


class WeChatILinkSendError(WeChatILinkError):
    """Mark a send failure as certainly not delivered unless stated otherwise."""


@dataclass(frozen=True)
class ILinkQRCode:
    """Describe one login QR record returned by the official iLink API."""

    qrcode: str
    image_png_base64: str


@dataclass(frozen=True)
class ILinkQRStatus:
    """Describe one polling result of the official QR login status endpoint."""

    status: str
    bot_token: str | None
    base_url: str | None


@dataclass(frozen=True)
class ILinkUpdates:
    """Describe one long-poll response with its continuation cursor."""

    messages: tuple[dict[str, Any], ...]
    cursor: str | None


@dataclass(frozen=True)
class ILinkInbound:
    """Describe one verified user text message from the official long poll."""

    from_user_id: str
    text: str
    context_token: str | None
    create_time: int | None
    client_msg_id: str | None


class ILinkAPIClient(Protocol):
    """Define the bounded official iLink operations used by this backend."""

    async def get_qrcode(self) -> ILinkQRCode:
        """Fetch one bot login QR record or raise a safe error."""
        raise NotImplementedError

    async def get_qrcode_status(self, qrcode: str) -> ILinkQRStatus:
        """Poll one QR login status or raise a safe error."""
        raise NotImplementedError

    async def get_updates(self, cursor: str) -> ILinkUpdates:
        """Run one bounded long-poll cycle and return new messages plus cursor."""
        raise NotImplementedError

    async def send_text_message(
        self, to_user_id: str, content: str, context_token: str
    ) -> None:
        """Send one text reply bound to the inbound conversation token."""
        raise NotImplementedError


def _validate_base_url(base_url: str) -> str:
    """Require one plain HTTP(S) origin without userinfo, query or fragment."""
    failed = False
    try:
        parsed = urlsplit(base_url)
        url = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not url.host
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
        ):
            failed = True
    except (ValueError, httpx.InvalidURL):
        failed = True
    if failed:
        raise WeChatILinkError("CONFIG_INVALID")
    return base_url.rstrip("/")


def _wechat_uin() -> str:
    """Build one replay-protection header value: base64 of a random uint32."""
    return base64.b64encode(str(secrets.randbelow(2**32)).encode("ascii")).decode(
        "ascii"
    )


class ILinkHTTPClient:
    """Call only the fixed official iLink endpoints with bounded retries."""

    def __init__(
        self,
        base_url: str,
        bot_token: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        channel_version: str = CHANNEL_VERSION,
    ) -> None:
        """Validate configuration without opening a client when one is injected.

        A ``None`` token builds a pre-login client: QR endpoints stay reachable
        while message endpoints reject calls until credentials exist.
        """
        self._base_url = _validate_base_url(base_url)
        if bot_token is not None and (
            not isinstance(bot_token, str) or not bot_token.strip()
        ):
            raise WeChatILinkError("CREDENTIALS_INVALID")
        self._bot_token = bot_token.strip() if bot_token is not None else None
        self._channel_version = channel_version
        self._owns_http_client = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        """Enter a reusable client scope and reject an already closed client."""
        if self._closed or self._http.is_closed:
            raise WeChatILinkError("HTTP_FAILED")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close only an internally owned HTTP client, including error exits."""
        self._closed = True
        if self._owns_http_client:
            await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        """Build the fixed official header set with a fresh replay nonce."""
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _wechat_uin(),
        }
        if self._bot_token is not None:
            headers["Authorization"] = f"Bearer {self._bot_token}"
        return headers

    def _require_token(self) -> None:
        """Reject message operations on a pre-login client."""
        if self._bot_token is None:
            raise WeChatILinkError("CREDENTIALS_INVALID")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        """Run one HTTP call with exactly one retry on transport-level failure."""
        failure: Exception | None = None
        for attempt in range(2):
            try:
                async with asyncio.timeout(timeout + 5):
                    response = await self._http.request(
                        method,
                        f"{self._base_url}{path}",
                        params=params,
                        json=json_body,
                        headers=self._headers(),
                        timeout=timeout,
                        follow_redirects=False,
                    )
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        "Unexpected iLink HTTP status",
                        request=response.request,
                        response=response,
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise WeChatILinkError("PAYLOAD_INVALID") from exc
                if not isinstance(payload, dict):
                    raise WeChatILinkError("PAYLOAD_INVALID")
                return payload
            except WeChatILinkError:
                raise
            except Exception as exc:  # noqa: BLE001 -- Transport details stay private.
                failure = exc
                logger.warning(
                    "wechat_ilink_request_failed",
                    msg_id=None,
                    attempt=attempt + 1,
                    retry=attempt == 0,
                    error=type(exc).__name__,
                )
        raise WeChatILinkError("HTTP_FAILED") from failure

    async def get_qrcode(self) -> ILinkQRCode:
        """Fetch one login QR record with strict field validation."""
        payload = await self._request(
            "GET",
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": BOT_TYPE},
        )
        qrcode = payload.get("qrcode")
        image = payload.get("qrcode_img_content")
        if not isinstance(qrcode, str) or not qrcode.strip():
            raise WeChatILinkError("PAYLOAD_INVALID")
        if not isinstance(image, str) or not image.strip():
            raise WeChatILinkError("PAYLOAD_INVALID")
        try:
            base64.b64decode(image, validate=True)
        except ValueError as exc:
            raise WeChatILinkError("PAYLOAD_INVALID") from exc
        return ILinkQRCode(qrcode.strip(), image.strip())

    async def get_qrcode_status(self, qrcode: str) -> ILinkQRStatus:
        """Poll one QR status with strict field validation."""
        if not isinstance(qrcode, str) or not qrcode.strip():
            raise WeChatILinkError("PAYLOAD_INVALID")
        payload = await self._request(
            "GET",
            "/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode.strip()},
        )
        status = payload.get("status")
        if not isinstance(status, str) or not status.strip():
            raise WeChatILinkError("PAYLOAD_INVALID")
        bot_token = payload.get("bot_token")
        base_url = payload.get("baseurl")
        return ILinkQRStatus(
            status.strip(),
            bot_token.strip()
            if isinstance(bot_token, str) and bot_token.strip()
            else None,
            base_url.strip()
            if isinstance(base_url, str) and base_url.strip()
            else None,
        )

    async def get_updates(self, cursor: str) -> ILinkUpdates:
        """Run one official long-poll cycle without forwarding private bodies."""
        self._require_token()
        if not isinstance(cursor, str):
            raise WeChatILinkError("PAYLOAD_INVALID")
        payload = await self._request(
            "POST",
            "/ilink/bot/getupdates",
            json_body={
                "get_updates_buf": cursor,
                "base_info": {"channel_version": self._channel_version},
            },
            timeout=LONG_POLL_TIMEOUT_SECONDS,
        )
        if payload.get("ret") != 0:
            raise WeChatILinkError("PAYLOAD_INVALID")
        raw_messages = payload.get("msgs")
        messages: list[dict[str, Any]] = []
        if raw_messages is not None:
            if not isinstance(raw_messages, list) or not all(
                isinstance(item, dict) for item in raw_messages
            ):
                raise WeChatILinkError("PAYLOAD_INVALID")
            messages = list(raw_messages)
        next_cursor = payload.get("get_updates_buf")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise WeChatILinkError("PAYLOAD_INVALID")
        return ILinkUpdates(tuple(messages), next_cursor)

    async def send_text_message(
        self, to_user_id: str, content: str, context_token: str
    ) -> None:
        """Send one text reply; the server associates it via the context token."""
        for field, value in (
            ("to_user_id", to_user_id),
            ("content", content),
            ("context_token", context_token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise WeChatILinkSendError("SEND_REJECTED")
        self._require_token()
        payload = await self._request(
            "POST",
            "/ilink/bot/sendmessage",
            json_body={
                "msg": {
                    "to_user_id": to_user_id.strip(),
                    "message_type": BOT_MESSAGE_TYPE,
                    "message_state": MESSAGE_STATE_FINISH,
                    "context_token": context_token.strip(),
                    "item_list": [
                        {"type": TEXT_ITEM_TYPE, "text_item": {"text": content}}
                    ],
                }
            },
        )
        if payload.get("ret") not in (0, None):
            raise WeChatILinkSendError("SEND_REJECTED")


def extract_inbound(raw: dict[str, Any]) -> ILinkInbound | None:
    """Map one raw official message to a verified inbound text or return None.

    Returning None means the message is deliberately out of scope (bot-originated,
    grouped, non-text or missing the original server timestamp), never that it
    was silently dropped: the inbox counts every None with an explicit reason.
    """
    if not isinstance(raw, dict):
        raise WeChatILinkError("MESSAGE_MALFORMED")
    if raw.get("message_type") != USER_MESSAGE_TYPE:
        return None
    if raw.get("group_id"):
        return None
    from_user_id = raw.get("from_user_id")
    if not isinstance(from_user_id, str) or not from_user_id.strip():
        raise WeChatILinkError("MESSAGE_MALFORMED")
    create_time = raw.get("create_time")
    if create_time is not None and type(create_time) is not int:
        raise WeChatILinkError("MESSAGE_MALFORMED")
    client_msg_id = raw.get("client_msg_id")
    if client_msg_id is not None and not isinstance(client_msg_id, str):
        raise WeChatILinkError("MESSAGE_MALFORMED")
    item_list = raw.get("item_list")
    if (
        not isinstance(item_list, list)
        or len(item_list) != 1
        or not isinstance(item_list[0], dict)
    ):
        raise WeChatILinkError("MESSAGE_MALFORMED")
    item = item_list[0]
    if item.get("type") != TEXT_ITEM_TYPE:
        return None
    text_item = item.get("text_item")
    if not isinstance(text_item, dict):
        raise WeChatILinkError("MESSAGE_MALFORMED")
    text = text_item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    context_token = raw.get("context_token")
    return ILinkInbound(
        from_user_id=from_user_id.strip(),
        text=text,
        context_token=(
            context_token.strip()
            if isinstance(context_token, str) and context_token.strip()
            else None
        ),
        create_time=create_time if create_time is not None else None,
        client_msg_id=(
            client_msg_id.strip()
            if isinstance(client_msg_id, str) and client_msg_id.strip()
            else None
        ),
    )


class ILinkCursorStore(Protocol):
    """Persist the official long-poll cursor across process restarts."""

    def load(self) -> str:
        """Return the last confirmed cursor or an empty string."""
        raise NotImplementedError

    def save(self, cursor: str) -> None:
        """Persist one confirmed cursor for the next process start."""
        raise NotImplementedError


class FileILinkCursorStore:
    """Store the cursor in one local JSON file with private permissions."""

    def __init__(self, path: Path) -> None:
        """Remember the target path without creating the file yet."""
        self._path = path

    def load(self) -> str:
        """Read one bounded cursor string; any problem resets to empty."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(raw, dict):
            return ""
        cursor = raw.get("get_updates_buf")
        return cursor if isinstance(cursor, str) else ""

    def save(self, cursor: str) -> None:
        """Atomically rewrite the cursor file with owner-only permissions."""
        if not isinstance(cursor, str):
            raise WeChatILinkError("PAYLOAD_INVALID")
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        tmp_path.write_text(
            json.dumps({"get_updates_buf": cursor}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.chmod(0o600)
        tmp_path.replace(self._path)


class WeChatILinkInbox(Inbox):
    """Poll one official bot long-poll stream and map authorized direct texts.

    Only messages from the explicitly configured peer are emitted; every other
    message is counted with a visible log line. Messages without an original
    server timestamp are never stamped locally and are reported as skipped.
    """

    def __init__(
        self,
        api: ILinkAPIClient,
        cursor_store: ILinkCursorStore,
        *,
        allowed_user: str,
    ) -> None:
        """Require one explicit peer identity; empty peers are rejected upstream."""
        if not isinstance(allowed_user, str) or not allowed_user.strip():
            raise ValueError("allowed_user must be the verified @im.wechat peer ID")
        self._api = api
        self._cursor_store = cursor_store
        self._allowed_user = allowed_user.strip()
        self._contexts: dict[str, str] = {}

    def context_for(self, user_id: str) -> str | None:
        """Return the latest observed conversation token for one peer, if any."""
        return self._contexts.get(user_id)

    async def poll_unread(self) -> list[Msg]:
        """Run one long-poll cycle and emit authorized, time-verified messages."""
        updates = await self._api.get_updates(self._cursor_store.load())
        if updates.cursor is not None:
            self._cursor_store.save(updates.cursor)
        messages: list[Msg] = []
        skipped_other = 0
        skipped_group = 0
        skipped_time = 0
        for raw in updates.messages:
            inbound = extract_inbound(raw)
            if inbound is None:
                skipped_other += 1
                continue
            if inbound.context_token is not None:
                self._contexts[inbound.from_user_id] = inbound.context_token
            if inbound.from_user_id != self._allowed_user:
                skipped_other += 1
                logger.info(
                    "wechat_ilink_unlisted_sender",
                    msg_id=None,
                    sender=inbound.from_user_id,
                    accepted=False,
                )
                continue
            if inbound.create_time is None or inbound.create_time <= 0:
                skipped_time += 1
                logger.warning(
                    "wechat_ilink_message_without_original_time",
                    msg_id=None,
                    sender=inbound.from_user_id,
                    accepted=False,
                )
                continue
            messages.append(
                Msg(
                    chat=f"wxilink:{inbound.from_user_id}",
                    sender=inbound.from_user_id,
                    content=inbound.text,
                    ts=inbound.create_time,
                    dedupe_key=(
                        f"wxilink:msg:{inbound.client_msg_id}"
                        if inbound.client_msg_id is not None
                        else None
                    ),
                )
            )
        if skipped_other or skipped_group or skipped_time:
            logger.info(
                "wechat_ilink_poll_skipped",
                msg_id=None,
                other=skipped_other,
                group=skipped_group,
                no_original_time=skipped_time,
                accepted=len(messages),
            )
        return messages


class WeChatILinkSender:
    """Send one text reply through the official iLink bot send endpoint."""

    def __init__(self, api: ILinkAPIClient, inbox: WeChatILinkInbox) -> None:
        """Bind the sender to one inbox so replies reuse observed context tokens."""
        self._api = api
        self._inbox = inbox

    async def send_text(self, user_id: str, content: str) -> None:
        """Require a nonempty draft and one observed conversation token."""
        if not isinstance(user_id, str) or not user_id.strip():
            raise WeChatILinkSendError("SEND_REJECTED")
        if not isinstance(content, str) or not content.strip():
            raise WeChatILinkSendError("SEND_REJECTED")
        context_token = self._inbox.context_for(user_id.strip())
        if context_token is None:
            raise WeChatILinkSendError("NO_CONTEXT_TOKEN")
        await self._api.send_text_message(user_id.strip(), content, context_token)
