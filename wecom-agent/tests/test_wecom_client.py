"""Verify outbound WeCom requests using an isolated HTTP transport."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from structlog.contextvars import bound_contextvars

from src.gateway import wecom_client as client_module
from src.gateway.wecom_client import WeComAPIError, WecomClient, WecomClientProtocol

CORPID = "corp-test"
SECRET = "private-corp-secret"
TOKEN = "private-access-token"
TOKEN_PATH = "/cgi-bin/gettoken"
SEND_PATH = "/cgi-bin/message/send"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@asynccontextmanager
async def mock_client(handler: Handler) -> AsyncIterator[WecomClient]:
    """Provide real HTTP request construction without opening network sockets."""
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        WecomClient(CORPID, SECRET, 1000001, http_client=http) as client,
    ):
        yield client


def token_response(token: str = TOKEN, **extra: Any) -> httpx.Response:
    """Create a normal gettoken response with optional expiry overrides."""
    return httpx.Response(
        200, json={"access_token": token, "expires_in": 7200, **extra}
    )


@pytest.mark.asyncio
async def test_protocol_accepts_a_minimal_business_only_client() -> None:
    """A sender implementation must not need token or HTTP internals."""
    sent: list[tuple[str, str]] = []

    class MinimalSender:
        """A consumer-side fake implementing only the public business operation."""

        async def send_text(self, user_id: str, content: str) -> None:
            """Record the operation without external I/O."""
            sent.append((user_id, content))

    sender: WecomClientProtocol = MinimalSender()
    await sender.send_text("user-1", "回复")
    assert sent == [("user-1", "回复")]


@pytest.mark.asyncio
async def test_send_text_uses_expected_api_parameters_and_json() -> None:
    """Fetch a token once and construct the enterprise application message."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture both API requests and acknowledge the message."""
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    async with mock_client(handler) as client:
        assert await client.send_text("user-1", "你好 & 企业微信") is None

    assert len(requests) == 2
    token_request, message_request = requests
    assert token_request.method == "GET"
    assert token_request.url.scheme == "https"
    assert token_request.url.host == "qyapi.weixin.qq.com"
    assert token_request.url.path == TOKEN_PATH
    assert dict(token_request.url.params) == {"corpid": CORPID, "corpsecret": SECRET}
    assert message_request.method == "POST"
    assert message_request.url.host == "qyapi.weixin.qq.com"
    assert message_request.url.path == SEND_PATH
    assert dict(message_request.url.params) == {"access_token": TOKEN}
    assert json.loads(message_request.content) == {
        "touser": "user-1",
        "msgtype": "text",
        "agentid": 1000001,
        "text": {"content": "你好 & 企业微信"},
        "safe": 0,
    }
    assert all(
        request.extensions["timeout"][phase] == 10.0
        for request in requests
        for phase in ("connect", "read", "write", "pool")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_expiry", [True, False])
async def test_token_is_cached_and_refreshed_five_minutes_before_expiry(
    monkeypatch: pytest.MonkeyPatch, has_expiry: bool
) -> None:
    """Use the provider lifetime or its 7200-second default without sleeping."""
    now = [1000.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: now[0])
    token_count = 0
    sent_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Return distinct token versions so reuse is observable over HTTP."""
        nonlocal token_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            payload: dict[str, Any] = {"access_token": f"token-{token_count}"}
            if has_expiry:
                payload["expires_in"] = 7200
            return httpx.Response(200, json=payload)
        sent_tokens.append(request.url.params["access_token"])
        return httpx.Response(200, json={"errcode": 0})

    async with mock_client(handler) as client:
        await client.send_text("user-1", "first")
        now[0] = 7890.0
        await client.send_text("user-1", "cached")
        now[0] = 7910.0
        await client.send_text("user-1", "refresh")

    assert token_count == 2
    assert sent_tokens == ["token-1", "token-1", "token-2"]


@pytest.mark.asyncio
async def test_concurrent_initial_sends_fetch_one_token() -> None:
    """The first concurrent requests share the same token-fetch operation."""
    token_count = 0
    send_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """Yield during acquisition to exercise lock contention."""
        nonlocal token_count, send_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            await asyncio.sleep(0)
            return token_response()
        send_count += 1
        return httpx.Response(200, json={"errcode": 0})

    async with mock_client(handler) as client:
        await asyncio.gather(
            *(client.send_text(f"user-{i}", "hello") for i in range(8))
        )

    assert token_count == 1
    assert send_count == 8


@pytest.mark.asyncio
async def test_concurrent_near_expiry_sends_share_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recheck the cached deadline after acquiring the refresh lock."""
    now = [1000.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: now[0])
    token_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """Slow token acquisition enough for every contender to reach the lock."""
        nonlocal token_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            await asyncio.sleep(0)
            return token_response(f"token-{token_count}")
        return httpx.Response(200, json={"errcode": 0})

    async with mock_client(handler) as client:
        await client.send_text("user-1", "warm cache")
        now[0] = 7910.0
        await asyncio.gather(
            *(client.send_text(f"user-{i}", "hello") for i in range(8))
        )

    assert token_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("errcode", [42001, 40014])
async def test_invalid_token_is_refreshed_and_message_is_retried_once(
    errcode: int,
) -> None:
    """Provider token errors force refresh even before the local deadline."""
    token_count = 0
    sent_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Reject only the original token."""
        nonlocal token_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")
        token = request.url.params["access_token"]
        sent_tokens.append(token)
        return httpx.Response(
            200, json={"errcode": errcode if token == "token-1" else 0}
        )

    async with mock_client(handler) as client:
        await client.send_text("user-1", "hello")

    assert token_count == 2
    assert sent_tokens == ["token-1", "token-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_token_value", [False, True])
async def test_concurrent_token_rejections_share_one_refresh(
    same_token_value: bool,
) -> None:
    """Late rejections reuse a refreshed version even if its string is identical."""
    workers = 6
    token_count = 0
    send_count = 0
    reject_cached_token = False
    old_requests_arrived = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """Release the rejected requests together to expose refresh races."""
        nonlocal token_count, send_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            await asyncio.sleep(0)
            token = TOKEN if same_token_value else f"token-{token_count}"
            return token_response(token)
        send_count += 1
        if reject_cached_token and send_count <= workers + 1:
            if send_count == workers + 1:
                old_requests_arrived.set()
            await old_requests_arrived.wait()
            return httpx.Response(200, json={"errcode": 42001})
        return httpx.Response(200, json={"errcode": 0})

    async with mock_client(handler) as client:
        await client.send_text("user-1", "warm cache")
        reject_cached_token = True
        async with asyncio.timeout(2):
            await asyncio.gather(
                *(client.send_text(f"user-{i}", "hello") for i in range(workers))
            )

    assert token_count == 2
    assert send_count == 1 + workers * 2


@pytest.mark.asyncio
async def test_repeated_token_failure_has_finite_attempts() -> None:
    """A newly fetched token failing again must not cause an infinite loop."""
    token_count = 0
    send_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Always reject sends, including the single authorized resend."""
        nonlocal token_count, send_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")
        send_count += 1
        return httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid token"})

    async with mock_client(handler) as client:
        with pytest.raises(WeComAPIError) as caught:
            await client.send_text("user-1", "hello")

    assert caught.value.errcode == 40014
    assert caught.value.errmsg == "invalid token"
    assert token_count == send_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [TOKEN_PATH, SEND_PATH])
