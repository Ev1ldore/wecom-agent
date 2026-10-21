"""Send WeCom text messages with cached tokens and bounded HTTP retries."""

import asyncio
from dataclasses import dataclass
from time import monotonic
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import uuid4

import httpx
from structlog.contextvars import get_contextvars

from src.core.logging import EventLoggerProtocol, get_logger

API_BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"
REQUEST_TIMEOUT_SECONDS = 10.0
TOKEN_REFRESH_MARGIN_SECONDS = 300
TOKEN_ERROR_CODES = frozenset({42001, 40014})

logger = get_logger(__name__)


class WecomClientProtocol(Protocol):
    """Define the only public business operation used by message consumers."""

    async def send_text(self, user_id: str, content: str) -> None:
        """Send text to a WeCom user or raise a client/API error."""
        raise NotImplementedError


class WeComAPIError(Exception):
    """Report a WeCom business error or an exhausted HTTP request failure."""

    def __init__(self, errcode: int, errmsg: str) -> None:
        """Preserve the API error fields for callers without retaining URLs."""
        self.errcode: int = errcode
        self.errmsg: str = errmsg
        super().__init__(f"WeCom API error {errcode}: {errmsg}")


@dataclass(frozen=True)
class _AccessToken:
    """Keep a token deadline and use object identity as its refresh version."""

    value: str
    expires_at: float


class WecomClient:
    """Reuse one client per credential set to share its token cache and lock.

    Use as an async context manager. An injected HTTP client stays owned by the
    caller; an internally created client is closed when the context exits.
    """

    def __init__(
        self,
        corpid: str,
        secret: str,
        agent_id: int,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a lazy HTTP client and an instance-local in-memory token cache."""
        if not corpid or not secret or agent_id <= 0:
            raise ValueError("corpid, secret and a positive agent_id are required")
        self._corpid: str = corpid
        self._secret: str = secret
        self._agent_id: int = agent_id
        self._owns_http_client: bool = http_client is None
        self._http: httpx.AsyncClient = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        )
        self._token: _AccessToken | None = None
        self._token_lock = asyncio.Lock()
        self._closed: bool = False

    async def __aenter__(self) -> Self:
        """Enter the resource scope while retaining the shared token cache."""
        if self._closed:
            raise WeComAPIError(-1, "WeCom client is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close only the HTTP client owned by this wrapper."""
        self._closed = True
        if self._owns_http_client:
            await self._http.aclose()

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str],
        log: EventLoggerProtocol,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Try each HTTP operation twice with a ten-second budget per attempt."""
        for attempt in range(2):
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    response = await self._http.request(
                        method,
                        f"{API_BASE_URL}/{endpoint}",
                        params=params,
                        json=payload,
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise TypeError("WeCom response must be a JSON object")
                    return data
            except (httpx.HTTPError, TimeoutError, TypeError, ValueError) as exc:
                log.warning(
                    "wecom_http_request_failed",
                    endpoint=endpoint,
                    attempt=attempt + 1,
                    retry=attempt == 0,
                    error=type(exc).__name__,
                )
                if attempt == 1:
                    # HTTPX exception strings can include credential-bearing URLs.
                    raise WeComAPIError(
                        -1,
                        f"WeCom HTTP request failed after 2 attempts ({type(exc).__name__})",
                    ) from None
        raise AssertionError("HTTP retry loop exhausted without a result")

    @staticmethod
    def _error_fields(
        data: dict[str, Any], *, require_errcode: bool = False
    ) -> tuple[int, str]:
        """Validate business error fields without logging the response body."""
        if require_errcode and "errcode" not in data:
            raise WeComAPIError(-1, "WeCom response is missing errcode")
        errcode = data.get("errcode", 0)
        errmsg = data.get("errmsg", "")
        if type(errcode) is not int or not isinstance(errmsg, str):
            raise WeComAPIError(-1, "WeCom response has invalid error fields")
        return errcode, errmsg

    @staticmethod
    def _token_usable(token: _AccessToken | None) -> bool:
        """Refresh tokens at least five minutes before their local deadline."""
        return token is not None and monotonic() < (
            token.expires_at - TOKEN_REFRESH_MARGIN_SECONDS
        )

    async def _get_access_token(
        self,
        log: EventLoggerProtocol,
        *,
        rejected_token: _AccessToken | None = None,
    ) -> _AccessToken:
        """Single-flight initial, scheduled and API-triggered token refreshes."""
        token = self._token
        if rejected_token is None and self._token_usable(token):
            assert token is not None
            return token

        async with self._token_lock:
            token = self._token
            if self._token_usable(token) and (
                rejected_token is None or token is not rejected_token
            ):
                assert token is not None
                return token

            self._token = None
            data = await self._request_json(
                "GET",
                "gettoken",
                params={"corpid": self._corpid, "corpsecret": self._secret},
                log=log,
            )
            errcode, errmsg = self._error_fields(data)
            if errcode != 0:
                raise WeComAPIError(errcode, errmsg)
            access_token = data.get("access_token")
            expires_in = data.get("expires_in", 7200)
            if not isinstance(access_token, str) or not access_token:
                raise WeComAPIError(-1, "WeCom response is missing access_token")
            if type(expires_in) is not int or expires_in <= 0:
                raise WeComAPIError(-1, "WeCom response has invalid expires_in")
            token = _AccessToken(access_token, monotonic() + expires_in)
            self._token = token
            log.info("wecom_access_token_refreshed")
            return token

    async def send_text(self, user_id: str, content: str) -> None:
        """Send text with one token-recovery retry and bounded HTTP retries.

        Bind structlog's msg_id context in the consumer to correlate this send
        with an inbound message. Direct calls receive an outbound correlation ID.
        """
        if not user_id.strip() or not content:
            raise ValueError("user_id and content must not be empty")
        if self._closed or self._http.is_closed:
            raise WeComAPIError(-1, "WeCom client is closed")
        msg_id = get_contextvars().get("msg_id") or f"outbound-{uuid4().hex}"
        log = logger.bind(msg_id=msg_id)
        payload = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": content},
            "safe": 0,
        }
        rejected_token: _AccessToken | None = None
        try:
            for token_attempt in range(2):
                token = await self._get_access_token(log, rejected_token=rejected_token)
                data = await self._request_json(
                    "POST",
                    "message/send",
                    params={"access_token": token.value},
                    payload=payload,
                    log=log,
                )
                errcode, errmsg = self._error_fields(data, require_errcode=True)
                if errcode == 0:
                    log.info("wecom_text_sent")
                    return
                if errcode in TOKEN_ERROR_CODES:
                    if token_attempt == 0:
                        log.warning("wecom_access_token_rejected", errcode=errcode)
                        rejected_token = token
                        continue
                    async with self._token_lock:
                        if self._token is token:
                            self._token = None
                raise WeComAPIError(errcode, errmsg)
        except WeComAPIError as exc:
            log.error("wecom_text_send_failed", errcode=exc.errcode)
            raise
