"""Bounded session context, expiry and reset, without model or UI calls."""

import time
from types import SimpleNamespace

import pytest

from src.core.assistant.engine import Engine
from src.core.assistant.management import Management
from src.core.assistant.media import Media
from src.core.assistant.store import Store, encoded


@pytest.fixture
def store(tmp_path):
    state = Store(tmp_path / "state.db")
    yield state
    state.close()


def add(store, scope, text, created=None):
    store.execute(
        "INSERT INTO history(scope,role,text,source,created) VALUES(?,?,?,?,?)",
        (scope, "user", text, text[:10], time.time() if created is None else created),
    )


def test_expiry_is_applied_to_recent_and_excerpts(store):
    add(store, "a", "expired", time.time() - 1801)
    add(store, "a", "valid")
    ctx = store.context("a", window=1)
    assert [x["text"] for x in ctx["recent"]] == ["valid"]
    assert ctx["older_excerpts"] == []


def test_scope_and_text_budget(store):
    add(store, "b", "private")
    for i in range(40):
        add(store, "a", str(i) + "x" * 2000)
    ctx = store.context("a", window=3, max_chars=1200)
    assert sum(len(x["text"]) for x in ctx["recent"]) <= 1200
    assert ctx["recent"][-1]["text"].startswith("39")
    assert ctx["recent"][-1]["truncated"]
    assert ctx["older_excerpts"] == []
    assert "private" not in str(ctx)


def test_reset_persists_and_preserves_explicit_memory_and_audit(store, tmp_path):
    scope = encoded(["a", "alice"])
    store.execute("INSERT INTO sessions(scope) VALUES(?)", (scope,))
    store.execute(
        "INSERT INTO memory VALUES(?,?,?,?,?)",
        (scope, "style", "brief", "manual", None),
    )
    add(store, scope, "old")
    store.reset_context(scope)
    assert store.rows("SELECT revision FROM sessions")[0]["revision"] == 1
    assert store.context(scope)["recent"] == []
    assert store.rows("SELECT text FROM history")[0]["text"] == "old"
    with_second = Store(tmp_path / "state.db")
    try:
        add(with_second, scope, "new")
        ctx = with_second.context(scope)
        assert [x["text"] for x in ctx["recent"]] == ["new"]
        assert ctx["memory"][0]["value"] == "brief"
    finally:
        with_second.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 0},
        {"window": True},
        {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
        {"max_chars": 999},
        {"max_chars": 64001},
    ],
)
def test_invalid_limits_rejected(store, kwargs):
    with pytest.raises(ValueError):
        store.context("a", **kwargs)


def test_management_checks_whitelist_for_read_and_reset(store):
    allowed = encoded(["a", "alice"])
    other = encoded(["b", "bob"])
    manager = Management(SimpleNamespace(store=store, allowed={("a", "alice")}))
    add(store, allowed, "a")
    add(store, other, "b")
    for action in (manager.short_term, manager.reset_short_term):
        with pytest.raises(ValueError):
            action(other)
    manager.reset_short_term(allowed)
    assert manager.short_term(allowed)["recent"] == []
    assert store.context(other)["recent"][0]["text"] == "b"


def test_engine_passes_ttl_and_reset_invalidates_inflight_reply(store, tmp_path):
    scope = encoded(["a", "alice"])
    add(store, scope, "old", time.time() - 120)
    captured = []

    class Model:
        def generate(self, context):
            captured.append(context)
            store.reset_context(scope)
            return {"intent": "chat", "reply": "ok", "citations": []}

    engine = Engine(
        store,
        {
            "whitelist": [{"conversation": "a", "sender": "alice"}],
            "short_term_ttl_seconds": 60,
        },
        Model(),
        Media(tmp_path, {}),
    )
    engine.ingest(
        {
            "id": "new",
            "conversation": "a",
            "sender": "alice",
            "kind": "text",
            "text": "new",
        }
    )
    engine.drain()
    assert [x["text"] for x in captured[0]["recent"]] == ["new"]
    assert store.rows("SELECT status FROM messages")[0]["status"] == "stale"
    assert not store.rows("SELECT * FROM receipts")


def test_cli_session_show_and_reset(tmp_path, capsys):
    import json
    from pathlib import Path

    from src.core.assistant.cli import main

    config = json.loads(
        (Path(__file__).resolve().parents[1] / "assistant.example.json").read_text()
    )
    config["whitelist"] = [{"conversation": "a", "sender": "alice"}]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    database = tmp_path / "cli.db"
    state = Store(database)
    add(state, encoded(["a", "alice"]), "retained audit")
    state.close()
    options = [
        "--config",
        str(path),
        "--db",
        str(database),
        "--conversation",
        "a",
        "--sender",
        "alice",
    ]
    main(["session-show", *options])
    assert (
        json.loads(capsys.readouterr().out)["short_term"]["recent"][0]["text"]
        == "retained audit"
    )
    main(["session-reset", *options])
    result = json.loads(capsys.readouterr().out)
    assert result["reset"] and result["short_term"]["recent"] == []
