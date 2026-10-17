"""Prove one fixed Ri personal-history input through the strictly dry-run pipeline."""

import argparse
import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Protocol

import httpx
from pydantic import ValidationError

from adapters.inbox import Msg
from adapters.wecom_personal import (
    HISTORY_SENDER,
    PERSONAL_IDENTITY,
    PERSONAL_TITLE,
    AppleScriptPersonalHistoryReader,
    PersonalHistoryInbox,
    PersonalHistoryReader,
    PersonalHistoryRow,
    PersonalHistorySnapshot,
)
from src.core.desktop_llm import HTTPReplyModel, ReplyModel
from src.core.desktop_loop import AgentLoop, RunStats, message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings

PROOF_TEXT = "你好"
PROOF_TIME_DISPLAY = "10/16 12:11"
INBOX_TIMEOUT_SECONDS = 22.0
SMOKE_TIMEOUT_SECONDS = 80.0


class ModelFactory(Protocol):
    """Construct the configured model while allowing mocked HTTP in offline tests."""

    def __call__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        template_name: str,
    ) -> AbstractAsyncContextManager[ReplyModel]:
        """Return an owned model scope without selecting any chat sender."""
        raise NotImplementedError


class PersonalSmokeProtocol(Protocol):
    """Expose the fixed existing-message proof without an outbound chat API."""

    async def __call__(self, settings: DesktopSettings) -> "PersonalSmokeResult":
        """Verify Ri's original minute label and durable no-send processing."""
        raise NotImplementedError


class PersonalSmokeError(Exception):
    """Report fixed proof failures without raw history, model bodies or credentials."""


@dataclass(frozen=True)
class PersonalSmokeResult:
    """Expose observed time precision and processing counts without reply content."""

    already_completed: bool
    llm_calls: int
    sender_calls: int
    first_stats: RunStats
    replay_stats: RunStats
    ts: int | None
    time_precision: Literal["second", "minute"]
    time_display: str | None


class NoSendSender:
    """Own no chat client and make every attempted outbound send fail locally."""

    def __init__(self) -> None:
        """Start a counter independent of the loop's reported send statistics."""
        self.calls: int = 0

    async def send_text(self, user_id: str, content: str) -> None:
        """Count and reject all calls, including an accidental dry-run regression."""
        self.calls += 1
        raise PersonalSmokeError("Personal proof must never invoke a sender")


class _ProofInbox:
    """Expose only the already matched proof to production processing."""

    def __init__(self, message: Msg) -> None:
        """Keep the one validated source message without retaining neighboring rows."""
        self._message = message

    async def poll_unread(self) -> list[Msg]:
        """Keep unrelated personal history out of the model and claim database."""
        return [self._message]


class _CountedModel:
    """Permit one new reply and reject all replay generation attempts."""

    def __init__(self, model: ReplyModel | None) -> None:
        """Block generation immediately when a completed durable proof already exists."""
        self._model = model
        self.calls: int = 0
        self.blocked: bool = model is None

    async def reply(self, content: str) -> str:
        """Track attempted operations even if the loop catches their safe failure."""
        self.calls += 1
        if self.blocked or self.calls != 1 or self._model is None:
            raise PersonalSmokeError("Personal proof attempted another model operation")
        return await self._model.reply(content)


def _require(condition: bool, message: str) -> None:
    """Fail explicitly when a proof invariant is not observed."""
    if not condition:
        raise PersonalSmokeError(message)


async def _read_proof(reader: PersonalHistoryReader) -> Msg:
    """Read without navigation and require one exact existing Ri input."""
    async with asyncio.timeout(INBOX_TIMEOUT_SECONDS):
        messages = await PersonalHistoryInbox(reader).poll_unread()
    matches = [
        message
        for message in messages
        if message.content == PROOF_TEXT and message.time_display == PROOF_TIME_DISPLAY
    ]
    _require(len(matches) == 1, "Exact Ri proof is missing or ambiguous")
    proof = matches[0]
    _require(
        proof.chat == proof.sender == PERSONAL_IDENTITY
        and proof.chat_type == "single"
        and not proof.is_self
        and proof.ts is None
        and proof.time_precision == "minute",
        "Personal proof identity or original minute precision was not preserved",
    )
    return proof


