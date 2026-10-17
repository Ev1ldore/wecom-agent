"""Verify WeCom callbacks and enqueue normalized messages without business work."""

import asyncio
from typing import Annotated, Any
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from redis import asyncio as redis
from structlog.contextvars import bound_contextvars

from src.config import get_settings
from src.core.logging import get_logger
from src.core.models import WecomMessage
from src.core.queue import MessageQueue
from src.gateway.wecom_crypto import WXBizMsgCrypt

logger = get_logger(__name__)
router = APIRouter(prefix="/wecom")

GET_TIMEOUT_SECONDS = 0.8
POST_TIMEOUT_SECONDS = 4.0
DEDUP_TTL_SECONDS = 300
SUPPORTED_MESSAGE_TYPES = frozenset({"text", "voice", "image", "file", "event"})


class WecomCryptoError(ValueError):
    """A callback SDK operation returned a nonzero result code."""

    def __init__(self, operation: str, ret: int) -> None:
        """Keep the SDK return code without including message contents."""
        self.ret = ret
        super().__init__(f"{operation} returned {ret}")


async def get_queue(request: Request) -> MessageQueue:
    """Get the shared queue created by the application's lifespan."""
    return request.app.state.queue


async def get_redis(request: Request) -> redis.Redis:
    """Get the shared asyncio Redis client used for callback deduplication."""
    return request.app.state.callback_redis


def _require_query(*values: str) -> None:
    """Reject missing verification parameters inside the callback boundary."""
    if not all(values):
        raise ValueError("Missing callback verification parameters")


def _verify_url(msg_signature: str, timestamp: str, nonce: str, echostr: str) -> bytes:
    """Run synchronous SDK verification outside the event loop."""
    _require_query(msg_signature, timestamp, nonce, echostr)
    settings = get_settings()
    crypt = WXBizMsgCrypt(
        settings.wecom_token, settings.wecom_aes_key, settings.wecom_corpid
    )
    ret, plaintext = crypt.VerifyURL(msg_signature, timestamp, nonce, echostr)
    if ret != 0:
        raise WecomCryptoError("VerifyURL", ret)
    return plaintext


def _xml_fields(element: ElementTree.Element) -> dict[str, Any]:
    """Keep decrypted XML fields, nested elements and repeated child tags."""
    fields: dict[str, Any] = {}
    for child in element:
        value = _xml_fields(child) if len(child) else (child.text or "")
        if child.tag not in fields:
            fields[child.tag] = value
        elif isinstance(fields[child.tag], list):
            fields[child.tag].append(value)
        else:
            fields[child.tag] = [fields[child.tag], value]
    return fields


def _required_text(raw: dict[str, Any], name: str) -> str:
    """Read a required scalar XML field without changing its contents."""
    value = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid XML field: {name}")
    return value


def _decrypt_message(
    post_data: bytes, msg_signature: str, timestamp: str, nonce: str
) -> WecomMessage:
    """Verify, decrypt and parse XML in a worker thread, without Redis writes."""
    _require_query(msg_signature, timestamp, nonce)
    envelope = ElementTree.fromstring(post_data)
    if envelope.tag != "xml" or not envelope.findtext("ToUserName"):
        raise ValueError("Invalid callback XML envelope")
    if not envelope.findtext("Encrypt"):
        raise ValueError("Missing encrypted callback payload")

    settings = get_settings()
    crypt = WXBizMsgCrypt(
        settings.wecom_token, settings.wecom_aes_key, settings.wecom_corpid
    )
    ret, plaintext = crypt.DecryptMsg(post_data, msg_signature, timestamp, nonce)
    if ret != 0:
        raise WecomCryptoError("DecryptMsg", ret)

    root = ElementTree.fromstring(plaintext)
    if root.tag != "xml":
        raise ValueError("Invalid decrypted XML root")
    raw = _xml_fields(root)
    from_user = _required_text(raw, "FromUserName")
    create_time = _required_text(raw, "CreateTime")
    msg_type = _required_text(raw, "MsgType")
    if msg_type not in SUPPORTED_MESSAGE_TYPES:
        raise ValueError("Unsupported callback message type")
    msg_id = raw.get("MsgId") or f"{from_user}-{create_time}"
    return WecomMessage(
        msg_id=msg_id,
        msg_type=msg_type,
        from_user=from_user,
        chat_type="single",
        content=raw.get("Content") if msg_type == "text" else None,
        raw=raw,
        ts=int(create_time),
    )


@router.get("/callback", response_class=PlainTextResponse)
async def verify_callback(
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
) -> PlainTextResponse:
    """Return the exact decrypted challenge; reject verification errors with 400."""
    try:
        async with asyncio.timeout(GET_TIMEOUT_SECONDS):
            plaintext = await asyncio.to_thread(
                _verify_url, msg_signature, timestamp, nonce, echostr
            )
        return PlainTextResponse(plaintext)
    except Exception as exc:  # noqa: BLE001 -- Protocol boundary; keep SDK data out of logs.
        logger.error(
            "企微 URL 验证失败",
            msg_id=None,
            error=type(exc).__name__,
            ret=getattr(exc, "ret", None),
        )
        return PlainTextResponse("", status_code=400)


@router.post("/callback", response_class=PlainTextResponse)
async def receive_callback(
    request: Request,
    queue: Annotated[MessageQueue, Depends(get_queue)],
    redis_client: Annotated[redis.Redis, Depends(get_redis)],
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
) -> PlainTextResponse:
    """Verify, deduplicate and await publication; acknowledge POST errors with 200.

    SET NX EX and publication are separate operations. A failure after SET can
    leave a dedup key without a Stream entry; this is not exactly-once delivery.
    """
    callback_logger = logger.bind(msg_id=None)
    try:
        async with asyncio.timeout(POST_TIMEOUT_SECONDS):
            body = await request.body()
            message = await asyncio.to_thread(
                _decrypt_message, body, msg_signature, timestamp, nonce
            )
            callback_logger = logger.bind(msg_id=message.msg_id)
            settings = get_settings()
            dedup_key = (
                f"{settings.queue_key}:dedup:{settings.wecom_corpid}:{message.msg_id}"
            )
            if await redis_client.set(dedup_key, "1", nx=True, ex=DEDUP_TTL_SECONDS):
                with bound_contextvars(msg_id=message.msg_id):
                    await queue.publish(message)
                callback_logger.info("企微回调消息已入队", msg_type=message.msg_type)
            else:
                callback_logger.info(
                    "企微回调重复消息已跳过", msg_type=message.msg_type
                )
    except Exception as exc:  # noqa: BLE001 -- Protocol boundary must acknowledge POST errors.
        callback_logger.error(
            "企微消息回调失败，返回 200 空串；消息可能未入队，请检查 Redis 和回调配置",
            error=type(exc).__name__,
            ret=getattr(exc, "ret", None),
        )
    return PlainTextResponse("")
