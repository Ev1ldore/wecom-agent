"""Verify the existing AgentLoop actually uses the new contextual interface."""

import json
from unittest.mock import AsyncMock

import pytest

from adapters.inbox import Msg
from src.core.assistant.desktop import DesktopAssistant, DesktopAssistantProtocol
from src.core.desktop_loop import AgentLoop, message_key
from src.core.desktop_store import SQLiteStore


def message(text, **kwargs):
    return Msg(chat="Ri", sender="Ri", content=text, ts=1700000000, **kwargs)


@pytest.mark.asyncio
async def test_existing_loop_uses_persistent_memory_and_never_sends(tmp_path):
    llm = AsyncMock()
    llm.reply.return_value = json.dumps(
        {"intent": "chat", "reply": "已按简短风格回复", "citations": []}
    )
    store = SQLiteStore(tmp_path / "desktop.db")
    processor: DesktopAssistantProtocol = DesktopAssistant(
        llm, tmp_path / "memory.db", chat="Ri", sender="Ri"
    )
    inbox = AsyncMock()
    first, second = message("记住：回复风格=简短"), message("今天聊什么")
    inbox.poll_unread.return_value = [first, second]
    sender = AsyncMock()
    drafts = []
    try:
        loop = AgentLoop(
            inbox,
            llm,
            sender,
            store,
            allowed_chat="Ri",
            allowed_sender="Ri",
            self_senders=frozenset({"self"}),
            message_processor=processor,
            emit=drafts.append,
        )
        assert (await loop.run_once())["dry_run"] == 2
        sender.send_text.assert_not_awaited()
        llm.reply.assert_awaited_once()
        context = json.loads(llm.reply.call_args.args[0])
        assert context["memory"][0]["value"] == "简短"
        records = await store.records()
        assert records[0]["key"] == message_key(first)
        assert records[0]["ts"] == 1700000000
        assert (await loop.run_once())["duplicate"] == 2
    finally:
        await processor.aclose()
        await store.aclose()


@pytest.mark.asyncio
async def test_manual_takeover_and_spam_filter_existing_loop(tmp_path):
    llm = AsyncMock()
    llm.reply.return_value = json.dumps(
        {"intent": "chat", "reply": "你好", "citations": []}
    )
    processor = DesktopAssistant(llm, tmp_path / "memory.db", chat="Ri", sender="Ri")
    store = SQLiteStore(tmp_path / "desktop.db")
    inbox = AsyncMock()
    sender = AsyncMock()
    try:
        inbox.poll_unread.return_value = [message("刷单返利")]
        loop = AgentLoop(
            inbox,
            llm,
            sender,
            store,
            allowed_chat="Ri",
            allowed_sender="Ri",
            self_senders=frozenset(),
            message_processor=processor,
            emit=lambda x: None,
        )
        assert (await loop.run_once())["filtered"] == 1
        inbox.poll_unread.return_value = [
            message("我手动回复", is_self=True),
            message("新问题"),
        ]
        stats = await loop.run_once()
        assert stats["filtered"] == 2
        llm.reply.assert_not_awaited()
        sender.send_text.assert_not_awaited()
    finally:
        await processor.aclose()
        await store.aclose()


def test_context_processor_cannot_enter_live_branch():
    with pytest.raises(ValueError, match="dry-run"):
        AgentLoop(
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            allowed_chat="Ri",
            allowed_sender="Ri",
            self_senders=frozenset(),
            dry_run=False,
            message_processor=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_bad_model_json_is_persisted_not_sent(tmp_path):
    llm = AsyncMock()
    llm.reply.return_value = "broken"
    processor = DesktopAssistant(llm, tmp_path / "memory.db", chat="Ri", sender="Ri")
    try:
        with pytest.raises(ValueError, match="增强回复生成失败"):
            await processor.reply_message(message("hello"), "key")
        assert (
            processor.store.rows("SELECT status FROM messages")[0]["status"] == "failed"
        )
    finally:
        await processor.aclose()
