"""Exercise the shared assistant and original AgentLoop with no real adapters."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from fastapi.testclient import TestClient

from adapters.inbox import Msg
from src.core.assistant.contact_book import ContactBook
from src.core.assistant.desktop import DesktopAssistant
from src.core.assistant.engine import Engine
from src.core.assistant.management import Management, WhitelistFile
from src.core.assistant.media import Media
from src.core.assistant.model import FixtureModel
from src.core.assistant.store import Store, encoded
from src.core.assistant.web import create_app
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
            manager = Management(engine)
            scope = encoded(["demo", "peer"])
            manager.set_memory(scope, "风格", "简短", 3600)
            assert manager.memories(scope)[0]["value"] == "简短"
            assert manager.short_term(scope)["recent"]
            prior_history = state.rows("SELECT * FROM history")
            manager.reset_short_term(scope)
            assert manager.short_term(scope)["recent"] == []
            assert state.rows("SELECT * FROM history") == prior_history
            assert manager.memories(scope)[0]["value"] == "简短"
            assert manager.forget_memory(scope, "风格") == 1
            document = manager.documents(scope)[0]
            assert manager.forget_document(scope, document["id"]) == 1
            assert manager.documents(scope) == []
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"whitelist": []}))
            config = WhitelistFile(config_path)
            config.change("demo", "peer")
            engine.update_whitelist(config.read())
            engine.ingest({**event, "id": "pending", "kind": "text", "text": "你好"})
            config.change("demo", "peer", remove=True)
            engine.update_whitelist(config.read())
            engine.drain()
            assert (
                state.rows("SELECT status FROM messages ORDER BY rowid DESC LIMIT 1")[
                    0
                ]["status"]
                == "cancelled"
            )
            assert len(state.rows("SELECT * FROM receipts")) == 1
            catalog = ContactBook(config_path)
            catalog.add("本地冒烟联系人", "demo", "peer", catalog.state()["revision"])
            with TestClient(
                create_app(config_path, root / "ui.db"), base_url="http://127.0.0.1"
            ) as client:
                data = client.get("/api/state").json()
                assert not data["contacts"][0]["selected"]
                assert (
                    client.post(
                        "/api/selection",
                        headers={"X-CSRF-Token": data["csrf_token"]},
                        json={
                            "revision": data["revision"],
                            "ids": [data["contacts"][0]["id"]],
                        },
                    ).status_code
                    == 200
                )
                assert client.get("/api/state").json()["contacts"][0]["selected"]
                assert client.get("/").status_code == 200
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
    print(
        "assistant smoke: PASS; memory CRUD + short-term reset + files + whitelist revocation + existing AgentLoop; real sends=0"
    )


async def run_mcp_smoke():
    """Use the official stdio client against temporary local management state."""
    import sys
    from datetime import timedelta

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from src.core.assistant.contact_book import contact_id

    with TemporaryDirectory() as temp:
        root = Path(temp)
        config = root / "config.json"
        config.write_text(
            json.dumps({"whitelist": [{"conversation": "demo", "sender": "peer"}]})
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "src.core.assistant.mcp_server",
                "--config",
                str(config),
                "--db",
                str(root / "state.db"),
            ],
        )
        with (root / "stderr.log").open("w") as errors:
            async with (
                stdio_client(params, errlog=errors) as (read, write),
                ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=15)
                ) as client,
            ):
                result = await client.initialize()
                assert result.protocolVersion == "2025-11-25"
                tools = (await client.list_tools()).tools
                assert len(tools) == 7
                cid = contact_id("demo", "peer")
                result = await client.call_tool(
                    "memory_set", {"contact_id": cid, "key": "style", "value": "brief"}
                )
                assert not result.isError
                result = await client.call_tool("memory_list", {"contact_id": cid})
                assert result.structuredContent["data"]["memory"][0]["value"] == "brief"
                result = await client.call_tool(
                    "session_context_reset", {"contact_id": cid}
                )
                assert not result.isError and result.structuredContent["data"]["reset"]
                config.write_text(
                    json.dumps(
                        {
                            "contacts": [
                                {
                                    "name": "peer",
                                    "conversation": "demo",
                                    "sender": "peer",
                                }
                            ],
                            "whitelist": [],
                        }
                    )
                )
                result = await client.call_tool("memory_list", {"contact_id": cid})
                assert (
                    result.isError
                    and result.structuredContent["error"]["code"] == "NOT_ALLOWED"
                )
    print(
        "MCP smoke: PASS; official stdio initialize + 7 tools + scoped memory + reset + revoke; real sends=0"
    )
