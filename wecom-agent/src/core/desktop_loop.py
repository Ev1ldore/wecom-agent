"""Filter authorized desktop messages and persist dry-run or live reply outcomes."""

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from typing import Protocol

from structlog.contextvars import bound_contextvars

from adapters.inbox import Inbox, Msg
from src.core.desktop_store import StoreProtocol
from src.core.logging import get_logger
from src.gateway.wecom_client import WecomClientProtocol

LLM_TIMEOUT_SECONDS = 25.0
PREVIEW_TIMEOUT_SECONDS = 25.0
SEND_FAILURE_LIMIT = 3
SEND_COOLDOWN_SECONDS = 60.0
logger = get_logger(__name__)
RunStats = dict[str, int | bool]


class LLMProtocol(Protocol):
    """Expose a single bounded model operation; its adapter owns HTTP retries."""

    async def reply(self, content: str) -> str:
        """Generate one reply or raise without performing an outbound chat send."""
        raise NotImplementedError


class LoopProtocol(Protocol):
    """Define one non-overlapping inbox polling and message-processing cycle."""

    async def run_once(self) -> RunStats:
        """Return observed filtering and processing counts for one cycle."""
        raise NotImplementedError


class ReplyPreviewProtocol(Protocol):
    """Bind a dry-run reply to its actual destination without sending it."""

    async def prepare_reply(self, user_id: str, content: str) -> None:
        """Validate the original personal chat and leave its input untouched."""
        raise NotImplementedError


