"""Verify one explicitly identified real CLI message through the guarded dry-run path."""

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from adapters.inbox import Inbox, Msg
from adapters.wecom_cli import WecomCLIInbox
from src.core.desktop_llm import HTTPReplyModel, ReplyModel
from src.core.desktop_loop import AgentLoop, RunStats, message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings

PROOF_TEXT = "本地dry-run测试-1017"
INBOX_TIMEOUT_SECONDS = 25.0


class InboxFactory(Protocol):
    """Construct a fresh verified CLI adapter for one exact proof window."""

    def __call__(
        self,
        *,
        chat_id: str,
        sender_id: str,
        normalized_chat: str,
        normalized_sender: str,
        verified_single_chat: bool,
        timezone_name: str,
        start_at: float,
    ) -> Inbox:
        """Return a receive-only inbox without changing its source identities."""
        raise NotImplementedError


class ModelFactory(Protocol):
    """Construct the configured compatible model with explicit resource ownership."""

    def __call__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        template_name: str,
    ) -> AbstractAsyncContextManager[ReplyModel]:
        """Return a scoped model implementation; smoke tests inject mocked HTTP."""
        raise NotImplementedError


class LocalCLISmokeProtocol(Protocol):
    """Define the proof-specific real-inbox dry-run verification operation."""

    async def __call__(
        self,
        settings: DesktopSettings,
        *,
        message_ts: int,
        proof_text: str = PROOF_TEXT,
    ) -> "LocalCLISmokeResult":
        """Verify the exact proof without any outbound chat-send operation."""
        raise NotImplementedError


class LocalCLISmokeError(Exception):
    """Report a safe proof failure without raw CLI, HTTP or credential details."""


@dataclass(frozen=True)
class LocalCLISmokeResult:
    """Expose whether this run generated a reply or only confirmed prior dry-run dedup."""

    already_completed: bool
    llm_calls: int
    first_stats: RunStats
    replay_stats: RunStats
    reply_saved: bool


class NoSendSender:
    """Reject every outbound chat-send invocation while counting attempted calls."""

    def __init__(self) -> None:
        """Create no HTTP client and start with zero attempted sends."""
        self.calls: int = 0

    async def send_text(self, user_id: str, content: str) -> None:
        """Fail immediately instead of sending anything to enterprise WeChat."""
        self.calls += 1
        raise LocalCLISmokeError("Proof smoke must never invoke an outbound sender")


class _ProofInbox:
    """Pass one already validated proof message to the real processing loop."""

    def __init__(self, message: Msg) -> None:
        self._message = message

    async def poll_unread(self) -> list[Msg]:
        """Expose only the exact proof, never neighboring source messages."""
        return [self._message]


class _CountedModel:
    """Allow at most one fresh model operation and actively forbid replay calls."""

    def __init__(self, model: ReplyModel | None) -> None:
        self._model = model
        self.calls: int = 0
        self.blocked: bool = model is None

    async def reply(self, content: str) -> str:
        """Count each invocation before rejecting any unexpected second generation."""
        self.calls += 1
        if self.blocked or self.calls != 1 or self._model is None:
            raise LocalCLISmokeError(
                "Proof replay attempted an additional model operation"
            )
        return await self._model.reply(content)


def _require(condition: bool, message: str) -> None:
    """Raise an explicit smoke failure instead of weakening a proof condition."""
    if not condition:
        raise LocalCLISmokeError(message)


async def _read_proof(
    settings: DesktopSettings,
    message_ts: int,
    proof_text: str,
    inbox_factory: InboxFactory,
) -> Msg:
    """Require one exact text/timestamp match from a newly constructed adapter."""
    inbox = inbox_factory(
        chat_id=settings.inbox_chat_id,
        sender_id=settings.inbox_sender_id,
        normalized_chat=settings.allowed_chat,
        normalized_sender=settings.allowed_user_id,
        verified_single_chat=settings.inbox_verified_single_chat,
        timezone_name=settings.inbox_timezone,
        start_at=message_ts - 1,
    )
    async with asyncio.timeout(INBOX_TIMEOUT_SECONDS):
        messages = await inbox.poll_unread()
    matches = [
        msg for msg in messages if msg.content == proof_text and msg.ts == message_ts
    ]
    _require(
        bool(matches), "Exact proof message was not found in the selected CLI window"
    )
    _require(
        len(matches) == 1, "Multiple exact proof messages found; input is ambiguous"
    )
    message = matches[0]
    _require(
        message.chat == settings.allowed_chat
        and message.sender == settings.allowed_user_id
        and message.chat_type == "single"
        and not message.is_self,
        "Proof message does not match the verified single-chat/user mapping",
    )
    return message


def _validate_stored(row: dict[str, Any], message: Msg) -> None:
    """Require the exact persisted input before reusing any previous proof result."""
    _require(
        all(row.get(field) == value for field, value in message.model_dump().items()),
        "Proof dedup key belongs to a different stored input; manual inspection required",
    )
    status = row.get("status")
    safe_status = (
        status
        if status in {"claimed", "llm_failed", "send_failed", "emit_failed", "sent"}
        else "other"
    )
    _require(
        status == "dry_run",
        f"Existing proof claim is incomplete or failed (status={safe_status}); claim retained",
    )
    _require(
        isinstance(row.get("reply"), str) and bool(row["reply"].strip()),
        "Existing dry_run proof has no complete textual reply; claim retained",
    )


@asynccontextmanager
async def _proof_model(
    settings: DesktopSettings, already_completed: bool, factory: ModelFactory
) -> AsyncIterator[_CountedModel]:
    """Avoid creating any model or HTTP client when a completed proof already exists."""
    if already_completed:
        yield _CountedModel(None)
        return
    async with factory(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.llm_model,
        template_name=settings.llm_template_name,
    ) as model:
        yield _CountedModel(model)


