"""Verify proof-specific CLI dry-runs with fake inboxes and mocked model HTTP."""

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.inbox import Inbox, Msg
from smoke import smoke_local_cli as smoke
from src.core.desktop_llm import HTTPReplyModel
from src.core.desktop_loop import message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings

MESSAGE_TS = 1700000001
REPLY = "完整模型回复第一行\n完整模型回复第二行，不应打印到冒烟日志。"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every actual socket or HTTP transport attempt fail before transmission."""

    async def no_http(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        raise AssertionError("Unexpected real HTTP operation")

    def no_socket(sock: socket.socket, address: Any) -> None:
        raise AssertionError("Unexpected real socket operation")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)
    monkeypatch.setattr(socket.socket, "connect", no_socket)


@pytest.fixture
def settings(tmp_path: Any) -> DesktopSettings:
    """Use explicit test configuration and a unique real local SQLite file."""
    return DesktopSettings(
        dry_run=True,
        inbox_chat_id="verified-test-chat",
        inbox_sender_id="verified-test-ciphertext-user",
        inbox_verified_single_chat=True,
        wecom_agent_id=1000002,
        allowed_user_id="LiChengYun",
        llm_base_url="https://proof-model.invalid/v1",
        llm_api_key="private-test-model-key",
        llm_model="test-model",
        llm_template_name="desktop_reply.txt",
        db_url=f"sqlite:///{tmp_path / 'proof.db'}",
        _env_file=None,
    )


def proof_message(settings: DesktopSettings, **changes: Any) -> Msg:
    """Construct only synthetic normalized proof data with optional variations."""
    return Msg(
        **{
            "chat": settings.allowed_chat,
            "sender": settings.allowed_user_id,
            "content": smoke.PROOF_TEXT,
            "ts": MESSAGE_TS,
            **changes,
        }
    )


def fake_inboxes(
    windows: list[list[Msg]],
) -> tuple[Callable[..., Inbox], list[dict[str, Any]]]:
    """Create independent inbox instances and capture their exact source mappings."""
    constructions: list[dict[str, Any]] = []

    class FakeInbox:
        """Expose a fixed source window without launching a CLI process."""

        def __init__(self, messages: list[Msg]) -> None:
            self.messages = messages

        async def poll_unread(self) -> list[Msg]:
            return list(self.messages)

    def factory(**kwargs: Any) -> Inbox:
        index = len(constructions)
        constructions.append(kwargs)
        return FakeInbox(windows[index])

    return factory, constructions


def forbidden_model(*args: Any, **kwargs: Any) -> HTTPReplyModel:
    """Fail if a rejected or already-completed proof tries to construct a model."""
    raise AssertionError("Model must not be constructed for this proof")


def model_factory(
    http: httpx.AsyncClient, constructions: list[dict[str, Any]]
) -> Callable[..., HTTPReplyModel]:
    """Retain the real model adapter while replacing its HTTP network boundary."""

    def factory(
        base_url: str, api_key: str, model: str, *, template_name: str
    ) -> HTTPReplyModel:
        constructions.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "template_name": template_name,
            }
        )
        return HTTPReplyModel(
            base_url, api_key, model, template_name=template_name, http_client=http
        )

    return factory


async def seed_claim(
    settings: DesktopSettings,
    message: Msg,
    status: str,
    reply: str = REPLY,
) -> None:
    """Persist a real pre-existing outcome without removing or altering its key."""
    store = SQLiteStore(settings.sqlite_path)
    try:
        key = message_key(message, settings.dedup_bucket_seconds)
        assert await store.claim(message, key)
        if status != "claimed":
            await store.finish(key, status, reply=reply)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_fresh_proof_calls_real_model_adapter_once_then_replays_without_llm(
    settings: DesktopSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Filter exact text/time, preserve the reply and require a fresh adapter replay."""
    proof = proof_message(settings)
    other = proof_message(settings, content="unrelated private test message")
    wrong_time = proof_message(settings, ts=MESSAGE_TS + 1)
    factory, sources = fake_inboxes([[other, wrong_time, proof], [proof, other]])
    requests: list[httpx.Request] = []
    models: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLY}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=model_factory(http, models),
        )

    assert result.already_completed is False
    assert result.llm_calls == 1 and result.reply_saved is True
    assert result.first_stats["dry_run"] == 1
    assert result.first_stats["sent"] == 0
    assert result.replay_stats["duplicate"] == 1
    assert result.replay_stats["dry_run"] == result.replay_stats["sent"] == 0
    assert len(requests) == len(models) == 1
    assert json.loads(requests[0].content)["messages"][1]["content"] == smoke.PROOF_TEXT
    assert models[0] == {
        "base_url": settings.llm_base_url,
        "api_key": settings.llm_api_key.get_secret_value(),
        "model": settings.llm_model,
        "template_name": settings.llm_template_name,
    }
    assert len(sources) == 2
    assert (
        sources[0]
        == sources[1]
        == {
            "chat_id": settings.inbox_chat_id,
            "sender_id": settings.inbox_sender_id,
            "normalized_chat": settings.allowed_chat,
            "normalized_sender": settings.allowed_user_id,
            "verified_single_chat": True,
            "timezone_name": settings.inbox_timezone,
            "start_at": MESSAGE_TS - 1,
        }
    )
    store = SQLiteStore(settings.sqlite_path)
    try:
        rows = await store.records()
        assert len(rows) == 1
        assert rows[0]["reply"] == REPLY
        assert rows[0]["status"] == "dry_run"
        assert all(rows[0][key] == value for key, value in proof.model_dump().items())
    finally:
        await store.aclose()
    output = capsys.readouterr().out
    assert "new dry_run=1" in output and "replay duplicate=1" in output
    assert "real sends=0" in output
    for private in (
        REPLY,
        other.content,
        settings.llm_api_key.get_secret_value(),
        settings.llm_base_url,
    ):
        assert private not in output


