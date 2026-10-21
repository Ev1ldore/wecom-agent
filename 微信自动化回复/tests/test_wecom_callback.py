"""Exercise callback responses, message normalization and Redis deduplication."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from src.config import Settings
from src.core.queue import MessageQueue
from src.gateway import wecom_callback as callback

QUERY = {"msg_signature": "test-signature", "timestamp": "1700000000", "nonce": "n1"}
ENCRYPTED_BODY = (
    b"<xml><ToUserName>corp-test</ToUserName><Encrypt>encrypted</Encrypt></xml>"
)
TEXT_XML = """<xml>
<ToUserName>corp-test</ToUserName>
<FromUserName>user-1</FromUserName>
<CreateTime>1700000000</CreateTime>
<MsgType>text</MsgType>
<Content><![CDATA[你好 & 企业微信]]></Content>
<MsgId>message-1</MsgId>
</xml>""".encode()


@dataclass
class CallbackHarness:
    """An isolated HTTP app and observable callback dependencies."""

    client: httpx.AsyncClient
    queue: AsyncMock
    redis: FakeAsyncRedis
    settings: Settings
    verify: Mock
    decrypt: Mock


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CallbackHarness]:
    """Keep routing real while replacing cryptography and external I/O."""
    settings = Settings(
        wecom_corpid="corp-test",
        wecom_secret="test-secret",
        wecom_token="test-token",
        wecom_aes_key="a" * 43,
        wecom_agent_id=1000001,
        queue_key="test:wecom:stream",
        _env_file=None,
    )
    verify = Mock(return_value=(0, "验证明文".encode()))
    decrypt = Mock(return_value=(0, TEXT_XML))
    monkeypatch.setattr(callback, "get_settings", lambda: settings)
    monkeypatch.setattr(callback.WXBizMsgCrypt, "VerifyURL", verify)
    monkeypatch.setattr(callback.WXBizMsgCrypt, "DecryptMsg", decrypt)

    queue = AsyncMock(spec=MessageQueue)
    redis = FakeAsyncRedis(decode_responses=False)
    app = FastAPI()
    app.state.queue = queue
    app.state.callback_redis = redis
    app.include_router(callback.router)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield CallbackHarness(client, queue, redis, settings, verify, decrypt)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_get_returns_exact_decrypted_plaintext(harness: CallbackHarness) -> None:
    """Verification must not add JSON quotes or a trailing newline."""
    response = await harness.client.get(
        "/wecom/callback", params={**QUERY, "echostr": "encrypted-echo"}
    )

    assert response.status_code == 200
    assert response.content == "验证明文".encode()
    assert response.headers["content-type"].startswith("text/plain")
    harness.verify.assert_called_once_with(
        "test-signature", "1700000000", "n1", "encrypted-echo"
    )
    harness.queue.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_publishes_normalized_text_before_acknowledging(
    harness: CallbackHarness,
) -> None:
    """Deliver one model to the queue and acknowledge with a truly empty body."""
    response = await harness.client.post(
        "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
    )

    assert response.status_code == 200
    assert response.content == b""
    harness.decrypt.assert_called_once_with(
        ENCRYPTED_BODY, "test-signature", "1700000000", "n1"
    )
    harness.queue.publish.assert_awaited_once()
    message = harness.queue.publish.await_args.args[0]
    assert message.msg_id == "message-1"
    assert message.msg_type == "text"
    assert message.from_user == "user-1"
    assert message.chat_type == "single"
    assert message.content == "你好 & 企业微信"
    assert message.ts == 1700000000
    assert message.raw["Content"] == "你好 & 企业微信"
    assert message.raw["ToUserName"] == "corp-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["voice", "image", "file", "event"])
async def test_non_text_is_published_without_content(
    harness: CallbackHarness, msg_type: str
) -> None:
    """Preserve original fields while leaving unsupported content to consumers."""
    msg_id_element = "" if msg_type == "event" else "<MsgId>non-text-1</MsgId>"
    harness.decrypt.return_value = (
        0,
        f"""<xml><FromUserName>user-2</FromUserName>
