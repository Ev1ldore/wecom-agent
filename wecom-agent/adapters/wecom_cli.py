"""Poll an explicitly verified single chat using the CLI's discovery v1 schema.

The timezone is an operator setting, not a verified server timezone. Discovery
does not provide message IDs or direction flags; callers still deduplicate.
"""

import asyncio
import json
import math
import os
import signal
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adapters.inbox import InboxError, Msg
from src.core.logging import get_logger

logger = get_logger(__name__)
CLI_TIMEOUT_SECONDS = 10.0
CLI_CLEANUP_SECONDS = 3.0
IS_POSIX = os.name == "posix"
MAX_PAGES = 10
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class CLIInboxError(InboxError):
    """Expose safe local diagnostics and an optional upstream numeric code."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        """Keep diagnostic codes without retaining response bodies or stderr."""
        self.code = code
        super().__init__(message)


class CLIRunner(Protocol):
    """Execute only a receive command and return its parsed JSON object."""

    async def __call__(self, argv: list[str]) -> dict[str, Any]:
        """Read one CLI response or raise an InboxError."""
        raise NotImplementedError


def _observe_cleanup(task: asyncio.Task[Any]) -> None:
    """Retrieve a finished cleanup task's exception without emitting raw output."""
    if not task.cancelled():
        task.exception()


async def stop_cli_process(
    process: asyncio.subprocess.Process, *, isolated_session: bool = False
) -> None:
    """Kill and reap an owned child within three seconds.

    Set isolated_session only for a process created by this caller with
    start_new_session=True. Never pass an existing interactive/auth process.
    That new session makes pid its own PGID; children inherit that private
    group. The guard prevents targeting this application's process group.
    """
    if IS_POSIX and isolated_session:
        pid = process.pid
        if type(pid) is not int or pid <= 1 or pid == os.getpgrp():
            raise CLIInboxError("Refusing unsafe CLI process-group cleanup")
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
    elif process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()

    async def drain_and_reap() -> None:
        """Drain inherited pipes and reap the direct child after group exit."""
        await process.communicate()
        await process.wait()

    cleanup = asyncio.create_task(drain_and_reap())
    waiter = asyncio.create_task(asyncio.wait({cleanup}, timeout=CLI_CLEANUP_SECONDS))
    cancelled = False
    # Preserve caller cancellation while allowing this bounded cleanup to finish.
    while True:
        try:
            done, _pending = await asyncio.shield(waiter)
            break
        except asyncio.CancelledError:
            if waiter.cancelled():
                cleanup.cancel()
                cleanup.add_done_callback(_observe_cleanup)
                raise
            cancelled = True
    if not done:
        cleanup.cancel()
        cleanup.add_done_callback(_observe_cleanup)
        if cancelled:
            raise asyncio.CancelledError
        raise CLIInboxError("Timed out reaping CLI process")
    try:
        cleanup.result()
    except (OSError, ValueError):
        raise CLIInboxError("Unable to reap CLI process") from None
    if cancelled:
        raise asyncio.CancelledError


def _upstream_code(data: dict[str, Any]) -> int | None:
    """Read a numeric CLI/API code without exposing accompanying messages."""
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else data.get("errcode")
    return code if type(code) is int else None


class SubprocessCLIRunner:
    """Run a bounded receive-only subprocess, with one retry and child cleanup."""

    async def __call__(self, argv: list[str]) -> dict[str, Any]:
        """Execute a fixed argv shape; never authenticate or send messages."""
        if len(argv) != 6 or argv[:5] != [
            "wecom-cli",
            "chat",
            "messages",
            "list",
            "--json",
        ]:
            raise CLIInboxError("Unsupported CLI receive command")
        failure = CLIInboxError("CLI receive request failed")
        for attempt in range(2):
            try:
                return await self._once(argv)
            except (CLIInboxError, OSError, TimeoutError, ValueError) as exc:
                code = exc.code if isinstance(exc, CLIInboxError) else None
                failure = CLIInboxError(
                    f"CLI receive request failed ({type(exc).__name__})", code=code
                )
                logger.warning(
                    "wecom_cli_receive_failed",
                    msg_id=None,
                    attempt=attempt + 1,
                    code=code,
                    error=type(exc).__name__,
                )
        raise failure from None

    async def _once(self, argv: list[str]) -> dict[str, Any]:
        """Apply one total deadline and reap a child on timeout or cancellation."""
        process: asyncio.subprocess.Process | None = None
        drained = False
        environment = os.environ.copy()
        environment.pop("WECOM_CLI_LOG_DIR", None)
        environment["WECOM_CLI_LOG_LEVEL"] = "warn"
        try:
            async with asyncio.timeout(CLI_TIMEOUT_SECONDS):
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=environment,
                    start_new_session=IS_POSIX,
                )
                stdout, _stderr = await process.communicate()
                drained = True
                data = json.loads(stdout)
                if not isinstance(data, dict):
                    raise CLIInboxError("CLI response must be a JSON object")
                code = _upstream_code(data)
                if process.returncode != 0 or "error" in data or code not in (None, 0):
                    raise CLIInboxError("CLI returned an error", code=code)
                return data
        finally:
            if process is not None and not drained:
                await stop_cli_process(process, isolated_session=IS_POSIX)