@pytest.mark.asyncio
async def test_existing_completed_proof_reports_zero_fresh_model_operations(
    settings: DesktopSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """A previous dry-run is verified through dedup without creating a model client."""
    proof = proof_message(settings)
    await seed_claim(settings, proof, "dry_run")
    factory, sources = fake_inboxes([[proof], [proof]])

    result = await smoke.run_local_cli_smoke(
        settings,
        message_ts=MESSAGE_TS,
        inbox_factory=factory,
        model_factory=forbidden_model,
    )

    assert result.already_completed is True
    assert result.llm_calls == 0
    assert result.first_stats["duplicate"] == result.replay_stats["duplicate"] == 1
    assert result.first_stats["dry_run"] == result.replay_stats["dry_run"] == 0
    assert len(sources) == 2
    output = capsys.readouterr().out
    assert "prior dry_run" in output and "fresh LLM calls=0" in output
    assert "new dry_run=1" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["claimed", "llm_failed", "send_failed", "emit_failed", "sent"]
)
async def test_existing_incomplete_or_failed_claim_is_never_bypassed(
    settings: DesktopSettings, status: str
) -> None:
    """Keep every pre-existing claim intact and require manual inspection."""
    proof = proof_message(settings)
    await seed_claim(settings, proof, status)
    factory, sources = fake_inboxes([[proof]])

    with pytest.raises(smoke.LocalCLISmokeError, match=f"status={status}"):
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=forbidden_model,
        )

    assert len(sources) == 1
    store = SQLiteStore(settings.sqlite_path)
    try:
        rows = await store.records()
        assert len(rows) == 1 and rows[0]["status"] == status
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages", [[], ["wrong-text"], ["wrong-time"], ["duplicate", "duplicate"]]
)
async def test_missing_and_ambiguous_proofs_fail_before_model_or_claim(
    settings: DesktopSettings, messages: list[str]
) -> None:
    """Require exactly one match without selecting an arbitrary duplicate input."""
    proof = proof_message(settings)
    windows = {
        "wrong-text": proof_message(settings, content="different text"),
        "wrong-time": proof_message(settings, ts=MESSAGE_TS + 1),
        "duplicate": proof,
    }
    factory, _ = fake_inboxes([[windows[name] for name in messages]])
    expected = "Multiple exact proof" if len(messages) == 2 else "not found"

    with pytest.raises(smoke.LocalCLISmokeError, match=expected):
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=forbidden_model,
        )

    assert not settings.sqlite_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"dry_run": False},
        {"inbox_verified_single_chat": False},
        {"inbox_chat_id": ""},
        {"inbox_sender_id": ""},
    ],
)
async def test_unverified_or_send_enabled_configuration_cannot_touch_source(
    settings: DesktopSettings, changes: dict[str, Any]
) -> None:
    """Reject unsafe configuration before opening the inbox, SQLite or model."""
    factory, sources = fake_inboxes([])

    with pytest.raises(smoke.LocalCLISmokeError):
        await smoke.run_local_cli_smoke(
            settings.model_copy(update=changes),
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=forbidden_model,
        )

    assert sources == []
    assert not settings.sqlite_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"chat": "other-chat"},
        {"sender": "other-user"},
        {"is_self": True},
        {"chat_type": "group"},
    ],
)
async def test_exact_text_and_time_do_not_override_identity_filter(
    settings: DesktopSettings, changes: dict[str, Any]
) -> None:
    """Keep authorization mandatory even when proof text and timestamp match."""
    factory, _ = fake_inboxes([[proof_message(settings, **changes)]])

    with pytest.raises(
        smoke.LocalCLISmokeError, match="verified single-chat/user mapping"
    ):
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=forbidden_model,
        )

    assert not settings.sqlite_path.exists()


