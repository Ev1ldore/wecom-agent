"""Exercise the shared assistant and original AgentLoop with no real adapters."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from adapters.inbox import Msg
from src.core.assistant.desktop import DesktopAssistant
from src.core.assistant.engine import Engine
from src.core.assistant.media import Media
from src.core.assistant.model import FixtureModel
from src.core.assistant.store import Store
from src.core.desktop_loop import AgentLoop
from src.core.desktop_store import SQLiteStore


class AssistantSmokeProtocol(Protocol):
    async def __call__(self) -> None:
        """Exercise offline and existing-loop bridges without external effects."""
        raise NotImplementedError


async def run_assistant_smoke() -> None:
    class Model:
        async def reply(self, content):
            context = json.loads(content)
            assert context["memory"][0]["value"] == "简短"
            return json.dumps({"intent": "chat", "reply": "简短回复", "citations": []})

    class Inbox:
        async def poll_unread(self):
            return [
                Msg(chat="demo", sender="peer", content=text, ts=1700000000)
                for text in ["记住：风格=简短", "你好"]
            ]

    class Sender:
        async def send_text(self, *args):
            raise AssertionError("no real sends permitted")

    with TemporaryDirectory() as directory:
        root = Path(directory)
        state = Store(root / "offline.db")
        (root / "notes.txt").write_text("评审15:00开始。")
        try:
            engine = Engine(
                state,
                {"whitelist": [{"conversation": "demo", "sender": "peer"}]},
                FixtureModel(),
                Media(root, {}),
            )
            event = {
                "id": "file1",
                "conversation": "demo",
                "sender": "peer",
                "kind": "file",
                "attachment": "notes.txt",
                "text": "几点开始？",
            }
            engine.ingest(event)
            engine.drain()
            assert "15:00" in state.rows("SELECT text FROM outbox")[0]["text"]
            assert engine.ingest(event) == "duplicate"
        finally:
            state.close()
        store = SQLiteStore(root / "desktop.db")
        processor = DesktopAssistant(
            Model(), root / "memory.db", chat="demo", sender="peer"
        )
        try:
            loop = AgentLoop(
                Inbox(),
                Model(),
                Sender(),
                store,
                allowed_chat="demo",
                allowed_sender="peer",
                self_senders=frozenset(),
                message_processor=processor,
                emit=lambda value: None,
            )
            assert (await loop.run_once())["dry_run"] == 2
            assert (await loop.run_once())["duplicate"] == 2
        finally:
            await processor.aclose()
            await store.aclose()
    print("assistant smoke: PASS; memory + files + existing AgentLoop; real sends=0")
