"""Verify the desktop smoke uses real local storage and no external networks."""

import socket
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from smoke import smoke_desktop
from src.core.desktop_store import SQLiteStore


def forbid_network(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Reject real socket/HTTP operations while observing mocked model requests."""
    requests: list[httpx.Request] = []
    original_mock_request = httpx.MockTransport.handle_async_request

    async def no_http(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        """Fail the test if an actual async HTTP transport is ever used."""
        raise AssertionError("Desktop smoke attempted a real HTTP request")

    def no_socket(sock: socket.socket, address: Any) -> None:
        """Fail before any outbound socket connection can leave the process."""
        raise AssertionError("Desktop smoke attempted a real socket connection")

    async def observe_mock(
        transport: httpx.MockTransport, request: httpx.Request
    ) -> httpx.Response:
        """Count calls through the real HTTPReplyModel's injected MockTransport."""
        requests.append(request)
        return await original_mock_request(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_http)
    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", observe_mock)
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    return requests


@pytest.mark.asyncio
async def test_desktop_smoke_checks_real_store_restart_and_zero_outbound_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run the entire offline path and independently observe persistence and sends."""
    requests = forbid_network(monkeypatch)
    observed_rows: list[list[dict[str, Any]]] = []
    original_records = SQLiteStore.records
    send = AsyncMock(side_effect=AssertionError("No sender may run in dry-run smoke"))

    async def observe_records(store: SQLiteStore) -> list[dict[str, Any]]:
        """Read the actual temporary SQLite file, retaining evidence before cleanup."""
        rows = await original_records(store)
        observed_rows.append(rows)
        return rows

    monkeypatch.setattr(SQLiteStore, "records", observe_records)
    monkeypatch.setattr(smoke_desktop.NoSendSender, "send_text", send)

    await smoke_desktop.run_desktop_smoke()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    send.assert_not_awaited()
    assert len(observed_rows) == 2
    assert observed_rows[0] == observed_rows[1]
    assert len(observed_rows[0]) == 1
    stored = observed_rows[0][0]
    assert stored["status"] == "dry_run"
    assert stored["reply"] == smoke_desktop.DEMO_REPLY
    assert stored["content"] == smoke_desktop.DEMO_CONTENT
    assert stored["sender"] == smoke_desktop.DEMO_USER_ID
    assert stored["error"] == ""
    assert isinstance(stored["created_at"], int)
    assert isinstance(stored["updated_at"], int)
    output = capsys.readouterr().out
    assert "desktop smoke: PASS" in output
    assert "fake inbox" in output and "mock LLM" in output
    assert "zero real sends" in output
    assert output.count("[DRY-RUN]") == 1
    assert smoke_desktop.DEMO_REPLY in output
    assert smoke_desktop.DEMO_CONTENT not in output


@pytest.mark.asyncio
async def test_no_send_sender_counts_and_rejects_every_attempt() -> None:
    """Keep the smoke sender unusable even if a loop accidentally invokes it."""
    sender = smoke_desktop.NoSendSender()
    assert sender.calls == 0
    with pytest.raises(AssertionError, match="must never invoke"):
        await sender.send_text("synthetic-user", "synthetic-reply")
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_wx2_smoke_uses_mock_http_and_no_native_ui(monkeypatch, capsys) -> None:
    """The real OCR parser and sender run only against injected synthetic surfaces."""
    from adapters import wechat_ocr

    requests = forbid_network(monkeypatch)

    def no_native():
        raise AssertionError("Native UI must not be opened in offline smoke")

    monkeypatch.setattr(wechat_ocr, "_load_quartz", no_native)
    monkeypatch.setattr(wechat_ocr, "_load_appkit", no_native)
    await smoke_desktop.run_wx2_smoke()
    assert len(requests) == 1
    assert "wx2 smoke: PASS" in capsys.readouterr().out


def test_standalone_entry_returns_zero_after_offline_smoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercise the sync entry point with socket and real HTTP transport guards."""
    requests = forbid_network(monkeypatch)

    assert smoke_desktop.main() == 0

    assert len(requests) == 1
    assert "desktop smoke: PASS" in capsys.readouterr().out


def test_entry_propagates_failed_smoke_instead_of_reporting_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preserve a non-successful CLI outcome when an offline assertion fails."""
    runner = AsyncMock(side_effect=AssertionError("synthetic failed smoke"))
    monkeypatch.setattr(smoke_desktop, "run_desktop_smoke", runner)

    with pytest.raises(AssertionError, match="synthetic failed smoke"):
        smoke_desktop.main()

    runner.assert_awaited_once()
    assert "PASS" not in capsys.readouterr().out