async def test_business_errors_are_exposed_without_transport_retry(
    endpoint: str,
) -> None:
    """Retrying invalid credentials or recipients would repeat a deterministic failure."""
    failed_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a provider business error at the selected API stage."""
        nonlocal failed_calls
        if request.url.path == endpoint:
            failed_calls += 1
            return httpx.Response(
                200, json={"errcode": 40003, "errmsg": "invalid user or credential"}
            )
        return token_response()

    async with mock_client(handler) as client:
        with pytest.raises(WeComAPIError) as caught:
            await client.send_text("user-1", "hello")

    assert caught.value.errcode == 40003
    assert caught.value.errmsg == "invalid user or credential"
    assert failed_calls == 1


def temporary_failure(request: httpx.Request, kind: str) -> httpx.Response:
    """Construct a transport or invalid-response failure without real network I/O."""
    if kind == "timeout":
        raise httpx.ReadTimeout("network timeout", request=request)
    if kind == "http":
        return httpx.Response(503, json={"errcode": 0})
    if kind == "json":
        return httpx.Response(200, content=b"not JSON")
    return httpx.Response(200, json=["not", "an", "object"])


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [TOKEN_PATH, SEND_PATH])
@pytest.mark.parametrize("kind", ["timeout", "http", "json", "non_object"])
async def test_temporary_api_failures_get_one_retry(endpoint: str, kind: str) -> None:
    """Both API stages recover once from network, status and malformed JSON errors."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail the selected endpoint once and then respond normally."""
        nonlocal attempts
        if request.url.path == endpoint:
            attempts += 1
            if attempts == 1:
                return temporary_failure(request, kind)
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(200, json={"errcode": 0})

    async with mock_client(handler) as client:
        await client.send_text("user-1", "hello")

    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [TOKEN_PATH, SEND_PATH])