class WecomCLIInbox:
    """Map one verified raw chat/sender pair to an official application identity.

    A fractional start time is rounded upward so a second before startup is
    never included. Successful polls overlap by five seconds; errors retain the
    previous checkpoint. CLI ciphertext IDs are never used as official IDs.
    """

    def __init__(
        self,
        *,
        chat_id: str,
        sender_id: str,
        normalized_chat: str,
        normalized_sender: str = "LiChengYun",
        verified_single_chat: bool = False,
        timezone_name: str = "Asia/Shanghai",
        start_at: float | None = None,
        runner: CLIRunner | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Require an operator-verified mapping before any CLI process can run."""
        if verified_single_chat is not True or any(
            not isinstance(value, str) or not value.strip()
            for value in (chat_id, sender_id, normalized_chat, normalized_sender)
        ):
            raise ValueError(
                "A verified single chat and explicit identity mapping are required"
            )
        try:
            self._timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("Invalid configured CLI timezone") from None
        initial = clock() if start_at is None else start_at
        if not math.isfinite(initial) or initial <= 0:
            raise ValueError("start_at must be a finite positive timestamp")
        self._initial_start = math.ceil(initial)
        self._watermark = self._initial_start
        self._chat_id = chat_id
        self._sender_id = sender_id
        self._normalized_chat = normalized_chat
        self._normalized_sender = normalized_sender
        self._runner = runner if runner is not None else SubprocessCLIRunner()
        self._clock = clock
        self._lock = asyncio.Lock()

    def _format_time(self, timestamp: int) -> str:
        """Format a timestamp using the explicitly configured local timezone."""
        return datetime.fromtimestamp(timestamp, self._timezone).strftime(TIME_FORMAT)

    def _message(self, raw: Any, begin: int, end: int) -> Msg | None:
        """Read only exact schema fields and the explicitly allowlisted sender."""
        if not isinstance(raw, dict):
            raise CLIInboxError("Invalid CLI message object")
        sender, kind = raw.get("userid"), raw.get("msg_type")
        if not isinstance(sender, str) or not sender:
            raise CLIInboxError("Invalid CLI message userid")
        if sender != self._sender_id:
            return None
        if not isinstance(kind, str) or not kind:
            raise CLIInboxError("Invalid CLI message msg_type")
        if kind != "text":
            return None
        text, sent = raw.get("text"), raw.get("send_time")
        if (
            not isinstance(text, dict)
            or not isinstance(text.get("content"), str)
            or not isinstance(sent, str)
        ):
            raise CLIInboxError("Invalid CLI text message fields")
        try:
            local = datetime.strptime(sent, TIME_FORMAT).replace(tzinfo=self._timezone)
            if local.strftime(TIME_FORMAT) != sent:
                raise ValueError("Noncanonical timestamp")
            if local.utcoffset() != local.replace(fold=1).utcoffset():
                raise ValueError("Ambiguous timestamp")
            timestamp = int(local.timestamp())
            if self._format_time(timestamp) != sent:
                raise ValueError("Nonexistent local timestamp")
        except (ValueError, OverflowError, OSError):
            raise CLIInboxError(
                "Invalid CLI send_time for the configured timezone"
            ) from None
        if not begin <= timestamp <= end:
            return None
        return Msg(
            chat=self._normalized_chat,
            sender=self._normalized_sender,
            content=text["content"],
            ts=timestamp,
            chat_type="single",
            is_self=False,
        )

    async def poll_unread(self) -> list[Msg]:
        """Return a complete bounded window; never advance after a failed page."""
        async with self._lock:
            end = math.floor(self._clock())
            if end <= self._watermark:
                return []
            begin = max(self._initial_start, self._watermark - 5)
            query: dict[str, str] = {
                "chat_id": self._chat_id,
                "begin_time": self._format_time(begin),
                "end_time": self._format_time(end),
            }
            seen_cursors: set[str] = set()
            messages: list[Msg] = []
            for _page in range(MAX_PAGES):
                argv = [
                    "wecom-cli",
                    "chat",
                    "messages",
                    "list",
                    "--json",
                    json.dumps(query),
                ]
                try:
                    response = await self._runner(argv)
                except CLIInboxError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- Never expose raw runner output.
                    raise CLIInboxError(
                        f"CLI runner failed ({type(exc).__name__})"
                    ) from None
                if not isinstance(response, dict):
                    raise CLIInboxError("CLI response must be a JSON object")
                code = _upstream_code(response)
                if "error" in response or code not in (None, 0):
                    raise CLIInboxError("CLI returned an error", code=code)
                has_more = response.get("has_more")
                rows = response.get("messages")
                # The live empty terminal response omits both optional arrays
                # and cursors. Missing data is empty only with explicit proof.
                if (
                    "messages" not in response
                    and type(response.get("messages_count")) is int
                    and response["messages_count"] == 0
                    and has_more is False
                ):
                    rows = []
                cursor = response.get("next_cursor", "" if has_more is False else None)
                if (
                    not isinstance(rows, list)
                    or type(has_more) is not bool
                    or not isinstance(cursor, str)
                ):
                    raise CLIInboxError("Invalid CLI pagination response")
                count = response.get("messages_count", len(rows))
                if type(count) is not int or count != len(rows):
                    raise CLIInboxError("Invalid CLI messages_count")
                for raw in rows:
                    message = self._message(raw, begin, end)
                    if message is not None:
                        messages.append(message)
                if not has_more:
                    messages.sort(key=lambda message: message.ts)
                    self._watermark = end
                    return messages
                if not cursor or cursor in seen_cursors:
                    raise CLIInboxError("Missing or repeated CLI pagination cursor")
                seen_cursors.add(cursor)
                query["cursor"] = cursor
            raise CLIInboxError("CLI pagination exceeded the ten-page limit")
