"""Verify restart baselines and receive/model/send wiring without real UI/LLM."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

import agent_loop
from adapters.inbox import Msg
from adapters.wechat_native import IDENTITY
from src.core.desktop_store import SQLiteStore
from src.desktop_config import DesktopSettings


def peer(text, label="18:40"):
    return Msg(
        chat=IDENTITY,
        sender=IDENTITY,
        content=text,
        ts=None,
        time_precision="minute",
        time_display=label,
    )


class Chat:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.sent = []
        self.activate = AsyncMock()

    async def poll_unread(self):
        return next(self.snapshots)

    async def send_text(self, user_id, content):
        self.sent.append((user_id, content))

    async def prepare_reply(self, user_id, content):
        return None


class Model:
    def __init__(self):
        self.reply = AsyncMock(return_value="模拟模型答复")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_fresh_baseline_skips_old_and_only_processes_new_append(
    tmp_path, monkeypatch, live
):
    import adapters.wechat_native as native

    old, new = peer("旧消息"), peer("新问题", "18:41")
    chat = Chat([[old], [old, new], [old, new]])
    model = Model()
    monkeypatch.setattr(native, "NativeRiChat", lambda **kw: chat)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", lambda *a, **kw: model)
    runs = []

    async def cycles(loop, **kw):
        for _ in range(3):
            runs.append(await loop.run_once())
        return 0

    monkeypatch.setattr(agent_loop, "run_cycles", cycles)
    settings = DesktopSettings(
        _env_file=None,
        dry_run=not live,
        db_url="sqlite:///" + str(tmp_path / "native.db"),
        llm_base_url="https://example.test/v1",
        llm_model="fake",
        llm_api_key=SecretStr("fake"),
    )
    assert await agent_loop._run_wechat_native_locked(settings, once=True) == 0
    model.reply.assert_awaited_once_with("新问题")
    assert chat.sent == ([(IDENTITY, "模拟模型答复")] if live else [])
    store = SQLiteStore(settings.sqlite_path)
    records = await store.records()
    await store.aclose()
    assert [r["status"] for r in records] == ["baseline", "sent" if live else "dry_run"]
    assert runs[-1]["polled"] == 0


@pytest.mark.asyncio
async def test_wrong_contact_is_rejected_before_native_action(monkeypatch):
    settings = DesktopSettings(_env_file=None, wechat_ocr_contact="Other")
    with pytest.raises(agent_loop.SetupError, match="Ri"):
        await agent_loop._run_wechat_native_locked(settings, once=True)


def test_entry_selects_native_mode_and_requires_explicit_live(monkeypatch):
    monkeypatch.setattr(
        agent_loop, "DesktopSettings", lambda: DesktopSettings(_env_file=None)
    )
    runtime = AsyncMock(return_value=0)
    monkeypatch.setattr(agent_loop, "run_wechat_native", runtime)
    assert agent_loop.main(["--wechat-native", "--once"]) == 0
    assert runtime.call_args.args[0].dry_run is True
    assert agent_loop.main(["--wechat-native", "--live", "--once"]) == 0
    assert runtime.call_args.args[0].dry_run is False


@pytest.mark.parametrize(
    "other", ["--personal", "--wechat", "--wechat-ocr", "--demo", "--wechat-login"]
)
def test_native_mode_refuses_combined_modes(other):
    assert agent_loop.main(["--wechat-native", other]) == 2


@pytest.mark.asyncio
async def test_assistant_uses_native_loop_with_memory_and_no_send(
    tmp_path, monkeypatch
):
    import json

    import adapters.wechat_native as native

    chat = Chat(
        [
            [peer("历史")],
            [peer("历史"), peer("记住：称呼=小李")],
            [peer("历史"), peer("记住：称呼=小李"), peer("你好", "18:42")],
            [peer("历史"), peer("记住：称呼=小李"), peer("你好", "18:42")],
        ]
    )
    model = Model()
    model.reply.return_value = json.dumps(
        {"intent": "chat", "reply": "你好，小李", "citations": []}
    )
    templates = []

    def make_model(*args, **kwargs):
        templates.append(kwargs["template_name"])
        return model

    monkeypatch.setattr(native, "NativeRiChat", lambda **kw: chat)
    monkeypatch.setattr(agent_loop, "HTTPReplyModel", make_model)

    async def cycles(loop, **kwargs):
        for _ in range(3):
            await loop.run_once()
        return 0

    monkeypatch.setattr(agent_loop, "run_cycles", cycles)
    settings = DesktopSettings(
        _env_file=None,
        dry_run=True,
        assistant_enabled=True,
        db_url="sqlite:///" + str(tmp_path / "native.db"),
        llm_base_url="https://example.test/v1",
        llm_model="fake",
        llm_api_key=SecretStr("fake"),
    )
    assert await agent_loop._run_wechat_native_locked(settings, once=True) == 0
    assert templates == ["assistant_reply.txt"]
    context = json.loads(model.reply.call_args.args[0])
    assert context["memory"][0]["value"] == "小李"
    assert chat.sent == []
    store = SQLiteStore(settings.sqlite_path)
    records = await store.records()
    await store.aclose()
    assert [r["status"] for r in records] == ["baseline", "dry_run", "dry_run"]
    assert records[-1]["time_display"] == "18:42"


@pytest.mark.parametrize("configured_dry_run", [True, False])
@pytest.mark.parametrize("explicit_live", [True, False])
def test_native_live_requires_this_invocation_flag(
    monkeypatch, configured_dry_run, explicit_live
):
    settings = DesktopSettings(_env_file=None, dry_run=configured_dry_run)
    monkeypatch.setattr(agent_loop, "DesktopSettings", lambda: settings)
    runtime = AsyncMock(return_value=0)
    monkeypatch.setattr(agent_loop, "run_wechat_native", runtime)
    args = ["--wechat-native", "--once"] + (["--live"] if explicit_live else [])
    assert agent_loop.main(args) == 0
    assert runtime.call_args.args[0].dry_run is not explicit_live