def _validate_stored(row: dict[str, Any], proof: Msg) -> None:
    """Reuse only a complete dry-run of the same unchanged source input."""
    _require(
        all(row.get(field) == value for field, value in proof.model_dump().items()),
        "Stored proof does not match the exact original input; claim retained",
    )
    _require(
        row.get("status") == "dry_run",
        "Existing proof is incomplete or failed; claim retained",
    )
    _require(
        isinstance(row.get("reply"), str) and bool(row["reply"].strip()),
        "Stored proof has no complete reply; claim retained",
    )
    _require(row.get("error") == "", "Stored proof contains a failure; claim retained")


@asynccontextmanager
async def _model_scope(
    settings: DesktopSettings,
    already_completed: bool,
    factory: ModelFactory,
) -> AsyncIterator[_CountedModel]:
    """Do not construct an HTTP client for a previously completed proof."""
    if already_completed:
        yield _CountedModel(None)
        return
    async with factory(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.llm_model,
        template_name=settings.personal_llm_template_name,
    ) as model:
        yield _CountedModel(model)


def _loop(
    settings: DesktopSettings,
    proof: Msg,
    model: ReplyModel,
    sender: NoSendSender,
    store: SQLiteStore,
    emitted: list[str],
) -> AgentLoop:
    """Use the real filtering, message key, durable claim and dry-run outcome path."""
    return AgentLoop(
        _ProofInbox(proof),
        model,
        sender,
        store,
        allowed_chat=PERSONAL_IDENTITY,
        allowed_sender=PERSONAL_IDENTITY,
        self_senders=frozenset(),
        dry_run=True,
        bucket_seconds=settings.dedup_bucket_seconds,
        emit=emitted.append,
    )


async def _run(
    settings: DesktopSettings,
    reader: PersonalHistoryReader | None,
    model_factory: ModelFactory,
) -> PersonalSmokeResult:
    """Verify one new or existing claim, then reopen storage and independently reread."""
    _require(settings.dry_run is True, "Personal smoke requires DRY_RUN=true")
    _require(
        settings.personal_contact_name == "Ri"
        and settings.personal_chat == PERSONAL_IDENTITY,
        "Personal smoke requires the fixed verified Ri personal chat",
    )
    source = reader if reader is not None else AppleScriptPersonalHistoryReader()
    proof = await _read_proof(source)
    key = message_key(proof, settings.dedup_bucket_seconds)
    sender = NoSendSender()
    emitted: list[str] = []
    store = SQLiteStore(settings.sqlite_path)
    try:
        prior = next((row for row in await store.records() if row["key"] == key), None)
        already_completed = prior is not None
        if prior is not None:
            _validate_stored(prior, proof)
        async with _model_scope(settings, already_completed, model_factory) as model:
            first = await _loop(
                settings, proof, model, sender, store, emitted
            ).run_once()
            rows = [row for row in await store.records() if row["key"] == key]
            _require(len(rows) == 1, "Personal proof did not retain one durable record")
            _validate_stored(rows[0], proof)
            _require(
                first["polled"] == 1
                and first["filtered"] == 0
                and first["sent"] == sender.calls == 0
                and first["dry_run"] == (0 if already_completed else 1)
                and first["duplicate"] == (1 if already_completed else 0)
                and model.calls == (0 if already_completed else 1),
                "Personal proof counts did not satisfy dry-run and no-send invariants",
            )
            _require(
                emitted
                == (
                    []
                    if already_completed
                    else [
                        (
                            f"[DRY-RUN] 将发送至 {PERSONAL_IDENTITY}"
                            f"（企微原始显示时间：{proof.time_display}，分钟精度）：{rows[0]['reply']}"
                        )
                    ]
                ),
                "Dry-run output did not preserve the complete stored reply",
            )
            await store.aclose()
            model.blocked = True
            replay_proof = await _read_proof(source)
            _require(
                replay_proof == proof,
                "Original proof changed during independent replay",
            )
            reopened = SQLiteStore(settings.sqlite_path)
            try:
                replay = await _loop(
                    settings,
                    replay_proof,
                    model,
                    sender,
                    reopened,
                    emitted,
                ).run_once()
                repeated = [
                    row for row in await reopened.records() if row["key"] == key
                ]
                _require(repeated == rows, "Restart replay changed the stored proof")
                _require(
                    replay["polled"] == replay["duplicate"] == 1
                    and replay["dry_run"] == replay["sent"] == sender.calls == 0
                    and model.calls == (0 if already_completed else 1),
                    "Restart replay did not deduplicate with zero additional model or send calls",
                )
            finally:
                await reopened.aclose()
            result = PersonalSmokeResult(
                already_completed,
                model.calls,
                sender.calls,
                first,
                replay,
                proof.ts,
                proof.time_precision,
                proof.time_display,
            )
    finally:
        await store.aclose()
    print(
        "personal smoke: PASS; chat=desktop:wechat:Ri; ts=NULL; precision=minute; "
        f"original time={result.time_display}; new dry_run={first['dry_run']}; "
        f"fresh LLM calls={result.llm_calls}; replay duplicate={replay['duplicate']}; "
        f"real sends={result.sender_calls}; complete reply saved in SQLite"
    )
    return result


