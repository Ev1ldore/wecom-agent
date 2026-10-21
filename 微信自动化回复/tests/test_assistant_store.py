"""Keep assistant state separate from existing desktop message claims."""

import pytest

from adapters.inbox import Msg
from src.core.assistant.store import MemoryStoreProtocol, Store
from src.core.desktop_store import SQLiteStore


@pytest.mark.asyncio
async def test_shared_database_does_not_change_original_claims(tmp_path):
    path = tmp_path / "existing.db"
    desktop = SQLiteStore(path)
    msg = Msg(chat="Ri", sender="Ri", content="old", ts=1700000000)
    assert await desktop.claim(msg, "old-key")
    await desktop.finish("old-key", "send_unknown")
    original = await desktop.records()
    state: MemoryStoreProtocol = Store(path)
    try:
        state.execute(
            "INSERT INTO memory VALUES(?,?,?,?,?)",
            ("scope", "风格", "简短", "source", None),
        )
        assert state.context("scope")["memory"][0]["value"] == "简短"
        assert await desktop.records() == original
    finally:
        state.close()
        await desktop.aclose()
