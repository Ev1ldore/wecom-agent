"""Minimal asynchronous Redis Stream publisher and consumer."""

import asyncio
from collections.abc import AsyncGenerator

from pydantic import ValidationError
from redis import asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from src.core.logging import get_logger
from src.core.models import WecomMessage

logger = get_logger(__name__)

_CONNECTION_ERROR = "Redis 连接失败，请检查 redis_url 配置及 Redis 是否启动。"


class MessageQueue:
    """Use one stream and one consumer group per queue instance.

    The first iteration of consume binds the group used by ack. Multiple
    consumers may share that group, but switching groups requires a new queue.
    """

    def __init__(self, redis_url: str, stream_key: str) -> None:
        """Create a lazy asyncio client; connect on the first Redis operation."""
        self.stream_key: str = stream_key
        self._group: str | None = None
        try:
            self._redis: redis.Redis = redis.from_url(
                redis_url,
                decode_responses=False,
                socket_connect_timeout=5,
            )
        except ValueError as exc:
            raise ValueError("Redis 连接配置无效，请检查 redis_url。") from exc

    async def publish(self, msg: WecomMessage) -> None:
        """Append a message as a JSON payload field using XADD."""
        try:
            await self._redis.xadd(self.stream_key, {"payload": msg.model_dump_json()})
        except (RedisConnectionError, RedisTimeoutError) as exc:
            logger.error(
                "Redis Stream 消息入队失败",
                msg_id=msg.msg_id,
                stream=self.stream_key,
                error=type(exc).__name__,
            )
            raise RedisConnectionError(_CONNECTION_ERROR) from exc
        except Exception as exc:
            logger.error(
                "Redis Stream 消息入队失败",
                msg_id=msg.msg_id,
                stream=self.stream_key,
                error=type(exc).__name__,
            )
            raise
        logger.info(
            "Redis Stream 消息已入队", msg_id=msg.msg_id, stream=self.stream_key
        )

    async def consume(
        self,
        group: str,
        consumer: str,
        batch: int = 10,
        block_ms: int = 5000,
    ) -> AsyncGenerator[WecomMessage, None]:
        """Yield new messages and acknowledge each on resume or explicit close.

        Create the group from stream ID 0 so earlier publications are included.
        Invalid payloads are logged and acknowledged without being yielded.
        Use contextlib.aclosing when stopping iteration early. This method does
        not recover pending entries left by interrupted consumers.
        """
        if not group or not consumer:
            raise ValueError("group and consumer must not be empty")
        if batch <= 0 or block_ms < 0:
            raise ValueError("batch must be positive and block_ms must be non-negative")
        if self._group is not None and self._group != group:
            raise ValueError("A MessageQueue instance can only consume one group")
        self._group = group

        try:
            try:
                await self._redis.xgroup_create(
                    self.stream_key, group, id="0", mkstream=True
                )
            except ResponseError as exc:
                if not str(exc).startswith("BUSYGROUP"):
                    raise

            while True:
                # A completed Redis write can race with task cancellation in
                # Python 3.11's wait_for. Preserve cancellation at this boundary.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                streams = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={self.stream_key: ">"},
                    count=batch,
                    block=block_ms,
                )
                for _stream, entries in streams:
                    for raw_entry_id, fields in entries:
                        entry_id = (
                            raw_entry_id.decode("ascii")
                            if isinstance(raw_entry_id, bytes)
                            else raw_entry_id
                        )
                        payload = fields.get(b"payload", fields.get("payload"))
                        try:
                            message = WecomMessage.model_validate_json(payload)
                        except (ValidationError, TypeError) as exc:
                            logger.warning(
                                "无法解析 Redis Stream 消息，尝试 ACK 后丢弃",
                                msg_id=None,
                                stream=self.stream_key,
                                entry_id=entry_id,
                                error=type(exc).__name__,
                            )
                            await self.ack(entry_id)
                            continue

                        try:
                            yield message
                        finally:
                            await self.ack(entry_id)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            raise RedisConnectionError(_CONNECTION_ERROR) from exc

    async def ack(self, entry_id: str) -> None:
        """Acknowledge an entry in the group established by consume."""
        if self._group is None:
            raise RuntimeError("Start consume before calling ack to select a group")
        try:
            await self._redis.xack(self.stream_key, self._group, entry_id)
        except (RedisConnectionError, RedisTimeoutError) as exc:
            raise RedisConnectionError(_CONNECTION_ERROR) from exc

    async def aclose(self) -> None:
        """Close the Redis client and its connection pool."""
        await self._redis.aclose()
