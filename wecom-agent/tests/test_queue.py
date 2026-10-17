"""Exercise Stream delivery and acknowledgements against in-memory Redis."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis

from src.core.models import WecomMessage
from src.core.queue import MessageQueue


@pytest_asyncio.fixture
async def queue_and_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[MessageQueue, FakeAsyncRedis]]:
    """Replace only the connection factory, retaining actual Stream commands."""
    client = FakeAsyncRedis(decode_responses=False)

    def fake_from_url(url: str, **kwargs: Any) -> FakeAsyncRedis:
        """Return the isolated in-memory client instead of opening a socket."""
        return client

    monkeypatch.setattr("src.core.queue.redis.from_url", fake_from_url)
    queue = MessageQueue("redis://localhost:6379/0", "test:wecom:messages")
    try:
        yield queue, client
    finally:
        await queue.aclose()


def make_message(msg_id: str) -> WecomMessage:
    """Build a valid normalized text message with non-ASCII content."""
    return WecomMessage(
        msg_id=msg_id,
        msg_type="text",
        from_user="user-1",
        chat_type="single",
        content="你好，企业微信",
        raw={"MsgId": msg_id, "Content": "你好，企业微信"},
        ts=1700000000,
    )


@pytest.mark.asyncio
async def test_publish_then_consume_preserves_message(
    queue_and_redis: tuple[MessageQueue, FakeAsyncRedis],
) -> None:
    """First-time group creation must include messages already published."""
    queue, client = queue_and_redis
    original = make_message("message-1")
    await queue.publish(original)
    entries = await client.xrange(queue.stream_key)
    assert entries[0][1][b"payload"] == original.model_dump_json().encode()

    async with aclosing(queue.consume("agents", "consumer-1", block_ms=10)) as stream:
        async with asyncio.timeout(2):
            received = await anext(stream)
        assert received.msg_id == original.msg_id
        assert received == original
        assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 1

    assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 0


@pytest.mark.asyncio
async def test_corrupt_payload_is_acknowledged_and_logged(
    queue_and_redis: tuple[MessageQueue, FakeAsyncRedis],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skip invalid JSON, invalid models, missing payloads and invalid UTF-8."""
    queue, client = queue_and_redis
    corrupt_fields = (
        {"payload": "not-json"},
        {"payload": "{}"},
        {"other": "missing-payload"},
        {"payload": b"\xff"},
    )
    corrupt_ids = [
        await client.xadd(queue.stream_key, fields) for fields in corrupt_fields
    ]
    await queue.publish(make_message("valid-after-corruption"))

    with caplog.at_level(logging.WARNING, logger="src.core.queue"):
        async with aclosing(
            queue.consume("agents", "consumer-1", block_ms=10)
        ) as stream:
            async with asyncio.timeout(2):
                received = await anext(stream)
            assert received.msg_id == "valid-after-corruption"
            pending = await client.xpending_range(
                queue.stream_key, "agents", "-", "+", 10
            )
            assert len(pending) == 1
            assert all(entry["message_id"] not in corrupt_ids for entry in pending)

    warnings = [record for record in caplog.records if record.name == "src.core.queue"]
    assert len(warnings) == len(corrupt_fields)
    assert all("无法解析" in record.getMessage() for record in warnings)
    assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 0


@pytest.mark.asyncio
async def test_ack_clears_pending_for_an_existing_group(
    queue_and_redis: tuple[MessageQueue, FakeAsyncRedis],
) -> None:
    """Handle BUSYGROUP and reduce pending from one to zero with explicit ACK."""
    queue, client = queue_and_redis
    await client.xgroup_create(queue.stream_key, "agents", id="0", mkstream=True)
    await queue.publish(make_message("message-to-ack"))

    async with aclosing(queue.consume("agents", "consumer-1", block_ms=10)) as stream:
        async with asyncio.timeout(2):
            received = await anext(stream)
        assert received.msg_id == "message-to-ack"
        assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 1
        pending = await client.xpending_range(queue.stream_key, "agents", "-", "+", 10)
        await queue.ack(pending[0]["message_id"].decode("ascii"))
        assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 0

    assert (await client.xpending(queue.stream_key, "agents"))["pending"] == 0