@pytest.mark.asyncio
async def test_dedup_collision_with_other_input_is_not_called_completed(
    settings: DesktopSettings,
) -> None:
    """A same-bucket message with a different exact timestamp is not this proof."""
    previous = proof_message(settings, ts=MESSAGE_TS + 1)
    proof = proof_message(settings)
    assert message_key(previous) == message_key(proof)
    await seed_claim(settings, previous, "dry_run")
    factory, _ = fake_inboxes([[proof]])

    with pytest.raises(smoke.LocalCLISmokeError, match="different stored input"):
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=factory,
            model_factory=forbidden_model,
        )


@pytest.mark.asyncio
async def test_model_failure_is_persisted_and_fails_without_source_replay(
    settings: DesktopSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use the model's bounded HTTP retries, then retain its failed claim."""
    proof = proof_message(settings)
    factory, sources = fake_inboxes([[proof]])
    requests: list[httpx.Request] = []

    def failed(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, text="private provider response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as http:
        with pytest.raises(smoke.LocalCLISmokeError, match="status=llm_failed"):
            await smoke.run_local_cli_smoke(
                settings,
                message_ts=MESSAGE_TS,
                inbox_factory=factory,
                model_factory=model_factory(http, []),
            )

    assert len(requests) == 2 and len(sources) == 1
    store = SQLiteStore(settings.sqlite_path)
    try:
        assert (await store.records())[0]["status"] == "llm_failed"
    finally:
        await store.aclose()
    assert "PASS" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_source_failure_has_safe_exception_without_raw_context(
    settings: DesktopSettings,
) -> None:
    """Do not echo raw source exceptions, URLs or credentials to callers."""

    class FailedInbox:
        async def poll_unread(self) -> list[Msg]:
            raise RuntimeError(
                "private API key https://private.invalid/private-response"
            )

    with pytest.raises(smoke.LocalCLISmokeError) as caught:
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=lambda **kwargs: FailedInbox(),
            model_factory=forbidden_model,
        )

    assert str(caught.value) == "Local CLI proof verification failed (RuntimeError)"
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_cancelled_source_propagates_without_creating_claim(
    settings: DesktopSettings,
) -> None:
    """Keep task cancellation distinct from a retry or completed proof."""

    class CancelledInbox:
        async def poll_unread(self) -> list[Msg]:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await smoke.run_local_cli_smoke(
            settings,
            message_ts=MESSAGE_TS,
            inbox_factory=lambda **kwargs: CancelledInbox(),
            model_factory=forbidden_model,
        )
    assert not settings.sqlite_path.exists()


def test_main_parses_exact_timestamp_without_reading_real_configuration(
    settings: DesktopSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise CLI dispatch with only explicit test settings and a mocked operation."""
    runner = AsyncMock()
    monkeypatch.setattr(smoke, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(smoke, "run_local_cli_smoke", runner)

    assert smoke.main(["--message-ts", str(MESSAGE_TS)]) == 0

    runner.assert_awaited_once_with(settings, message_ts=MESSAGE_TS)


def test_main_proof_failure_returns_nonzero_without_private_diagnostics(
    settings: DesktopSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Do not turn a failed smoke into CLI success or print an arbitrary exception."""
    runner = AsyncMock(
        side_effect=RuntimeError("private credential https://private.invalid")
    )
    monkeypatch.setattr(smoke, "DesktopSettings", lambda: settings)
    monkeypatch.setattr(smoke, "run_local_cli_smoke", runner)

    assert smoke.main(["--message-ts", str(MESSAGE_TS)]) == 1

    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "private" not in output and "https://" not in output