def message_key(msg: Msg, bucket_seconds: int = 5) -> str:
    """Keep second buckets stable and use the exact minute label without an epoch."""
    if type(bucket_seconds) is not int or bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be a positive integer")
    digest = hashlib.sha256(msg.content.encode("utf-8")).hexdigest()
    if msg.time_precision == "minute":
        bucket: int | tuple[str, str | None] = ("display-minute", msg.time_display)
    else:
        assert msg.ts is not None
        bucket = msg.ts // bucket_seconds
    return json.dumps(
        (msg.chat, digest, bucket),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _safe_error(exc: Exception) -> str:
    """Retain only exception type and an optional integer provider error code."""
    error = type(exc).__name__
    errcode = getattr(exc, "errcode", None)
    if type(errcode) is int:
        error += f" (errcode={errcode})"
    return error


class AgentLoop:
    """Authorize before model calls and persist claims before generating replies.

    Dry-run mode never invokes the sender. The optional sending branch does not
    retry at this layer and pauses polling after three consecutive send errors.
    """

    def __init__(
        self,
        inbox: Inbox,
        llm: LLMProtocol,
        sender: WecomClientProtocol,
        store: StoreProtocol,
        *,
        allowed_chat: str,
        allowed_sender: str = "LiChengYun",
        self_senders: frozenset[str],
        dry_run: bool = True,
        bucket_seconds: int = 5,
        monotonic: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] = print,
        reply_preview: ReplyPreviewProtocol | None = None,
    ) -> None:
        """Require explicit chat and agent identities for every polling cycle."""
        if not allowed_chat.strip() or not allowed_sender.strip():
            raise ValueError("An allowed chat and sender are required")
        if type(bucket_seconds) is not int or bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be a positive integer")
        self._inbox = inbox
        self._llm = llm
        self._sender = sender
        self._store = store
        self._allowed_chat = allowed_chat
        self._allowed_sender = allowed_sender
        self._self_senders = frozenset(self_senders)
        self._dry_run = dry_run
        self._bucket_seconds = bucket_seconds
        self._monotonic = monotonic
        self._emit = emit
        self._reply_preview = reply_preview
        self._consecutive_send_failures: int = 0
        self._paused_until: float = 0.0
        self._pending: list[Msg] = []
        self._run_lock = asyncio.Lock()

    def _authorized(self, msg: Msg) -> bool:
        """Require the selected incoming peer and reject the replying identity."""
        return (
            not msg.is_self
            and msg.sender not in self._self_senders
            and msg.chat_type == "single"
            and msg.chat == self._allowed_chat
            and msg.sender == self._allowed_sender
            and bool(msg.content.strip())
        )

    async def run_once(self) -> RunStats:
        """Process one poll, or leave the inbox untouched during send cooldown."""
        async with self._run_lock:
            stats: RunStats = {
                "polled": 0,
                "filtered": 0,
                "duplicate": 0,
                "dry_run": 0,
                "sent": 0,
                "llm_failed": 0,
                "send_failed": 0,
                "emit_failed": 0,
                "deferred": len(self._pending),
                "paused": False,
            }
            now = self._monotonic()
            if now < self._paused_until:
                stats["paused"] = True
                return stats
            if self._paused_until:
                self._paused_until = 0.0
                self._consecutive_send_failures = 0

            if self._pending:
                messages, self._pending = self._pending, []
                stats["deferred"] = 0
            else:
                messages = await self._inbox.poll_unread()
                stats["polled"] = len(messages)
            for index, msg in enumerate(messages):
                if not self._authorized(msg):
                    stats["filtered"] += 1
                    continue
                key = message_key(msg, self._bucket_seconds)
                with bound_contextvars(msg_id=key):
                    if not await self._store.claim(msg, key):
                        stats["duplicate"] += 1
                        logger.info("desktop_message_duplicate")
                        continue
                    try:
                        async with asyncio.timeout(LLM_TIMEOUT_SECONDS):
                            reply = await self._llm.reply(msg.content)
                        if not isinstance(reply, str) or not reply.strip():
                            raise ValueError("The model returned no textual reply")
                    except Exception as exc:  # noqa: BLE001 - Persist failure and continue safely.
                        await self._store.finish(
                            key, "llm_failed", error=_safe_error(exc)
                        )
                        stats["llm_failed"] += 1
                        logger.error("desktop_llm_failed", error=type(exc).__name__)
                        continue

                    if self._dry_run:
                        try:
                            if self._reply_preview is not None:
                                async with asyncio.timeout(PREVIEW_TIMEOUT_SECONDS):
                                    await self._reply_preview.prepare_reply(
                                        msg.sender, reply
                                    )
                            time_note = (
                                f"（企微原始显示时间：{msg.time_display}，分钟精度）"
                                if msg.time_precision == "minute"
                                else ""
                            )
                            self._emit(
                                f"[DRY-RUN] 将发送至 {msg.sender}{time_note}：{reply}"
                            )
                        except Exception as exc:  # noqa: BLE001 - Never retry an uncertain output.
                            await self._store.finish(
                                key, "emit_failed", reply=reply, error=_safe_error(exc)
                            )
                            stats["emit_failed"] += 1
                            logger.error(
                                "desktop_emit_failed", error=type(exc).__name__
                            )
                        else:
                            try:
                                await self._store.finish(key, "dry_run", reply=reply)
                            except Exception as exc:
                                logger.error(
                                    "desktop_outcome_persistence_failed",
                                    output_printed=True,
                                    persistence_failed=True,
                                    error=type(exc).__name__,
                                )
                                raise
                            stats["dry_run"] += 1
                            logger.info("desktop_dry_run")
                        continue

                    try:
                        await self._sender.send_text(msg.sender, reply)
                    except asyncio.CancelledError as exc:
                        if getattr(exc, "outcome_unknown", False):
                            try:
                                await self._store.finish(
                                    key,
                                    "send_unknown",
                                    reply=reply,
                                    error=type(exc).__name__,
                                )
                            except Exception as persistence_exc:  # noqa: BLE001 - Preserve caller cancellation while auditing private storage failures.
                                logger.error(
                                    "desktop_outcome_persistence_failed",
                                    outcome_unknown=True,
                                    error=type(persistence_exc).__name__,
                                )
                            logger.error("desktop_send_unknown", cancelled=True)
                        raise
                    except Exception as exc:  # noqa: BLE001 - A send may have an unknown outcome.
                        unknown = bool(getattr(exc, "outcome_unknown", False))
                        try:
                            await self._store.finish(
                                key,
                                "send_unknown" if unknown else "send_failed",
                                reply=reply,
                                error=_safe_error(exc),
                            )
                        except Exception as persistence_exc:
                            logger.error(
                                "desktop_outcome_persistence_failed",
                                outcome_unknown=unknown,
                                persistence_failed=True,
                                error=type(persistence_exc).__name__,
                            )
                            raise
                        stats["send_failed"] += 1
                        self._consecutive_send_failures += 1
                        fields: dict[str, str | int] = {"error": type(exc).__name__}
                        errcode = getattr(exc, "errcode", None)
                        if type(errcode) is int:
                            fields["errcode"] = errcode
                        logger.error(
                            "desktop_send_unknown"
                            if unknown
                            else "desktop_send_failed",
                            **fields,
                        )
                        if self._consecutive_send_failures >= SEND_FAILURE_LIMIT:
                            self._paused_until = (
                                self._monotonic() + SEND_COOLDOWN_SECONDS
                            )
                            self._pending = messages[index + 1 :]
                            stats["paused"] = True
                            stats["deferred"] = len(self._pending)
                            logger.warning(
                                "desktop_send_circuit_open", cooldown_seconds=60
                            )
                            break
                    else:
                        try:
                            await self._store.finish(key, "sent", reply=reply)
                        except Exception as exc:
                            logger.error(
                                "desktop_outcome_persistence_failed",
                                send_accepted=True,
                                persistence_failed=True,
                                error=type(exc).__name__,
                            )
                            raise
                        self._consecutive_send_failures = 0
                        stats["sent"] += 1
                        logger.info("desktop_text_sent")
            return stats
