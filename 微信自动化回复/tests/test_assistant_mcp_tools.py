"""Local management tool authorization and persistence; no external calls."""

import fcntl
import json
import time

import pytest

from src.core.assistant.contact_book import contact_id
from src.core.assistant.mcp_tools import LocalTools, ToolFailure
from src.core.assistant.store import Store, encoded


@pytest.fixture
def setup(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "whitelist": [
                    {"conversation": "a", "sender": "alice"},
                    {"conversation": "b", "sender": "bob"},
                ],
                "contacts": [
                    {"name": "unchecked", "conversation": "c", "sender": "carol"}
                ],
                "secret": "must-not-leak",
            }
        )
    )
    database = tmp_path / "state.db"
    return (
        LocalTools(config, database),
        config,
        database,
        contact_id("a", "alice"),
        contact_id("b", "bob"),
    )


def test_list_only_public_metadata(setup):
    tools, _, database, a, b = setup
    result = tools.call("contacts_list", {})
    assert len(result["contacts"]) == 3
    assert all(set(c) == {"id", "name", "selected"} for c in result["contacts"])
    assert {c["id"] for c in result["contacts"] if c["selected"]} == {a, b}
    assert "must-not-leak" not in str(result)
    assert not database.exists()


def test_memory_round_trip_and_scope(setup):
    tools, _, _, a, b = setup
    assert tools.call(
        "memory_set",
        {"contact_id": a, "key": "style", "value": "brief", "ttl_seconds": 60},
    ) == {"saved": True}
    memory = tools.call("memory_list", {"contact_id": a})["memory"]
    assert memory[0]["value"] == "brief" and memory[0]["expires"] > time.time()
    assert tools.call("memory_list", {"contact_id": b})["memory"] == []
    assert tools.call("memory_forget", {"contact_id": a, "key": "style"}) == {
        "removed": 1
    }
    assert tools.call("memory_forget", {"contact_id": a, "key": "style"}) == {
        "removed": 0
    }


def test_revocation_checked_each_call(setup):
    tools, path, _, a, _ = setup
    tools.call("memory_set", {"contact_id": a, "key": "k", "value": "v"})
    data = json.loads(path.read_text())
    data["contacts"].append({"name": "a", "conversation": "a", "sender": "alice"})
    data["whitelist"] = [{"conversation": "b", "sender": "bob"}]
    path.write_text(json.dumps(data))
    for name in (
        "memory_list",
        "session_context_get",
        "session_context_reset",
        "documents_list",
    ):
        with pytest.raises(ToolFailure) as exc:
            tools.call(name, {"contact_id": a})
        assert exc.value.code == "NOT_ALLOWED"


@pytest.mark.parametrize(
    "name,args",
    [
        (
            "memory_set",
            {"contact_id": "0" * 24, "key": "k", "value": "v", "ttl_seconds": True},
        ),
        (
            "memory_set",
            {"contact_id": "0" * 24, "key": "k", "value": "v", "ttl_seconds": 0},
        ),
        (
            "memory_set",
            {"contact_id": "0" * 24, "key": "k", "value": "v", "ttl_seconds": 1.5},
        ),
        ("memory_list", {"contact_id": "../../secret"}),
        ("contacts_list", {"db": "/tmp/private"}),
        ("send_message", {}),
        ("memory_list", {}),
    ],
)
def test_invalid_arguments_rejected_before_access(setup, name, args):
    tools, _, database, _, _ = setup
    with pytest.raises(ToolFailure) as exc:
        tools.call(name, args)
    assert exc.value.code == "INVALID_ARGUMENT"
    assert not database.exists()


def test_unknown_and_unselected_contact(setup):
    tools, _, database, _, _ = setup
    for cid, code in [
        ("0" * 24, "NOT_FOUND"),
        (contact_id("c", "carol"), "NOT_ALLOWED"),
    ]:
        with pytest.raises(ToolFailure) as exc:
            tools.call("memory_list", {"contact_id": cid})
        assert exc.value.code == code
    assert not database.exists()


@pytest.mark.parametrize("which", ["config", "database"])
def test_lock_conflict_is_busy(setup, which):
    tools, config, database, a, _ = setup
    lock_path = config if which == "config" else database
    with open(str(lock_path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ToolFailure) as exc:
            tools.call("memory_list", {"contact_id": a})
        assert exc.value.code == "BUSY"


def test_reset_preserves_audit_memory_documents_and_other_scope(setup):
    tools, _, database, a, b = setup
    tools.call("memory_set", {"contact_id": a, "key": "style", "value": "brief"})
    store = Store(database)
    try:
        for scope in (encoded(["a", "alice"]), encoded(["b", "bob"])):
            store.execute(
                "INSERT INTO history(scope,role,text,source,created) VALUES(?,?,?,?,?)",
                (scope, "user", "hello", "m1", time.time()),
            )
        store.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?)",
            (
                "doc",
                encoded(["a", "alice"]),
                "/private/subdir/note.txt",
                "secret contents",
                "text layer",
            ),
        )
        assert tools.call("session_context_get", {"contact_id": a})["recent"]
        assert tools.call("session_context_reset", {"contact_id": a}) == {"reset": True}
        assert tools.call("session_context_get", {"contact_id": a})["recent"] == []
        assert tools.call("session_context_get", {"contact_id": b})["recent"]
        assert (
            tools.call("memory_list", {"contact_id": a})["memory"][0]["value"]
            == "brief"
        )
        docs = tools.call("documents_list", {"contact_id": a})["documents"]
        assert docs == [{"id": "doc", "name": "note.txt", "evidence": "text layer"}]
        assert len(store.rows("SELECT * FROM history")) == 2
    finally:
        store.close()


def test_corrupt_configuration_redacts_details(setup):
    tools, path, _, _, _ = setup
    path.write_text("private-secret invalid json")
    with pytest.raises(ToolFailure) as exc:
        tools.call("contacts_list", {})
    assert exc.value.code == "STORE_UNAVAILABLE"
    assert "private-secret" not in str(exc.value)
