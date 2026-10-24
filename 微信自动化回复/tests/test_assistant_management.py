"""Manual configuration, scoped administration, and queue revocation checks."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.assistant.engine import Engine
from src.core.assistant.management import Management, WhitelistFile, whitelist
from src.core.assistant.media import Media
from src.core.assistant.model import FixtureModel
from src.core.assistant.store import Store, encoded

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "state.db")
    model = Mock(wraps=FixtureModel())
    config = {"whitelist": [{"conversation": "a", "sender": "alice"}]}
    engine = Engine(store, config, model, Media(tmp_path, {}))
    yield engine, Management(engine), store, model
    store.close()


def event(id="1", text="你好", **changes):
    return {
        "id": id,
        "conversation": "a",
        "sender": "alice",
        "kind": "text",
        "text": text,
        **changes,
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"whitelist": "*"},
        {"whitelist": ["alice"]},
        {"whitelist": [{"conversation": "a", "sender": "*"}]},
        {"whitelist": [{"conversation": "a", "sender": " alice"}]},
    ],
)
def test_invalid_whitelist_rejected(value):
    with pytest.raises(ValueError):
        whitelist(value)


def test_empty_whitelist_means_deny_everyone(tmp_path):
    assert whitelist({"whitelist": []}) == []


def test_config_changes_atomic_preserve_other_fields(tmp_path):
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"whitelist": [], "media": {"path": "keep"}}))
    repo = WhitelistFile(path)
    assert len(repo.change("a", "alice")) == 1
    assert len(repo.change("a", "alice")) == 1
    assert repo.read()["media"] == {"path": "keep"}
    assert repo.change("a", "alice", remove=True) == []


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "local.json"
    original = '{"whitelist": []}'
    path.write_text(original)

    def failed(*args):
        raise OSError("disk error")

    monkeypatch.setattr("os.replace", failed)
    with pytest.raises(OSError):
        WhitelistFile(path).change("a", "alice")
    assert path.read_text() == original
    assert sorted(x.name for x in tmp_path.iterdir()) == [
        "local.json",
        "local.json.lock",
    ]


def test_remove_whitelist_cancels_queued_and_requires_resume(setup):
    engine, _manager, store, model = setup
    engine.ingest(event())
    engine.update_whitelist({"whitelist": []})
    engine.drain()
    assert store.rows("SELECT status FROM messages")[0]["status"] == "cancelled"
    model.generate.assert_not_called()
    engine.update_whitelist({"whitelist": [{"conversation": "a", "sender": "alice"}]})
    assert engine.ingest(event("2")) == "paused"
    engine.resume(encoded(["a", "alice"]))
    assert engine.ingest(event("3")) == "pending"
    engine.drain()
    assert len(store.rows("SELECT * FROM receipts")) == 1


def test_restart_smaller_whitelist_drops_old_pending(setup):
    engine, _manager, store, model = setup
    engine.ingest(event())
    replacement = Engine(store, {"whitelist": []}, model, engine.media)
    replacement.drain()
    assert store.rows("SELECT status FROM messages")[0]["status"] == "cancelled"
    model.generate.assert_not_called()


def test_revocation_during_model_cannot_produce_outbox(setup):
    engine, _manager, store, model = setup

    def revoke(context):
        engine.update_whitelist({"whitelist": []})
        return {"intent": "chat", "reply": "draft", "citations": []}

    model.generate.side_effect = revoke
    engine.ingest(event())
    engine.drain()
    assert store.rows("SELECT status FROM messages")[0]["status"] == "stale"
    assert store.rows("SELECT * FROM outbox") == []


def test_question_before_memory_command_is_not_lost(setup):
    engine, manager, store, model = setup
    engine.ingest(event(text="会议几点？"))
    engine.ingest(event("2", "记住：风格=简短"))
    engine.drain()
    assert len(store.rows("SELECT * FROM receipts")) == 2
    model.generate.assert_called_once()
    assert manager.memories(encoded(["a", "alice"]))[0]["value"] == "简短"


def test_memory_is_scoped_and_survives_restart(setup):
    engine, manager, store, model = setup
    scope = encoded(["a", "alice"])
    manager.set_memory(scope, "风格", "简短")
    restored = Management(Engine(store, engine.config, model, engine.media))
    assert restored.memories(scope)[0]["value"] == "简短"
    with pytest.raises(ValueError):
        restored.memories(encoded(["b", "bob"]))
    assert restored.forget_memory(scope, "风格") == 1
    assert restored.memories(scope) == []


def test_memory_expiration_not_returned(setup, monkeypatch):
    _engine, manager, _store, _model = setup
    scope = encoded(["a", "alice"])
    monkeypatch.setattr("time.time", lambda: 100)
    manager.set_memory(scope, "风格", "简短", 5)
    assert manager.memories(scope)
    monkeypatch.setattr("time.time", lambda: 106)
    assert manager.memories(scope) == []


@pytest.mark.parametrize(
    "key,value,ttl",
    [
        (None, "v", None),
        ("k", None, None),
        ("k", "v", 0),
        ("k", "v", True),
        ("k", "v", 31536001),
    ],
)
def test_invalid_memory_does_not_write(setup, key, value, ttl):
    _engine, manager, store, _model = setup
    with pytest.raises(ValueError):
        manager.set_memory(encoded(["a", "alice"]), key, value, ttl)
    assert store.rows("SELECT * FROM memory") == []


def test_attachment_list_forget_and_original_file_retained(setup, tmp_path):
    engine, manager, _store, model = setup
    (tmp_path / "meeting.txt").write_text("会议：周五15点")
    engine.ingest(event(kind="file", attachment="meeting.txt", text="会议几点"))
    engine.drain()
    scope = encoded(["a", "alice"])
    documents = manager.documents(scope)
    assert len(documents) == 1
    assert documents[0]["name"] == "meeting.txt"
    assert manager.forget_document(scope, documents[0]["id"]) == 1
    assert manager.documents(scope) == []
    assert (tmp_path / "meeting.txt").exists()
    model.generate.reset_mock()
    engine.ingest(event("2", "会议几点"))
    engine.drain()
    assert model.generate.call_args.args[0]["documents"] == []


def cli(tmp_path, *args):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "agent_loop.py"),
            "--offline-assistant",
            *args,
            "--config",
            str(tmp_path / "config.json"),
            "--db",
            str(tmp_path / "state.db"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=ROOT,
    )


def test_cli_manual_whitelist_memory_and_attachment(tmp_path):
    config = json.loads((ROOT / "assistant.example.json").read_text())
    config.update(whitelist=[], inbox="inbox")
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "meeting.txt").write_text("会议时间：周五15点")
    pair = ["--conversation", "a", "--sender", "alice"]
    assert cli(tmp_path, "whitelist-add", *pair).returncode == 0
    assert len(json.loads(cli(tmp_path, "whitelist-list").stdout)["whitelist"]) == 1
    result = cli(tmp_path, "memory-set", *pair, "--key", "风格", "--value", "简短")
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(cli(tmp_path, "memory-list", *pair).stdout)["memory"][0]["value"]
        == "简短"
    )
    assert cli(tmp_path, "memory-forget", *pair, "--key", "风格").returncode == 0
    assert json.loads(cli(tmp_path, "memory-list", *pair).stdout)["memory"] == []
    result = cli(
        tmp_path,
        "attachment-add",
        *pair,
        "--attachment",
        "meeting.txt",
        "--text",
        "几点",
        "--message-id",
        "fixed",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["outbox"][0]["status"] == "simulated"
    doc = json.loads(cli(tmp_path, "attachment-list", *pair).stdout)["documents"][0]
    assert (
        cli(tmp_path, "attachment-forget", *pair, "--message-id", doc["id"]).returncode
        == 0
    )
    assert json.loads(cli(tmp_path, "attachment-list", *pair).stdout)["documents"] == []
    assert cli(tmp_path, "whitelist-remove", *pair).returncode == 0
    assert cli(tmp_path, "memory-list", *pair).returncode != 0


def test_watch_loads_manual_whitelist_changes_without_database_lock(tmp_path):
    import signal
    import sqlite3
    import time

    config = json.loads((ROOT / "assistant.example.json").read_text())
    config.update(whitelist=[], inbox="inbox", poll_seconds=0.02)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    events = tmp_path / "events.jsonl"
    events.touch()
    command = [
        sys.executable,
        str(ROOT / "agent_loop.py"),
        "--offline-assistant",
        "watch",
        "--config",
        str(path),
        "--input",
        str(events),
        "--db",
        str(tmp_path / "state.db"),
    ]

    def wait_for(sql):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            try:
                with sqlite3.connect(tmp_path / "state.db") as db:
                    if db.execute(sql).fetchone()[0]:
                        return
            except sqlite3.OperationalError:
                pass
            time.sleep(0.02)
        pytest.fail("watch未观察到预期状态")

    with (tmp_path / "watch.log").open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log)
        try:
            wait_for("SELECT COUNT(*) FROM sqlite_master WHERE name='messages'")
            assert (
                cli(
                    tmp_path,
                    "whitelist-add",
                    "--conversation",
                    "a",
                    "--sender",
                    "alice",
                ).returncode
                == 0
            )
            with events.open("a") as stream:
                stream.write(json.dumps(event()) + "\n")
            wait_for("SELECT COUNT(*) FROM receipts")
            assert (
                cli(
                    tmp_path,
                    "whitelist-remove",
                    "--conversation",
                    "a",
                    "--sender",
                    "alice",
                ).returncode
                == 0
            )
            wait_for("SELECT COUNT(*) FROM sessions WHERE paused=1")
            with events.open("a") as stream:
                stream.write(json.dumps(event("2")) + "\n")
            wait_for("SELECT COUNT(*) FROM messages WHERE detail='not_whitelisted'")
            with sqlite3.connect(tmp_path / "state.db") as db:
                assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        finally:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)
        assert process.returncode == 0


def test_voice_transcript_becomes_scoped_retrievable_document(setup):
    engine, manager, store, model = setup
    engine.media = Mock()
    engine.media.extract.return_value = (
        [{"source": "audio.wav:转写", "text": "明天下午三点评审"}],
        "mock local ASR",
    )
    engine.ingest(event(kind="voice", attachment="audio.wav", text=""))
    engine.drain()
    scope = encoded(["a", "alice"])
    assert manager.documents(scope)[0]["name"] == "audio.wav"
    engine.ingest(event("2", "评审几点"))
    engine.drain()
    assert (
        model.generate.call_args.args[0]["documents"][0]["source"] == "audio.wav:转写"
    )
    assert any(
        "明天下午三点" in x["text"]
        for x in store.rows("SELECT text FROM history WHERE role='user'")
    )
