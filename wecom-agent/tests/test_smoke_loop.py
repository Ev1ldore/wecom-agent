"""Verify encrypted smoke callbacks and isolated temporary reply consumers."""

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock
from xml.etree import ElementTree

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from structlog.contextvars import bound_contextvars, get_contextvars
from wechatpy.crypto import WeChatSigner
from wechatpy.enterprise.crypto import WeChatCrypto

from smoke import smoke_loop
from smoke.smoke_api import SecretRedactor
from smoke.smoke_loop import (
    FALLBACK_TEXT,
    ReplyQueueProtocol,
    SmokeReplyWorker,
    build_encrypted_callback,
)
from src.config import Settings
from src.core.models import WecomMessage
from src.core.queue import MessageQueue
from src.gateway.wecom_client import WeComAPIError, WecomClientProtocol


@pytest.fixture
def settings() -> Settings:
    """Create explicit test credentials without loading an environment file."""
    return Settings(
        wecom_corpid="corp-smoke-test",
        wecom_secret="sensitive-smoke-secret",
        wecom_token="sensitive-smoke-token",
        wecom_aes_key="a" * 43,
        wecom_agent_id=1000001,
        _env_file=None,
    )


@dataclass
class FiniteReplyQueue:
    """Yield only predefined messages without Redis or background tasks."""

    messages: list[WecomMessage]
    calls: list[tuple[str, str, int, int]] = field(default_factory=list)

    async def consume(
        self,
        group: str,
        consumer: str,
        batch: int = 10,
        block_ms: int = 5000,
    ) -> AsyncGenerator[WecomMessage, None]:
        """Retain invocation details and end naturally after the last message."""
        self.calls.append((group, consumer, batch, block_ms))
        for message in self.messages:
            yield message


@dataclass
class RecordingTextClient:
    """Observe send operations and optionally raise predefined API failures."""

    failures: dict[str, WeComAPIError] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    message_contexts: list[dict[str, Any]] = field(default_factory=list)

    async def send_text(self, user_id: str, content: str) -> None:
        """Capture context as a real API client would, with no HTTP operations."""
        self.calls.append((user_id, content))
        self.message_contexts.append(get_contextvars())
        if content in self.failures:
            raise self.failures[content]


def make_message(
    msg_id: str,
    *,
    from_user: str = "member-1",
    msg_type: str = "text",
    content: str | None = "hello & 企业微信",
) -> WecomMessage:
    """Build one normalized inbound message for the finite smoke queue."""
    return WecomMessage(
        msg_id=msg_id,
        msg_type=msg_type,
        from_user=from_user,
        chat_type="single",
        content=content,
        raw={"MsgId": msg_id, "FromUserName": from_user, "MsgType": msg_type},
        ts=1700000000,
    )


def structured_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Read the worker's public action fields from real stdlib handlers."""
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            event = json.loads(record.getMessage())
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("action") in {
            "reply_done",
            "reply_failed",
        }:
            events.append(event)
    return events


@pytest.mark.parametrize(
    ("msg_type", "content"),
    [("text", "你好 <企微> & 原文\n第二行"), ("image", None)],
)
def test_encrypted_callback_uses_valid_sdk_ciphertext_and_signature(
    settings: Settings, msg_type: str, content: str | None
) -> None:
    """Independently decrypt ciphertext and verify its official signer digest."""
    query, body = build_encrypted_callback(
        settings,
        "member-1",
        msg_type,
        content,
        "smoke-round-trip-1",
        1700000000,
    )

    assert isinstance(body, bytes)
    assert set(query) == {"msg_signature", "timestamp", "nonce"}
    envelope = ElementTree.fromstring(body)
    assert envelope.tag == "xml"
    assert envelope.findtext("ToUserName") == settings.wecom_corpid
    encrypted = envelope.findtext("Encrypt")
    assert encrypted
    signer = WeChatSigner()
    signer.add_data(settings.wecom_token, query["timestamp"], query["nonce"], encrypted)
    assert signer.signature == query["msg_signature"]

    crypto = WeChatCrypto(
        settings.wecom_token, settings.wecom_aes_key, settings.wecom_corpid
    )
    plaintext = crypto.decrypt_message(
        body, query["msg_signature"], query["timestamp"], query["nonce"]
    )
    message = ElementTree.fromstring(plaintext)
    assert message.findtext("ToUserName") == settings.wecom_corpid
    assert message.findtext("FromUserName") == "member-1"
    assert message.findtext("MsgType") == msg_type
    assert message.findtext("MsgId") == "smoke-round-trip-1"
    assert message.findtext("CreateTime") == "1700000000"
    assert (message.findtext("Content") or None) == content