@pytest.mark.parametrize("kind", ["timeout", "http", "json", "non_object"])
async def test_exhausted_api_retry_becomes_safe_domain_error(
    endpoint: str, kind: str
) -> None:
    """Stop after two attempts and hide credential-bearing request URLs."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        """Keep the selected endpoint unavailable."""
        nonlocal attempts
        if request.url.path == endpoint:
            attempts += 1
            return temporary_failure(request, kind)
        return token_response()

    async with mock_client(handler) as client:
        with pytest.raises(WeComAPIError) as caught:
            await client.send_text("user-1", "hello")

    assert attempts == 2
    assert caught.value.errcode == -1
    assert SECRET not in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert "https://" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"errcode": "0"}, {"errcode": False}])
async def test_send_response_requires_integer_errcode(payload: dict[str, Any]) -> None:
    """A malformed success response must not silently acknowledge delivery."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return a valid token followed by the malformed message result."""
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        with pytest.raises(WeComAPIError):
            await client.send_text("user-1", "hello")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"expires_in": 7200},
        {"access_token": "", "expires_in": 7200},
        {"access_token": TOKEN, "expires_in": 0},
        {"access_token": TOKEN, "expires_in": -1},
        {"access_token": TOKEN, "expires_in": "invalid"},
        {"access_token": TOKEN, "expires_in": None},
    ],
)
async def test_malformed_token_response_never_sends_a_message(
    payload: dict[str, Any],
) -> None:
    """Reject absent tokens and unusable lifetimes before the send endpoint."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any accidental send after an invalid token result."""
        paths.append(request.url.path)
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        with pytest.raises(WeComAPIError):
            await client.send_text("user-1", "hello")

    assert SEND_PATH not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "content"), [("", "hello"), ("user-1", ""), (" ", "hello")]
)
async def test_empty_business_input_does_not_open_network_requests(
    user_id: str, content: str
) -> None:
    """Reject empty recipients and content before requesting an access token."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record any unexpected I/O for invalid local arguments."""
        requests.append(request)
        return token_response()

    async with mock_client(handler) as client:
        with pytest.raises(ValueError):
            await client.send_text(user_id, content)

    assert requests == []


@pytest.mark.asyncio
async def test_request_wall_clock_budget_also_bounds_a_stalled_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected transport must not bypass the overall async request timeout."""
    attempts = 0
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT_SECONDS", 0.02)

    async def handler(request: httpx.Request) -> httpx.Response:
        """Simulate a socket operation that never completes on its own."""
        nonlocal attempts
        attempts += 1
        await asyncio.Event().wait()
        return token_response()

    async with mock_client(handler) as client:
        async with asyncio.timeout(1):
            with pytest.raises(WeComAPIError):
                await client.send_text("user-1", "hello")

    assert attempts == 2


@pytest.mark.asyncio
async def test_context_exit_does_not_close_injected_http_client() -> None:
    """The caller retains ownership of a supplied HTTP client."""
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: token_response())
    )
    try:
        async with WecomClient(CORPID, SECRET, 1000001, http_client=http):
            assert not http.is_closed
        assert not http.is_closed
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_context_exit_closes_owned_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An internally created client must release its connection pool."""
    owned = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: token_response())
    )
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **kwargs: owned)
    try:
        async with WecomClient(CORPID, SECRET, 1000001):
            assert not owned.is_closed
        assert owned.is_closed
    finally:
        await owned.aclose()


@pytest.mark.asyncio
async def test_client_logs_keep_message_context_and_hide_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Propagate the incoming message ID without logging tokens or raw HTTP errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Include credentials in a transport error to catch unsafe exception logging."""
        raise httpx.ConnectError(
            f"cannot connect: secret={SECRET}, token={TOKEN}, url={request.url}",
            request=request,
        )

    with (
        caplog.at_level(logging.INFO, logger="src.gateway.wecom_client"),
        bound_contextvars(msg_id="incoming-message-123"),
    ):
        async with mock_client(handler) as client:
            with pytest.raises(WeComAPIError):
                await client.send_text("user-1", "hello")

    records = [
        record for record in caplog.records if record.name == "src.gateway.wecom_client"
    ]
    assert records
    for record in records:
        rendered = record.getMessage()
        assert SECRET not in rendered
        assert TOKEN not in rendered
        assert "https://" not in rendered
        assert json.loads(rendered)["msg_id"] == "incoming-message-123"