async def run_live_personal_smoke(
    settings: DesktopSettings,
    *,
    reader: PersonalHistoryReader | None = None,
    model_factory: ModelFactory = HTTPReplyModel,
) -> PersonalSmokeResult:
    """Bound the real receive-only proof and redact arbitrary external failures."""
    error_type = "unknown"
    try:
        async with asyncio.timeout(SMOKE_TIMEOUT_SECONDS):
            return await _run(settings, reader, model_factory)
    except PersonalSmokeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- Do not expose native or model diagnostics.
        error_type = type(exc).__name__
    raise PersonalSmokeError(f"Personal dry-run proof failed ({error_type})")


async def run_personal_smoke() -> PersonalSmokeResult:
    """Run fake history, mocked model HTTP and temporary real SQLite without dotenv."""
    reply = "离线个人聊天验证回复。\n完整第二行仅保存到临时 SQLite。"
    http_calls = 0

    class FakeReader:
        """Keep this offline entry independent of macOS and local chat contents."""

        async def read_active_chat(self) -> PersonalHistorySnapshot:
            """Return only synthetic proof data through the public reader contract."""
            return PersonalHistorySnapshot(
                title=PERSONAL_TITLE,
                personal_verified=True,
                history_open=True,
                search_text="",
                rows=(
                    PersonalHistoryRow(
                        senders=(HISTORY_SENDER,),
                        texts=(PROOF_TEXT,),
                        labels=(PROOF_TIME_DISPLAY,),
                    ),
                ),
            )

    def handle(request: httpx.Request) -> httpx.Response:
        """Validate the actual HTTP adapter's payload entirely inside this process."""
        nonlocal http_calls
        http_calls += 1
        _require(
            request.method == "POST", "Offline personal smoke used the wrong method"
        )
        _require(
            request.url.path == "/v1/chat/completions", "Offline model route changed"
        )
        payload = json.loads(request.content)
        _require(
            payload["messages"][1] == {"role": "user", "content": PROOF_TEXT},
            "Offline personal smoke changed the exact input",
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    with TemporaryDirectory(prefix="wecom-personal-smoke-") as directory:
        settings = DesktopSettings(
            dry_run=True,
            inbox_backend="personal",
            personal_contact_name="Ri",
            llm_base_url="https://personal-smoke.invalid/v1",
            llm_api_key="personal-smoke-fake-key",
            llm_model="personal-smoke-fake-model",
            llm_template_name="desktop_reply.txt",
            db_url=f"sqlite:///{Path(directory, 'personal.db')}",
            _env_file=None,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:

            def factory(
                base_url: str,
                api_key: str,
                model: str,
                *,
                template_name: str,
            ) -> HTTPReplyModel:
                """Inject mocked HTTP into the otherwise real configured model adapter."""
                return HTTPReplyModel(
                    base_url,
                    api_key,
                    model,
                    template_name=template_name,
                    http_client=http,
                )

            result = await run_live_personal_smoke(
                settings,
                reader=FakeReader(),
                model_factory=factory,
            )
    _require(http_calls == result.llm_calls == 1, "Offline proof model count changed")
    return result


async def run_realtime_smoke() -> None:
    """Exercise refreshed appends, draft preview and opt-in sending with fake UI."""
    from unittest.mock import AsyncMock

    from adapters.personal_refresh import PersonalRefreshingReader
    from adapters.personal_reply import PersonalDesktopReplyChannel
    from adapters.personal_tail import PersonalTailInbox

    old = PersonalHistoryRow((HISTORY_SENDER,), ("old",), ("10/16 12:11",))
    incoming = PersonalHistoryRow(
        (HISTORY_SENDER,), ("接收测试-1017",), ("10/17 16:30", "10/17")
    )

    class FakeReader:
        """Expose a stable history followed by an explicitly appended message."""

        rows = (old,)

        async def read_active_chat(self) -> PersonalHistorySnapshot:
            """Return only synthetic native-shaped data, never a real UI snapshot."""
            return PersonalHistorySnapshot(PERSONAL_TITLE, True, True, "", self.rows)

    reader = FakeReader()
    refresher = AsyncMock()
    preflight = AsyncMock(return_value="OK\n")
    model = AsyncMock()
    model.reply.return_value = "已收到测试消息。"
    sender = NoSendSender()
    output: list[str] = []
    source = PersonalHistoryInbox(PersonalRefreshingReader(refresher, reader))
    tail = PersonalTailInbox(source, key_of=message_key)
    with TemporaryDirectory(prefix="wecom-realtime-smoke-") as directory:
        store = SQLiteStore(Path(directory, "realtime.db"))
        try:
            for msg in await tail.baseline():
                _require(
                    await store.claim(msg, message_key(msg)), "Baseline claim failed"
                )
                await store.finish(message_key(msg), "baseline")
            loop = AgentLoop(
                tail,
                model,
                sender,
                store,
                allowed_chat=PERSONAL_IDENTITY,
                allowed_sender=PERSONAL_IDENTITY,
                self_senders=frozenset({"desktop:self"}),
                dry_run=True,
                emit=output.append,
                reply_preview=PersonalDesktopReplyChannel(runner=preflight),
            )
            _require((await loop.run_once())["dry_run"] == 0, "Baseline was replied to")
            reader.rows = (old, incoming)
            _require(
                (await loop.run_once())["dry_run"] == 1, "New input was not prepared"
            )
            _require(
                (await loop.run_once())["polled"] == 0, "Unchanged history was replayed"
            )
            model.reply.assert_awaited_once_with("接收测试-1017")
            preflight.assert_awaited_once()
            _require(
                sender.calls == 0 and len(output) == 1, "Unexpected real send or output"
            )
            _require("分钟精度" in output[0], "Original time precision was lost")
            native_send = AsyncMock(return_value="SENT\n")
            live_channel = PersonalDesktopReplyChannel(
                runner=preflight, live_enabled=True, send_runner=native_send
            )
            live_loop = AgentLoop(
                tail,
                model,
                live_channel,
                store,
                allowed_chat=PERSONAL_IDENTITY,
                allowed_sender=PERSONAL_IDENTITY,
                self_senders=frozenset({"desktop:self"}),
                dry_run=False,
            )
            reader.rows = (
                old,
                incoming,
                PersonalHistoryRow(
                    (HISTORY_SENDER,), ("new live input",), ("10/17 16:31",)
                ),
            )
            _require((await live_loop.run_once())["sent"] == 1, "Fake live send failed")
            _require(
                (await live_loop.run_once())["sent"] == 0,
                "Unchanged history caused another fake live send",
            )
            native_send.assert_awaited_once_with("已收到测试消息。")
            _require(
                (await store.records())[-1]["status"] == "sent",
                "Fake live receipt was not persisted",
            )
        finally:
            await store.aclose()
    print("realtime smoke: PASS (new input + preview + fake live send; real sends=0)")


def main(argv: Sequence[str] | None = None) -> int:
    """Run only the fixed real Ri proof and print safe configuration or failure details."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        runner: PersonalSmokeProtocol = run_live_personal_smoke
        asyncio.run(runner(DesktopSettings()))
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(map(str, error["loc"]))
                for error in exc.errors(
                    include_input=False,
                    include_context=False,
                )
            }
        )
        print(
            "Personal smoke configuration is missing or invalid: " + ", ".join(fields)
        )
        return 2
    except PersonalSmokeError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Personal smoke cancelled; real sending remains disabled")
        return 130
    except Exception as exc:  # noqa: BLE001 -- Keep raw credential values out of CLI output.
        print(f"Personal smoke failed ({type(exc).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
