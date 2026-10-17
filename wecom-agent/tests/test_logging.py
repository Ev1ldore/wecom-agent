"""Verify structured event fields and isolation of per-message log context."""

import asyncio
import json
import logging

import pytest
from structlog.contextvars import bound_contextvars, get_contextvars

from src.core.logging import EventLoggerProtocol, get_logger


def test_logger_emits_structured_fields_and_readable_chinese(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve named stdlib logging, explicit fields and readable event text."""
    logger: EventLoggerProtocol = get_logger("test.structured").bind(msg_id="message-1")
    with caplog.at_level(logging.INFO, logger="test.structured"):
        logger.info("消息已入队", stream="wecom:stream")
        logger.warning("消息需要关注", attempt=1)
        logger.error("消息发送失败", errcode=40001)

    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert [event["level"] for event in events] == ["info", "warning", "error"]
    assert all(event["msg_id"] == "message-1" for event in events)
    assert events[0] == {
        "event": "消息已入队",
        "msg_id": "message-1",
        "stream": "wecom:stream",
        "level": "info",
    }
    assert events[1]["attempt"] == 1
    assert events[2]["errcode"] == 40001
    assert "消息已入队" in caplog.records[0].getMessage()
    assert all(record.name == "test.structured" for record in caplog.records)


def test_context_is_restored_after_nested_message_and_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Restore prior message context after a nested scope exits with an error."""
    logger = get_logger("test.context")
    initial_context = get_contextvars()
    with caplog.at_level(logging.INFO, logger="test.context"):
        with bound_contextvars(msg_id="outer-message"):
            logger.info("outer-before")
            with (
                pytest.raises(RuntimeError, match="message failure"),
                bound_contextvars(msg_id="inner-message"),
            ):
                logger.info("inner")
                raise RuntimeError("message failure")
            logger.info("outer-after")
        logger.info("outside")

    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert [event.get("msg_id") for event in events[:3]] == [
        "outer-message",
        "inner-message",
        "outer-message",
    ]
    assert events[3].get("msg_id") == initial_context.get("msg_id")
    assert get_contextvars() == initial_context


@pytest.mark.asyncio
async def test_concurrent_message_contexts_do_not_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep each asynchronous task's msg_id across overlapping awaited work."""
    logger = get_logger("test.concurrent")
    initial_context = get_contextvars()
    both_started = asyncio.Event()
    started = 0

    async def log_message(msg_id: str) -> None:
        """Interleave two messages while retaining their individual context."""
        nonlocal started
        with bound_contextvars(msg_id=msg_id):
            logger.info("message-start", expected_msg_id=msg_id)
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            await asyncio.sleep(0)
            logger.info("message-end", expected_msg_id=msg_id)

    with caplog.at_level(logging.INFO, logger="test.concurrent"):
        await asyncio.gather(log_message("message-a"), log_message("message-b"))

    events = [json.loads(record.getMessage()) for record in caplog.records]
    assert len(events) == 4
    assert all(event["msg_id"] == event["expected_msg_id"] for event in events)
    assert get_contextvars() == initial_context
