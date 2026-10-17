"""Verify runtime guards and local execution without private messages or sends."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agent_loop
from adapters.inbox import InboxError, Msg
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["live", "unverified"])
async def test_runtime_refuses_unsafe_setup_before_auth_or_llm(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Missing identity verification and DRY_RUN=false cannot reach any service."""
    auth = AsyncMock()
    monkeypatch.setattr(agent_loop, "_require_cli_authorized", auth)
    settings = DesktopSettings(
        _env_file=None,
        dry_run=mode != "live",
        inbox_chat_id="raw-chat",
        inbox_sender_id="raw-user",
    )
    with pytest.raises(agent_loop.SetupError):
        await agent_loop.run_cli(settings, once=True)
    auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_cli_runtime_records_reply_without_sender_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mapped CLI inbox reaches the model and SQLite while live sends stay off."""
    from adapters import wecom_cli

    settings = DesktopSettings(
        _env_file=None,
        inbox_chat_id="verified-raw-chat",
        inbox_sender_id="verified-raw-user",
        inbox_verified_single_chat=True,
        db_url=f"sqlite:///{tmp_path / 'test.db'}",
        llm_api_key="test-model-key",
    )
    message = Msg(
        chat=settings.allowed_chat, sender="LiChengYun", content="hello", ts=1700000000
    )
    model_calls: list[str] = []
    inbox_options: dict[str, Any] = {}

    class FakeInbox:
        """Keep exactly the public production constructor and receive contract."""

        def __init__(self, **kwargs: Any) -> None:
            """Retain mapping configuration for assertions without network access."""
            inbox_options.update(kwargs)

        async def poll_unread(self) -> list[Msg]:
            """Return one safe test message."""
            return [message]

    class FakeModel:
        """Implement only the reply operation used inside the runtime context."""

        async def reply(self, content: str) -> str:
            """Observe one local input and return a fixed test reply."""
            model_calls.append(content)
            return "mock reply"

    @asynccontextmanager
    async def fake_model(*args: Any, **kwargs: Any):
        """Avoid every HTTP operation, including client creation."""
        yield FakeModel()

    monkeypatch.setattr(agent_loop, "_require_cli_authorized", AsyncMock())
    monkeypatch.setattr(agent_loop, "_first_window_ready", AsyncMock())
    monkeypatch.setattr(wecom_cli, "WecomCLIInbox", FakeInbox)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", fake_model)
    monkeypatch.setattr(
        agent_loop.DisabledSender,
        "send_text",
        AsyncMock(side_effect=AssertionError("must not send")),
    )
    assert await agent_loop.run_cli(settings, once=True) == 0
    agent_loop._first_window_ready.assert_awaited_once()
    assert model_calls == ["hello"]
    assert inbox_options["chat_id"] == "verified-raw-chat"
    assert inbox_options["normalized_chat"] == settings.allowed_chat
    assert inbox_options["verified_single_chat"] is True
    agent_loop.DisabledSender.send_text.assert_not_awaited()
    store = SQLiteStore(settings.sqlite_path)
    try:
        rows = await store.records()
        assert [(row["status"], row["reply"]) for row in rows] == [
            ("dry_run", "mock reply")
        ]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_inbox_failure_reports_safe_diagnostic_without_raw_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A one-shot polling failure must not leak service diagnostics or traceback."""
    loop = AsyncMock()
    loop.run_once.side_effect = InboxError("dummy-private-transport-details")
    assert await agent_loop.run_cycles(loop, once=True, interval=2) == 1
    assert "dummy-private-transport-details" not in capsys.readouterr().out


def test_main_reports_validation_fields_without_secret_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed environment value must not be printed in validation output."""
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "private-invalid-setting")
    assert agent_loop.main(["--once"]) == 2
    output = capsys.readouterr().out
    assert "poll_interval_seconds" in output
    assert "private-invalid-setting" not in output


def test_main_generic_failure_never_prints_raw_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unexpected credential-bearing exceptions are reduced to their type."""
    monkeypatch.setattr(
        agent_loop,
        "DesktopSettings",
        lambda: (_ for _ in ()).throw(RuntimeError("dummy-private-key")),
    )
    assert agent_loop.main([]) == 1
    output = capsys.readouterr().out
    assert "RuntimeError" in output and "dummy-private-key" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["authorized", "unauthorized", "unexpected"])
async def test_cli_auth_precheck_is_read_only_and_suppresses_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Never authenticate, send or expose raw CLI stderr during authorization checks."""
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (status.encode(), b"dummy-private-stderr")
    spawn = AsyncMock(return_value=process)
    cleanup = AsyncMock()
    monkeypatch.setattr(agent_loop.shutil, "which", lambda name: "/verified/wecom-cli")
    monkeypatch.setattr(agent_loop.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(agent_loop, "stop_cli_process", cleanup)
    monkeypatch.setenv("WECOM_CLI_LOG_DIR", "/must-not-write-logs")
    if status == "authorized":
        await agent_loop._require_cli_authorized()
        assert spawn.await_count == 1
    else:
        with pytest.raises(agent_loop.SetupError) as error:
            await agent_loop._require_cli_authorized()
        assert "dummy-private-stderr" not in str(error.value)
        assert spawn.await_count == (1 if status == "unauthorized" else 2)
    for call in spawn.await_args_list:
        assert call.args == ("/verified/wecom-cli", "auth", "show", "--status")
        assert "WECOM_CLI_LOG_DIR" not in call.kwargs["env"]
        assert call.kwargs["env"]["WECOM_CLI_LOG_LEVEL"] == "warn"
        assert call.kwargs["start_new_session"] is agent_loop.IS_POSIX
    assert cleanup.await_count == spawn.await_count
