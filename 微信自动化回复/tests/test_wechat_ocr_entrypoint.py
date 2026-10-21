"""Verify the OCR runtime entry gates without any desktop window I/O."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import agent_loop
from adapters.inbox import Msg
from src.desktop_config import DesktopSettings


@pytest.fixture(autouse=True)
def isolate_desktop_process_lock(monkeypatch, tmp_path):
    """Unit tests must not contend with or bypass the real user's live executor."""
    monkeypatch.setattr(agent_loop.Path, "home", lambda: tmp_path)


@pytest.mark.asyncio
async def test_wechat_ocr_runtime_requires_verified_contact() -> None:
    """Without WECHAT_OCR_CONTACT the runtime stops before any window read."""
    settings = DesktopSettings(_env_file=None, wechat_ocr_contact="")
    with pytest.raises(agent_loop.SetupError):
        await agent_loop.run_wechat_ocr(settings, once=True)


def test_wechat_ocr_flag_requires_contact_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI entry reports the missing contact instead of touching WeChat."""
    monkeypatch.delenv("WECHAT_OCR_CONTACT", raising=False)
    monkeypatch.setattr(
        agent_loop, "DesktopSettings", lambda: DesktopSettings(_env_file=None)
    )
    assert agent_loop.main(["--wechat-ocr"]) == 2


@pytest.mark.asyncio
async def test_wechat_ocr_runtime_baselines_then_dry_runs_new_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Baseline history is skipped; one new message yields one dry-run draft."""
    from adapters import personal_tail, wechat_ocr

    def minute_message(content: str, label: str, *, is_self: bool = False) -> Msg:
        return Msg(
            chat="desktop:wechat-ocr:Ri",
            sender="desktop:self" if is_self else "desktop:wechat-ocr:Ri",
            content=content,
            ts=None,
            time_precision="minute",
            time_display=label,
            is_self=is_self,
        )

    old = minute_message("old", "昨天 19:49")
    new = minute_message("new", "15:07")
    own = minute_message("own", "15:07", is_self=True)
    polls = AsyncMock(side_effect=[[old], [old, new, own], []])
    prepared = AsyncMock()

    class FakeInbox:
        """Placeholder source consumed by the production tail wrapper."""

        def __init__(self, *, contact: str, reader: Any) -> None:
            """Verify the verified contact name reaches the inbox unchanged."""
            assert contact == "Ri"

        async def poll_unread(self) -> list[Msg]:
            """Return an empty snapshot; the tail drives real reads."""
            return []

    class FakeTail:
        """Replay baseline history, then one new poll, then silence."""

        def __init__(self, source: FakeInbox, *, known_keys: frozenset, key_of: Any):
            """Accept the same construction contract as the production tail."""
            assert isinstance(source, FakeInbox)

        async def baseline(self) -> list[Msg]:
            """Return the visible history once."""
            return await polls()

        async def poll_unread(self) -> list[Msg]:
            """Return appended messages and then nothing."""
            return await polls()

    class FakeChannel:
        """Record dry-run previews without any native preflight."""

        def __init__(self, *, contact: str, live_enabled: bool, reader: Any) -> None:
            """Capture the explicit live switch for assertion."""
            assert contact == "Ri"
            assert live_enabled is False

        async def prepare_reply(self, user_id: str, content: str) -> None:
            """Record that the draft was bound to the verified chat."""
            await prepared(user_id, content)

    @asynccontextmanager
    async def fake_model(*args: Any, **kwargs: Any):
        """Keep all model calls mocked, including context setup."""
        model = AsyncMock()
        model.reply.return_value = "mock ocr reply"
        yield model

    async def two_cycles(loop: Any, *, once: bool, interval: float) -> int:
        """Run the polling cycle that must contain exactly one new draft."""
        first = await loop.run_once()
        second = await loop.run_once()
        assert first["dry_run"] == 1
        assert second["polled"] == 0
        return 0

    monkeypatch.setattr(wechat_ocr, "WeChatOCRInbox", FakeInbox)
    monkeypatch.setattr(wechat_ocr, "WxCLIChangeProbe", lambda **kwargs: object())
    monkeypatch.setattr(wechat_ocr, "DesktopWeChatOCRReader", lambda **kwargs: object())
    monkeypatch.setattr(personal_tail, "PersonalTailInbox", FakeTail)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", fake_model)
    monkeypatch.setattr(agent_loop, "run_cycles", two_cycles)
    from adapters import wechat_ocr_send

    monkeypatch.setattr(wechat_ocr_send, "WeChatOCRReplyChannel", FakeChannel)

    settings = DesktopSettings(
        _env_file=None,
        inbox_backend="wechat_ocr",
        wechat_ocr_contact="Ri",
        db_url=f"sqlite:///{tmp_path / 'ocr.db'}",
        llm_api_key="test-model-key",
    )
    assert await agent_loop.run_wechat_ocr(settings, once=True) == 0
    prepared.assert_awaited_once_with("desktop:wechat-ocr:Ri", "mock ocr reply")


@pytest.mark.asyncio
async def test_unverified_cli_stops_before_any_ui_or_model(
    monkeypatch, tmp_path
) -> None:
    """The wx2 CLI source is mandatory; OCR never silently takes over."""
    from adapters import wechat_ocr

    def forbidden(**kwargs):
        raise AssertionError("UI or model must not be constructed")

    monkeypatch.setattr(wechat_ocr, "DesktopWeChatOCRReader", forbidden)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", forbidden)
    settings = DesktopSettings(
        _env_file=None,
        wechat_ocr_contact="Ri",
        wx_cli_account_verified=False,
        db_url=f"sqlite:///{tmp_path / 'not-created.db'}",
    )
    with pytest.raises(wechat_ocr.WeChatOCRError) as error:
        await agent_loop.run_wechat_ocr(settings, once=True)
    assert error.value.code == "CLI_SETUP"
    assert not settings.sqlite_path.exists()


def test_wx2_runtime_lock_blocks_a_second_executor_and_releases(
    monkeypatch, tmp_path
) -> None:
    """Separate DB choices cannot create two desktop actors for wx2."""
    monkeypatch.setattr(agent_loop.Path, "home", lambda: tmp_path)
    with (
        agent_loop._wx2_runtime_lock(),
        pytest.raises(agent_loop.SetupError, match="已有"),
        agent_loop._wx2_runtime_lock(),
    ):
        raise AssertionError("Second executor acquired the lock")
    with agent_loop._wx2_runtime_lock():
        pass