<CreateTime>1700000001</CreateTime><MsgType>{msg_type}</MsgType>
<Content>do not normalize non-text content</Content>{msg_id_element}
<Image><MediaId>media-1</MediaId></Image></xml>""".encode(),
    )

    response = await harness.client.post(
        "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
    )

    assert response.status_code == 200
    assert response.content == b""
    harness.queue.publish.assert_awaited_once()
    message = harness.queue.publish.await_args.args[0]
    assert message.msg_type == msg_type
    assert message.content is None
    assert message.msg_id == (
        "user-2-1700000001" if msg_type == "event" else "non-text-1"
    )
    assert message.raw["Image"] == {"MediaId": "media-1"}


@pytest.mark.asyncio
async def test_duplicate_msg_id_is_not_published_twice(
    harness: CallbackHarness,
) -> None:
    """Keep a five-minute atomic deduplication key after successful publication."""
    first = await harness.client.post(
        "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
    )
    second = await harness.client.post(
        "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
    )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b""
    harness.queue.publish.assert_awaited_once()
    dedup_key = f"{harness.settings.queue_key}:dedup:corp-test:message-1"
    assert await harness.redis.get(dedup_key) == b"1"
    assert 0 < await harness.redis.ttl(dedup_key) <= 300


@pytest.mark.asyncio
async def test_concurrent_retries_are_deduplicated(harness: CallbackHarness) -> None:
    """SET NX must prevent concurrent requests from both reaching the queue."""
    responses = await asyncio.gather(
        *(
            harness.client.post("/wecom/callback", params=QUERY, content=ENCRYPTED_BODY)
            for _ in range(4)
        )
    )

    assert all(response.status_code == 200 for response in responses)
    harness.queue.publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises_exception", [False, True])
async def test_decryption_failure_is_acknowledged_and_error_logged(
    harness: CallbackHarness,
    caplog: pytest.LogCaptureFixture,
    raises_exception: bool,
) -> None:
    """Both library error codes and raised exceptions must stop retry storms."""
    if raises_exception:
        harness.decrypt.side_effect = ValueError("invalid message signature")
    else:
        harness.decrypt.return_value = (-40001, b"")

    with caplog.at_level(logging.ERROR, logger="src.gateway.wecom_callback"):
        response = await harness.client.post(
            "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
        )

    assert response.status_code == 200
    assert response.content == b""
    harness.queue.publish.assert_not_awaited()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_invalid_plaintext_xml_is_acknowledged_and_error_logged(
    harness: CallbackHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed decrypted XML must never become a queued model or HTTP 500."""
    harness.decrypt.return_value = (0, b"<xml><FromUserName>unclosed")

    with caplog.at_level(logging.ERROR, logger="src.gateway.wecom_callback"):
        response = await harness.client.post(
            "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
        )

    assert response.status_code == 200
    assert response.content == b""
    harness.queue.publish.assert_not_awaited()
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_failed_url_verification_returns_empty_client_error(
    harness: CallbackHarness,
) -> None:
    """An unverified challenge must not be echoed as a valid handshake."""
    harness.verify.return_value = (-40001, b"")

    response = await harness.client.get(
        "/wecom/callback", params={**QUERY, "echostr": "invalid"}
    )

    assert response.status_code == 400
    assert response.content == b""
    harness.queue.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_url_verification_is_bounded(
    harness: CallbackHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blocked synchronous crypto call must not block the GET response budget."""

    def slow_verify(*args: str) -> tuple[int, bytes]:
        """Finish after the shortened request budget without accessing resources."""
        time.sleep(0.2)
        return 0, b"too-late"

    harness.verify.side_effect = slow_verify
    monkeypatch.setattr(callback, "GET_TIMEOUT_SECONDS", 0.02)
    with caplog.at_level(logging.ERROR, logger="src.gateway.wecom_callback"):
        async with asyncio.timeout(0.15):
            response = await harness.client.get(
                "/wecom/callback", params={**QUERY, "echostr": "encrypted-echo"}
            )

    assert response.status_code == 400
    assert response.content == b""
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    harness.queue.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_missing_query_parameters_does_not_return_500(
    harness: CallbackHarness,
) -> None:
    """Malformed callbacks are acknowledged without passing unverified data on."""
    response = await harness.client.post("/wecom/callback", content=ENCRYPTED_BODY)

    assert response.status_code == 200
    assert response.content == b""
    harness.queue.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_returns_empty_200_and_logs_error(
    harness: CallbackHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep the required HTTP contract when Redis Stream publication fails."""
    harness.queue.publish.side_effect = ConnectionError("Redis unavailable")

    with caplog.at_level(logging.ERROR, logger="src.gateway.wecom_callback"):
        response = await harness.client.post(
            "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
        )

    assert response.status_code == 200
    assert response.content == b""
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_slow_publication_is_bounded_and_error_logged(
    harness: CallbackHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Apply the callback time budget even while waiting for an unavailable queue."""

    async def blocked_publish(message: object) -> None:
        """Wait until the request budget cancels this simulated network operation."""
        await asyncio.Event().wait()

    harness.queue.publish.side_effect = blocked_publish
    monkeypatch.setattr(callback, "POST_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.ERROR, logger="src.gateway.wecom_callback"):
        async with asyncio.timeout(1):
            response = await harness.client.post(
                "/wecom/callback", params=QUERY, content=ENCRYPTED_BODY
            )

    assert response.status_code == 200
    assert response.content == b""
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
