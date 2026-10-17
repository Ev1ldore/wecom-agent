"""Prove the personal dry-run pipeline with synthetic AX rows and mocked HTTP."""

import asyncio
import json
import socket
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.inbox import Msg
from adapters.wecom_personal import (
    HISTORY_SENDER,
    PERSONAL_IDENTITY,
    PERSONAL_TITLE,
    PersonalHistoryRow,
    PersonalHistorySnapshot,
)
from smoke import smoke_personal as smoke
from src.core.desktop_llm import HTTPReplyModel
from src.core.desktop_loop import message_key
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings

REPLY = "完整回复第一行\n完整回复第二行，仅在本地数据库保存。"


@pytest.mark.asyncio
async def test_realtime_smoke_refreshes_new_input_and_preflights_without_send(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cover the complete new-message chain using only fakes and local SQLite."""
    await smoke.run_realtime_smoke()
    assert "realtime smoke: PASS" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject native subprocesses, real HTTP transports and real socket connects."""

    async def no_http(
        transport: httpx.AsyncHTTPTransport,
        request: httpx.Request,
    ) -> httpx.Response:
        raise AssertionError("Unexpected real HTTP")

    async def no_native(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Unexpected native subprocess")

    def no_socket(sock: socket.socket, address: Any) -> None:
        raise AssertionError("Unexpected real socket")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr("asyncio.create_subprocess_exec", no_native)


@pytest.fixture
def settings(tmp_path: Path) -> DesktopSettings:
    """Construct explicit fake credentials and a disposable SQLite location."""
    return DesktopSettings(
        dry_run=True,
        inbox_backend="personal",
        personal_contact_name="Ri",
        llm_base_url="https://personal-proof.invalid/v1",
        llm_api_key="private-personal-test-key",
        llm_model="personal-test-model",
        db_url=f"sqlite:///{tmp_path / 'personal.db'}",
        _env_file=None,
    )


def proof_row(**changes: Any) -> PersonalHistoryRow:
    """Build the exact authorized proof without reading actual chat history."""
    return PersonalHistoryRow(
        **{
            "senders": (HISTORY_SENDER,),
            "texts": ("你好",),
            "labels": ("10/16 12:11",),
            **changes,
        }
    )


class FakeReader:
    """Supply a fresh fixed personal-history snapshot for every explicit read."""

    def __init__(self, rows: tuple[PersonalHistoryRow, ...]) -> None:
        self.rows = rows
        self.calls = 0

    async def read_active_chat(self) -> PersonalHistorySnapshot:
        """Return synthetic verified Ri metadata and count independent replays."""
        self.calls += 1
        return PersonalHistorySnapshot(
            title=PERSONAL_TITLE,
            personal_verified=True,
            history_open=True,
            search_text="",
            rows=self.rows,
        )


def forbidden_model(*args: Any, **kwargs: Any) -> HTTPReplyModel:
    """Make rejected or already completed proofs incapable of generating text."""
    raise AssertionError("Model construction forbidden")


def proof_message() -> Msg:
    """Represent the known original minute label without an epoch timestamp."""
    return Msg(
        chat=PERSONAL_IDENTITY,
        sender=PERSONAL_IDENTITY,
        content="你好",
        ts=None,
        time_precision="minute",
        time_display="10/16 12:11",
    )


@pytest.mark.asyncio
async def test_exact_personal_proof_calls_mocked_model_once_then_reopens_database(
    settings: DesktopSettings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the known input enters the real model adapter and durable dry-run loop."""
    reader = FakeReader(
        (
            PersonalHistoryRow((), ("系统问候语",), ()),
            proof_row(texts=("其他私信不应进入模型",)),
            proof_row(labels=("10/16 12:12",)),
            proof_row(),
        )
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLY}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:

        def factory(
            base_url: str,
            api_key: str,
            model: str,
            *,
            template_name: str,
        ) -> HTTPReplyModel:
            assert template_name == settings.personal_llm_template_name
            return HTTPReplyModel(
                base_url, api_key, model, template_name=template_name, http_client=http
            )

        result = await smoke.run_live_personal_smoke(
            settings,
            reader=reader,
            model_factory=factory,
        )

    assert result.already_completed is False
    assert result.llm_calls == 1 and result.sender_calls == 0
    assert result.first_stats["dry_run"] == 1
    assert result.replay_stats["duplicate"] == 1
    assert result.replay_stats["dry_run"] == result.replay_stats["sent"] == 0
    assert result.ts is None and result.time_precision == "minute"
    assert result.time_display == "10/16 12:11"
    assert len(requests) == 1 and reader.calls == 2
    payload = json.loads(requests[0].content)
    assert payload["messages"][0] == {
        "role": "system",
        "content": files("prompts").joinpath("personal_reply.txt").read_text().strip(),
    }
    assert payload["messages"][1] == {"role": "user", "content": "你好"}
    assert requests[0].headers["Authorization"] == "Bearer private-personal-test-key"
    store = SQLiteStore(settings.sqlite_path)
    try:
        rows = await store.records()
        assert len(rows) == 1
        assert all(
            rows[0][key] == value for key, value in proof_message().model_dump().items()
        )
        assert rows[0]["status"] == "dry_run" and rows[0]["reply"] == REPLY
    finally:
        await store.aclose()
    output = capsys.readouterr().out
    assert "ts=NULL" in output and "precision=minute" in output
    assert "10/16 12:11" in output and "real sends=0" in output
    assert REPLY not in output and "其他私信" not in output
    assert settings.llm_api_key.get_secret_value() not in output
    assert settings.llm_base_url not in output


@pytest.mark.asyncio
async def test_existing_completed_personal_proof_never_constructs_a_model(
    settings: DesktopSettings,
) -> None:
    """A durable prior dry-run remains deduplicated without generating a reply."""
    store = SQLiteStore(settings.sqlite_path)
    try:
        key = message_key(proof_message(), settings.dedup_bucket_seconds)
        assert await store.claim(proof_message(), key)
        await store.finish(key, "dry_run", reply=REPLY)
    finally:
        await store.aclose()
    result = await smoke.run_live_personal_smoke(
        settings,
        reader=FakeReader((proof_row(),)),
        model_factory=forbidden_model,
    )
    assert result.already_completed is True
    assert result.llm_calls == result.sender_calls == 0
    assert result.first_stats["duplicate"] == result.replay_stats["duplicate"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows", [(), (proof_row(), proof_row()), (proof_row(texts=("系统问候",)),)]
)
async def test_missing_or_ambiguous_proof_is_rejected_before_model_or_claim(
    settings: DesktopSettings,
    rows: tuple[PersonalHistoryRow, ...],
) -> None:
    """Never silently substitute a neighboring message or ambiguous duplicate."""
    with pytest.raises(smoke.PersonalSmokeError):
        await smoke.run_live_personal_smoke(
            settings,
            reader=FakeReader(rows),
            model_factory=forbidden_model,
        )
    assert not settings.sqlite_path.exists()


@pytest.mark.asyncio
async def test_false_dry_run_is_rejected_before_reading_history(
    settings: DesktopSettings,
) -> None:
    """Explicitly reject configuration capable of enabling a send branch."""
    reader = FakeReader((proof_row(),))
    with pytest.raises(smoke.PersonalSmokeError, match="DRY_RUN=true"):
        await smoke.run_live_personal_smoke(
            settings.model_copy(update={"dry_run": False}),
            reader=reader,
            model_factory=forbidden_model,
        )
    assert reader.calls == 0 and not settings.sqlite_path.exists()


@pytest.mark.asyncio
async def test_offline_personal_smoke_uses_only_mocked_boundaries() -> None:
    """Root smoke integration can prove this path without dotenv, UI or sockets."""
    result = await smoke.run_personal_smoke()
    assert result.llm_calls == 1 and result.sender_calls == 0
    assert result.first_stats["dry_run"] == result.replay_stats["duplicate"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["claimed", "llm_failed", "send_failed", "sent"])
async def test_existing_incomplete_or_non_dry_run_claim_is_never_replayed(
    settings: DesktopSettings,
    status: str,
) -> None:
    """Retain prior claims and reject uncertain outcomes before constructing a model."""
    store = SQLiteStore(settings.sqlite_path)
    try:
        key = message_key(proof_message(), settings.dedup_bucket_seconds)
        assert await store.claim(proof_message(), key)
        if status != "claimed":
            await store.finish(key, status, reply=REPLY)
        before = await store.records()
    finally:
        await store.aclose()
    with pytest.raises(smoke.PersonalSmokeError, match="claim retained"):
        await smoke.run_live_personal_smoke(
            settings,
            reader=FakeReader((proof_row(),)),
            model_factory=forbidden_model,
        )
    reopened = SQLiteStore(settings.sqlite_path)
    try:
        assert await reopened.records() == before
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_external_reader_error_redacts_raw_values(
    settings: DesktopSettings,
) -> None:
    """Source diagnostics can contain private text and must not become error output."""

    class FailingReader:
        """Raise private details at the public reader boundary without actual I/O."""

        async def read_active_chat(self) -> PersonalHistorySnapshot:
            raise RuntimeError("private-personal-test-key private-chat-content")

    with pytest.raises(smoke.PersonalSmokeError) as error:
        await smoke.run_live_personal_smoke(
            settings,
            reader=FailingReader(),
            model_factory=forbidden_model,
        )
    assert str(error.value) == "Personal dry-run proof failed (RuntimeError)"
    assert error.value.__context__ is None
    assert not settings.sqlite_path.exists()


@pytest.mark.asyncio
async def test_reader_timeout_is_bounded_and_safely_reported(
    settings: DesktopSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable reader cannot hold the one-off verification indefinitely."""

    class HangingReader:
        """Block cooperatively until the proof's receive timeout cancels this read."""

        async def read_active_chat(self) -> PersonalHistorySnapshot:
            await asyncio.Event().wait()
            raise AssertionError("Unreachable")

    monkeypatch.setattr(smoke, "INBOX_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(smoke.PersonalSmokeError, match="TimeoutError"):
        await smoke.run_live_personal_smoke(
            settings,
            reader=HangingReader(),
            model_factory=forbidden_model,
        )
    assert not settings.sqlite_path.exists()


def test_help_never_reads_configuration_or_runs_the_personal_reader(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A standard help request must exit before touching dotenv or native history."""

    def forbidden_settings() -> DesktopSettings:
        raise AssertionError("Help must not load settings")

    monkeypatch.setattr(smoke, "DesktopSettings", forbidden_settings)
    with pytest.raises(SystemExit) as exit_info:
        smoke.main(["--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