@pytest.mark.asyncio
async def test_worker_echo_and_image_fallback_keep_message_context(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Echo exact text and send fallback with correlated logs and restored context."""
    queue = FiniteReplyQueue(
        [make_message("echo-1"), make_message("image-1", msg_type="image")]
    )
    client = RecordingTextClient()
    reply_queue: ReplyQueueProtocol = queue
    text_client: WecomClientProtocol = client
    worker = SmokeReplyWorker(
        reply_queue,
        text_client,
        "member-1",
        frozenset({"echo-1", "image-1"}),
        group="smoke-group",
        redactor=SecretRedactor(settings),
    )
    initial_context = get_contextvars()

    with (
        caplog.at_level(logging.INFO),
        bound_contextvars(msg_id="caller-message", trace_id="caller-trace"),
    ):
        await worker.run()
        assert get_contextvars()["msg_id"] == "caller-message"
        assert get_contextvars()["trace_id"] == "caller-trace"

    assert client.calls == [
        ("member-1", "hello & 企业微信"),
        ("member-1", FALLBACK_TEXT),
    ]
    assert FALLBACK_TEXT == "暂不支持该消息类型，请发送文本"
    assert [context["msg_id"] for context in client.message_contexts] == [
        "echo-1",
        "image-1",
    ]
    assert all(
        context["trace_id"] == "caller-trace" for context in client.message_contexts
    )
    assert get_contextvars() == initial_context
    assert len(queue.calls) == 1
    group, consumer, batch, block_ms = queue.calls[0]
    assert group == "smoke-group"
    assert consumer
    assert batch > 0
    assert block_ms >= 0
    done = [
        event for event in structured_events(caplog) if event["action"] == "reply_done"
    ]
    assert [(event["msg_id"], event["kind"]) for event in done] == [
        ("echo-1", "echo"),
        ("image-1", "fallback"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["voice", "file", "event"])
async def test_worker_sends_fallback_for_other_nontext_messages(
    settings: Settings, msg_type: str
) -> None:
    """Preserve the same unsupported-type response for every nontext kind."""
    queue = FiniteReplyQueue([make_message("nontext-1", msg_type=msg_type)])
    client = RecordingTextClient()
    worker = SmokeReplyWorker(
        queue,
        client,
        "member-1",
        frozenset({"nontext-1"}),
        group="smoke-group",
        redactor=SecretRedactor(settings),
    )

    await worker.run()

    assert client.calls == [("member-1", FALLBACK_TEXT)]


@pytest.mark.asyncio
async def test_worker_ignores_messages_outside_user_and_id_allowlists(
    settings: Settings,
) -> None:
    """Require both an expected smoke ID and the explicitly selected member."""
    queue = FiniteReplyQueue(
        [
            make_message("allowed-1", from_user="different-member"),
            make_message("unrelated-1", from_user="member-1"),
            make_message("unrelated-2", from_user="different-member"),
            make_message("allowed-1", content="allowed exact echo"),
        ]
    )
    client = RecordingTextClient()
    worker = SmokeReplyWorker(
        queue,
        client,
        "member-1",
        frozenset({"allowed-1"}),
        group="smoke-group",
        redactor=SecretRedactor(settings),
    )

    await worker.run()

    assert client.calls == [("member-1", "allowed exact echo")]
    assert client.message_contexts[0]["msg_id"] == "allowed-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("errcode", [60020, 81013, 40001])
async def test_worker_preserves_configuration_errors_redacts_and_continues(
    settings: Settings, caplog: pytest.LogCaptureFixture, errcode: int
) -> None:
    """Log configuration errors unchanged and continue without bypassing the API."""
    sensitive_error = (
        f"configured secret={settings.wecom_secret}; token={settings.wecom_token}; "
        f"aes={settings.wecom_aes_key}"
    )
    queue = FiniteReplyQueue(
        [
            make_message("failed-1", content="will-fail"),
            make_message("next-1", content="next exact echo"),
        ]
    )
    client = RecordingTextClient(
        failures={"will-fail": WeComAPIError(errcode, sensitive_error)}
    )
    worker = SmokeReplyWorker(
        queue,
        client,
        "member-1",
        frozenset({"failed-1", "next-1"}),
        group="smoke-group",
        redactor=SecretRedactor(settings),
    )
    initial_context = get_contextvars()

    with caplog.at_level(logging.INFO):
        await worker.run()

    assert client.calls == [
        ("member-1", "will-fail"),
        ("member-1", "next exact echo"),
    ]
    assert [context["msg_id"] for context in client.message_contexts] == [
        "failed-1",
        "next-1",
    ]
    assert get_contextvars() == initial_context
    events = structured_events(caplog)
    failed = [event for event in events if event.get("action") == "reply_failed"]
    assert len(failed) == 1
    assert failed[0]["msg_id"] == "failed-1"
    assert failed[0]["errcode"] == errcode
    assert "[REDACTED]" in failed[0]["errmsg"]
    assert settings.wecom_secret not in caplog.text
    assert settings.wecom_token not in caplog.text
    assert settings.wecom_aes_key not in caplog.text
    done = [event for event in events if event.get("action") == "reply_done"]
    assert [(event["msg_id"], event["kind"]) for event in done] == [("next-1", "echo")]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_history", [False, True])
async def test_live_lifespan_creates_group_at_tail_before_accepting_new_messages(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    has_history: bool,
) -> None:
    """Skip existing entries and consume post-start messages through the real queue."""
    redis_client = FakeAsyncRedis(decode_responses=False)
    monkeypatch.setattr(
        "src.core.queue.redis.from_url", lambda *args, **kwargs: redis_client
    )
    queue = MessageQueue(settings.redis_url, settings.queue_key)
    delivered = asyncio.Event()

    class SignalingTextClient(RecordingTextClient):
        """Signal completed sends while retaining the worker's observable behavior."""

        async def send_text(self, user_id: str, content: str) -> None:
            """Record delivery and wake the test without network access or sleeps."""
            await super().send_text(user_id, content)
            delivered.set()

    client = SignalingTextClient()

    @asynccontextmanager
    async def fake_service_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Install isolated Redis clients without loading settings from disk."""
        app.state.queue = queue
        app.state.callback_redis = redis_client
        yield

    @asynccontextmanager
    async def fake_client(
        *args: Any, **kwargs: Any
    ) -> AsyncGenerator[RecordingTextClient, None]:
        """Replace only outbound sends while exercising actual Stream consumption."""
        yield client

    monkeypatch.setattr(smoke_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(smoke_loop, "service_lifespan", fake_service_lifespan)
    monkeypatch.setattr(smoke_loop, "WecomClient", fake_client)
    monkeypatch.setenv("WECOM_SMOKE_USER_ID", "member-1")
    monkeypatch.setenv("WECOM_SMOKE_MSG_IDS", json.dumps(["historical-1", "new-1"]))
    app = FastAPI()

    try:
        if has_history:
            await queue.publish(make_message("historical-1", content="old message"))
            historical_id = (await redis_client.xrange(settings.queue_key))[0][0]
        else:
            historical_id = b"0-0"

        async with smoke_loop._live_lifespan(app):
            groups = await redis_client.xinfo_groups(settings.queue_key)
            assert len(groups) == 1
            assert groups[0]["name"].startswith(b"wecom-smoke-")
            assert groups[0]["last-delivered-id"] == historical_id
            assert groups[0]["pending"] == 0

            await queue.publish(make_message("new-1", content="new message"))
            async with asyncio.timeout(2):
                await delivered.wait()
            assert client.calls == [("member-1", "new message")]
            assert [context["msg_id"] for context in client.message_contexts] == [
                "new-1"
            ]

        assert await redis_client.xinfo_groups(settings.queue_key) == []
    finally:
        await queue.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_case", ["already_exited", "terminate_race", "kill_race"]
)
async def test_server_cleanup_preserves_original_failure_after_process_exit(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_case: str,
) -> None:
    """Do not replace startup/health failures with a signal-to-exited-process error."""
    redis_client = FakeAsyncRedis(decode_responses=False)
    monkeypatch.setattr(
        smoke_loop.redis, "from_url", lambda *args, **kwargs: redis_client
    )
    listener = MagicMock()
    listener.__enter__.return_value = listener
    monkeypatch.setattr(smoke_loop.socket, "socket", lambda *args, **kwargs: listener)
    process = Mock(returncode=1 if cleanup_case == "already_exited" else None)
    process.wait = AsyncMock(return_value=1)

    def exit_during_signal() -> None:
        """Model the child exiting between returncode inspection and the signal."""
        process.returncode = 1
        raise ProcessLookupError("child already exited")

    if cleanup_case == "terminate_race":
        process.terminate.side_effect = exit_during_signal
    elif cleanup_case == "kill_race":
        process.wait.side_effect = [TimeoutError(), 1]
        process.kill.side_effect = exit_during_signal
    start_server = AsyncMock(return_value=process)
    monkeypatch.setattr(smoke_loop, "_start_server", start_server)

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail an in-memory health check without a socket or real HTTP request."""
        raise smoke_loop.SmokeLoopError("health", "original health failure")

    http = httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(smoke_loop.httpx, "AsyncClient", lambda **kwargs: http)
    try:
        with pytest.raises(smoke_loop.SmokeLoopError) as caught:
            await smoke_loop.run_live_loop(settings, "member-1")
    finally:
        await http.aclose()
        await redis_client.aclose()

    start_server.assert_awaited_once()
    if cleanup_case == "already_exited":
        assert caught.value.stage == "startup"
        assert caught.value.errmsg == "Uvicorn exited before health check"
        process.terminate.assert_not_called()
    else:
        assert caught.value.stage == "health"
        assert caught.value.errmsg == "original health failure"
        process.terminate.assert_called_once_with()
    if cleanup_case == "kill_race":
        process.kill.assert_called_once_with()
        assert process.wait.await_count == 2
    else:
        process.kill.assert_not_called()
        process.wait.assert_awaited_once()


@pytest.mark.parametrize("invalid_key", ["short-private-aes", "!" * 43, "a" * 42])
def test_invalid_aes_key_becomes_safe_encrypt_configuration_error(
    settings: Settings, invalid_key: str
) -> None:
    """Convert actual SDK key-validation failures without retaining raw exceptions."""
    invalid_settings = settings.model_copy(update={"wecom_aes_key": invalid_key})

    with pytest.raises(smoke_loop.SmokeLoopError) as caught:
        build_encrypted_callback(
            invalid_settings, "member-1", "text", "hello", "invalid-key-1", 1700000000
        )

    assert caught.value.stage == "encrypt"
    assert "EncodingAESKey" in caught.value.errmsg
    assert "43" in caught.value.errmsg
    assert "32" in caught.value.errmsg
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert invalid_key not in rendered
    assert settings.wecom_secret not in rendered


@pytest.mark.parametrize("error_type", [AssertionError, ValueError, TypeError])
@pytest.mark.parametrize("error_stage", ["construct", "encrypt"])
def test_sdk_key_failures_are_sanitized_at_construction_and_encryption(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    error_stage: str,
) -> None:
    """Never expose exception messages supplied by the SDK's key-dependent operations."""
    secret_exception_message = f"bad SDK key={settings.wecom_aes_key}"
    sdk = Mock()
    if error_stage == "construct":
        sdk.side_effect = error_type(secret_exception_message)
    else:
        sdk.return_value.encrypt_message.side_effect = error_type(
            secret_exception_message
        )
    monkeypatch.setattr(smoke_loop, "WeChatCrypto", sdk)

    with pytest.raises(smoke_loop.SmokeLoopError) as caught:
        build_encrypted_callback(
            settings, "member-1", "text", "hello", "invalid-key-1", 1700000000
        )

    assert caught.value.stage == "encrypt"
    assert "43" in caught.value.errmsg and "32" in caught.value.errmsg
    assert secret_exception_message not in str(caught.value)
    assert settings.wecom_aes_key not in str(caught.value)
    assert caught.value.__context__ is None


def test_cli_reports_invalid_aes_configuration_without_key_or_traceback(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise CLI handling of a real SDK failure without Redis or external I/O."""
    invalid_key = "private-invalid-sdk-key"
    invalid_settings = settings.model_copy(update={"wecom_aes_key": invalid_key})
    monkeypatch.setattr(smoke_loop, "get_settings", lambda: invalid_settings)

    async def build_only(settings: Settings, user_id: str) -> None:
        """Stop at SDK validation instead of starting the live smoke runner."""
        build_encrypted_callback(
            settings, user_id, "text", "hello", "invalid-key-1", 1700000000
        )

    monkeypatch.setattr(smoke_loop, "run_live_loop", build_only)
    assert smoke_loop.main(["--user-id", "member-1"]) == 1

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "stage=encrypt" in output
    assert "EncodingAESKey" in output
    assert "43" in output and "32" in output
    assert "Traceback" not in output
    assert invalid_key not in output
    assert settings.wecom_secret not in output


@pytest.mark.parametrize("error_type", [AssertionError, RuntimeError, OSError])
def test_cli_unexpected_errors_print_only_type(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    """The final CLI safety net must not print secret-bearing error messages."""
    monkeypatch.setattr(smoke_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(
        smoke_loop,
        "run_live_loop",
        AsyncMock(
            side_effect=error_type(f"private SDK input: {settings.wecom_aes_key}")
        ),
    )

    assert smoke_loop.main(["--user-id", "member-1"]) == 1

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert type(error_type()).__name__ in output
    assert "Traceback" not in output
    assert "private SDK input" not in output
    assert settings.wecom_aes_key not in output