def _loop(
    settings: DesktopSettings,
    message: Msg,
    model: ReplyModel,
    sender: NoSendSender,
    store: SQLiteStore,
    emitted: list[str],
) -> AgentLoop:
    """Reuse production filtering, claim, generation and dry-run persistence."""
    return AgentLoop(
        _ProofInbox(message),
        model,
        sender,
        store,
        allowed_chat=settings.allowed_chat,
        allowed_sender=settings.allowed_user_id,
        self_senders=frozenset({f"app:{settings.wecom_agent_id}"}),
        dry_run=True,
        bucket_seconds=settings.dedup_bucket_seconds,
        emit=emitted.append,
    )


async def _run(
    settings: DesktopSettings,
    message_ts: int,
    proof_text: str,
    inbox_factory: InboxFactory,
    model_factory: ModelFactory,
) -> LocalCLISmokeResult:
    """Process once, reopen local storage and require an independent source replay."""
    _require(settings.dry_run is True, "Local CLI smoke requires DRY_RUN=true")
    _require(
        settings.inbox_verified_single_chat is True
        and bool(settings.inbox_chat_id.strip())
        and bool(settings.inbox_sender_id.strip()),
        "A verified single chat and explicit CLI sender/chat IDs are required",
    )
    _require(
        type(message_ts) is int and message_ts > 1,
        "message_ts must be a positive epoch second",
    )
    _require(
        isinstance(proof_text, str) and bool(proof_text.strip()),
        "proof_text must not be empty",
    )
    proof = await _read_proof(settings, message_ts, proof_text, inbox_factory)
    key = message_key(proof, settings.dedup_bucket_seconds)
    sender = NoSendSender()
    emitted: list[str] = []
    store = SQLiteStore(settings.sqlite_path)
    try:
        prior = next((row for row in await store.records() if row["key"] == key), None)
        already_completed = prior is not None
        if prior is not None:
            _validate_stored(prior, proof)
        async with _proof_model(settings, already_completed, model_factory) as model:
            first = await _loop(
                settings, proof, model, sender, store, emitted
            ).run_once()
            rows = [row for row in await store.records() if row["key"] == key]
            _require(
                len(rows) == 1,
                "Proof processing did not retain exactly one SQLite record",
            )
            _validate_stored(rows[0], proof)
            _require(
                first["sent"] == sender.calls == 0
                and first["dry_run"] == (0 if already_completed else 1)
                and first["duplicate"] == (1 if already_completed else 0),
                "Proof processing did not satisfy the exact dry-run/send/dedup counts",
            )
            _require(
                model.calls == (0 if already_completed else 1),
                "Unexpected model operation count",
            )
            _require(
                emitted
                == (
                    []
                    if already_completed
                    else [f"[DRY-RUN] 将发送至 {proof.sender}：{rows[0]['reply']}"]
                ),
                "Dry-run output did not preserve the complete stored reply",
            )
            await store.aclose()
            model.blocked = True
            repeated_proof = await _read_proof(
                settings, message_ts, proof_text, inbox_factory
            )
            reopened = SQLiteStore(settings.sqlite_path)
            try:
                replay = await _loop(
                    settings, repeated_proof, model, sender, reopened, emitted
                ).run_once()
                repeated_rows = [
                    row for row in await reopened.records() if row["key"] == key
                ]
                _require(
                    repeated_rows == rows, "Replay changed the stored proof result"
                )
                _require(
                    replay["duplicate"] == 1
                    and replay["dry_run"] == replay["sent"] == sender.calls == 0,
                    "Source replay did not complete exactly one duplicate with zero sends",
                )
                _require(
                    model.calls == (0 if already_completed else 1),
                    "Replay attempted an additional model operation",
                )
            finally:
                await reopened.aclose()
            result = LocalCLISmokeResult(
                already_completed, model.calls, first, replay, True
            )
    finally:
        await store.aclose()
    if result.already_completed:
        print(
            "local CLI smoke: PASS; prior dry_run confirmed by dedup; fresh LLM calls=0; real sends=0"
        )
    else:
        print(
            "local CLI smoke: PASS; new dry_run=1; fresh LLM calls=1; replay duplicate=1; real sends=0; complete reply saved in SQLite"
        )
    return result


async def run_local_cli_smoke(
    settings: DesktopSettings,
    *,
    message_ts: int,
    proof_text: str = PROOF_TEXT,
    inbox_factory: InboxFactory = WecomCLIInbox,
    model_factory: ModelFactory = HTTPReplyModel,
) -> LocalCLISmokeResult:
    """Verify a single proof without reading dotenv or bypassing an existing claim."""
    error_type = "unknown"
    try:
        return await _run(
            settings, message_ts, proof_text, inbox_factory, model_factory
        )
    except LocalCLISmokeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- Never expose raw external diagnostics.
        error_type = type(exc).__name__
    raise LocalCLISmokeError(f"Local CLI proof verification failed ({error_type})")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicitly requested proof and report safe, non-successful failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-ts", required=True, type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = DesktopSettings()
        runner: LocalCLISmokeProtocol = run_local_cli_smoke
        asyncio.run(runner(settings, message_ts=args.message_ts))
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(map(str, error["loc"]))
                for error in exc.errors(include_input=False, include_context=False)
            }
        )
        print(
            "Local CLI smoke configuration is missing or invalid: " + ", ".join(fields)
        )
        return 2
    except LocalCLISmokeError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Local CLI smoke cancelled; real sending remains disabled")
        return 130
    except Exception as exc:  # noqa: BLE001 -- Keep unknown errors credential-free.
        print(f"Local CLI smoke failed ({type(exc).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
