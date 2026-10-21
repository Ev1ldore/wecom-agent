"""Offline routing must happen before dotenv or desktop adapter construction."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import agent_loop
from src.desktop_config import DesktopSettings


def test_offline_dispatch_ignores_live_environment(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]

    def forbidden():
        raise AssertionError("offline dispatch must not read .env")

    monkeypatch.setattr(agent_loop, "DesktopSettings", forbidden)
    monkeypatch.setenv("DRY_RUN", "false")
    report = tmp_path / "report.json"
    assert (
        agent_loop.main(
            [
                "--offline-assistant",
                "replay",
                "--config",
                str(root / "assistant.example.json"),
                "--input",
                str(root / "examples/assistant/messages.jsonl"),
                "--db",
                str(tmp_path / "offline.db"),
                "--report",
                str(report),
            ]
        )
        == 0
    )
    data = json.loads(report.read_text())
    assert data["wechat_access"] is False
    assert len(data["receipts"]) == 5
    assert "DRY_RUN=false" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "flag",
    [
        "--live",
        "--wechat-native",
        "--personal",
        "--wechat",
        "--wechat-ocr",
        "--wechat-login",
        "--demo",
        "--assistant",
    ],
)
def test_offline_rejects_live_and_other_modes(flag):
    assert agent_loop.main(["--offline-assistant", "replay", flag]) == 2


def test_enhanced_live_blocked_before_client_activation(monkeypatch):
    runtime = AsyncMock()
    monkeypatch.setattr(agent_loop, "run_wechat_native", runtime)
    assert agent_loop.main(["--wechat-native", "--assistant", "--live"]) == 2
    runtime.assert_not_called()


@pytest.mark.asyncio
async def test_env_enhanced_live_blocked_before_client_activation(monkeypatch):
    import adapters.wechat_native as native

    def forbidden(**kwargs):
        raise AssertionError("must not construct desktop adapter")

    monkeypatch.setattr(native, "NativeRiChat", forbidden)
    settings = DesktopSettings(_env_file=None, assistant_enabled=True, dry_run=False)
    with pytest.raises(agent_loop.SetupError, match="只支持 dry-run"):
        await agent_loop._run_wechat_native_locked(settings, once=True)
