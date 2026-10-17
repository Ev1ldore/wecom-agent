"""Verify durable desktop-message claims using temporary SQLite databases."""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from adapters.inbox import Msg
from src.core.desktop_store import SQLiteStore, StoreProtocol


def message() -> Msg:
    """Build one complete input without reading environment configuration."""
    return Msg(
        chat="app:1000001:LiChengYun",
        sender="LiChengYun",
        content="完整原文 & 中文\n第二行",
        ts=1700000001,
    )


def create_legacy_database(path: Path) -> None:
    """Create a real prior-version database with nonsequential claim rowids."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE desktop_messages (
                key TEXT PRIMARY KEY, chat TEXT NOT NULL, sender TEXT NOT NULL,
                content TEXT NOT NULL, ts INTEGER NOT NULL, chat_type TEXT NOT NULL,
                is_self INTEGER NOT NULL, status TEXT NOT NULL,
                reply TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            )"""
        )
        connection.executemany(
            """INSERT INTO desktop_messages
            (rowid, key, chat, sender, content, ts, chat_type, is_self, status,
             reply, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    3,
                    "old-failure",
                    "Ri",
                    "Ri",
                    "旧消息一",
                    1700000001,
                    "single",
                    0,
                    "send_failed",
                    "保留回复",
                    "保留错误",
                    100,
                    101,
                ),
                (
                    9,
                    "old-claim",
                    "Ri",
                    "本人",
                    "旧消息二",
                    1700000002,
                    "single",
                    1,
                    "claimed",
                    "",
                    "",
                    100,
                    100,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_minute_message_survives_restart_without_fabricated_timestamp(
    tmp_path: Path,
) -> None:
    """Persist exact source labels and NULL timestamps through the public API."""
    path = tmp_path / "minute.db"
    msg = Msg(
        chat="Ri",
        sender="Ri",
        content="你好",
        ts=None,
        time_precision="minute",
        time_display="  昨天12:11  ",
    )
    first = SQLiteStore(path)
    try:
        assert await first.claim(msg, "minute-key")
        await first.finish("minute-key", "dry_run", reply="你好，Ri")
    finally:
        await first.aclose()

    reopened = SQLiteStore(path)
    try:
        assert not await reopened.claim(msg, "minute-key")
        row = (await reopened.records())[0]
        assert all(row[field] == value for field, value in msg.model_dump().items())
        assert row["ts"] is None
        assert row["time_display"] == "  昨天12:11  "
        assert row["status"] == "dry_run"
        assert row["reply"] == "你好，Ri"
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_old_database_migrates_idempotently_without_resetting_claims(
    tmp_path: Path,
) -> None:
    """Keep every legacy field and rowid, then permit minute inputs after upgrade."""
    path = tmp_path / "legacy.db"
    create_legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT rowid, * FROM desktop_messages ORDER BY rowid"
            )
        ]

    for _ in range(2):
        store = SQLiteStore(path)
        try:
            rows = await store.records()
            assert [row["key"] for row in rows] == ["old-failure", "old-claim"]
            for old, row in zip(legacy_rows, rows, strict=True):
                assert all(
                    row[field] == value
                    for field, value in old.items()
                    if field != "rowid"
                )
                assert row["time_precision"] == "second"
                assert row["time_display"] is None
                assert not await store.claim(message(), old["key"])
        finally:
            await store.aclose()

    store = SQLiteStore(path)
    try:
        minute = Msg(
            chat="Ri",
            sender="Ri",
            content="你好",
            ts=None,
            time_precision="minute",
            time_display="昨天12:11",
        )
        assert await store.claim(minute, "new-minute")
        assert [row["key"] for row in await store.records()] == [
            "old-failure",
            "old-claim",
            "new-minute",
        ]
    finally:
        await store.aclose()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT rowid, key FROM desktop_messages ORDER BY rowid"
        ).fetchall() == [(3, "old-failure"), (9, "old-claim"), (10, "new-minute")]


@pytest.mark.asyncio
async def test_two_instances_can_open_and_migrate_a_legacy_database(
    tmp_path: Path,
) -> None:
    """Serialize upgrades across connections while preserving one claim winner."""
    path = tmp_path / "migration-race.db"
    create_legacy_database(path)
    stores = [SQLiteStore(path), SQLiteStore(path)]
    try:
        results = await asyncio.gather(
            *(store.claim(message(), "new-key") for store in stores)
        )
        assert sum(results) == 1
        for store in stores:
            rows = await store.records()
            assert [row["key"] for row in rows] == [
                "old-failure",
                "old-claim",
                "new-key",
            ]
            assert rows[0]["time_precision"] == "second"
    finally:
        for store in stores:
            await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denied_action", [sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE]
)
async def test_failed_migration_rolls_back_the_entire_old_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied_action: int,
) -> None:
    """A database-level failure after copying cannot erase rows or old schema."""
    path = tmp_path / "failed-migration.db"
    create_legacy_database(path)
    connect = sqlite3.connect
    with connect(path) as connection:
        old_rows = connection.execute(
            "SELECT rowid, * FROM desktop_messages ORDER BY rowid"
        ).fetchall()

    def connect_with_migration_failure(
        database: str,
        *,
        timeout: float,
        check_same_thread: bool,
    ) -> sqlite3.Connection:
        """Inject an SQLite authorization error at a late migration statement."""
        connection = connect(
            database, timeout=timeout, check_same_thread=check_same_thread
        )

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database_name: str | None,
            trigger: str | None,
        ) -> int:
            """Deny only the selected DDL operation in this temporary test DB."""
            return sqlite3.SQLITE_DENY if action == denied_action else sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect_with_migration_failure)
        store = SQLiteStore(path)
        try:
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                await store.records()
        finally:
            await store.aclose()

    with connect(path) as connection:
        assert (
            connection.execute(
                "SELECT rowid, * FROM desktop_messages ORDER BY rowid"
            ).fetchall()
            == old_rows
        )
        columns = connection.execute("PRAGMA table_info(desktop_messages)").fetchall()
        assert next(column for column in columns if column[1] == "ts")[3] == 1
        assert "time_precision" not in {column[1] for column in columns}
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='desktop_messages_migration'"
            ).fetchall()
            == []
        )

    recovered = SQLiteStore(path)
    try:
        assert not await recovered.claim(message(), "old-failure")
        assert [row["key"] for row in await recovered.records()] == [
            "old-failure",
            "old-claim",
        ]
    finally:
        await recovered.aclose()


@pytest.mark.asyncio
async def test_store_protocol_preserves_all_input_and_result_fields(
    tmp_path: Path,
) -> None:
    """Commit before returning claim and retain complete local reply/error data."""
    store: StoreProtocol = SQLiteStore(tmp_path / "nested" / "messages.db")
    msg = message()
    try:
        assert await store.claim(msg, "message-key")
        claimed = (await store.records())[0]
        assert claimed["status"] == "claimed"
        assert all(claimed[field] == value for field, value in msg.model_dump().items())
        await store.finish(
            "message-key",
            "send_failed",
            reply="完整回复\n第二行",
            error="WeComAPIError (errcode=40003)",
        )
        finished = (await store.records())[0]
        assert finished["key"] == "message-key"
        assert finished["status"] == "send_failed"
        assert finished["reply"] == "完整回复\n第二行"
        assert finished["error"] == "WeComAPIError (errcode=40003)"
        assert not await store.claim(msg, "message-key")
        assert await store.records() == [finished]
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["claimed", "dry_run", "send_failed"])
async def test_claim_remains_deduplicated_after_reopening(
    tmp_path: Path, status: str
) -> None:
    """Neither incomplete work nor a failed send becomes automatically retryable."""
    path = tmp_path / "messages.db"
    first = SQLiteStore(path)
    try:
        assert await first.claim(message(), "restart-key")
        if status != "claimed":
            await first.finish("restart-key", status, reply="stored reply")
    finally:
        await first.aclose()

    second = SQLiteStore(path)
    try:
        assert not await second.claim(message(), "restart-key")
        rows = await second.records()
        assert len(rows) == 1
        assert rows[0]["status"] == status
    finally:
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("instances", [1, 2])
async def test_concurrent_claims_have_one_winner(
    tmp_path: Path, instances: int
) -> None:
    """Use database uniqueness for separate connections as well as one async lock."""
    stores = [SQLiteStore(tmp_path / "race.db") for _ in range(instances)]
    try:
        results = await asyncio.gather(
            *(
                stores[index % instances].claim(message(), "same-key")
                for index in range(20)
            )
        )
        assert sum(results) == 1
        assert len(await stores[0].records()) == 1
    finally:
        for store in stores:
            await store.aclose()


@pytest.mark.asyncio
async def test_invalid_updates_cannot_create_or_silently_lose_claims(
    tmp_path: Path,
) -> None:
    """Expose caller mistakes while keeping the persistence invariant intact."""
    store = SQLiteStore(tmp_path / "messages.db")
    try:
        with pytest.raises(ValueError):
            await store.claim(message(), "")
        with pytest.raises(KeyError):
            await store.finish("unknown-key", "sent")
        assert await store.claim(message(), "valid-key")
        with pytest.raises(ValueError):
            await store.finish("valid-key", " ")
        assert (await store.records())[0]["status"] == "claimed"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_closed_store_is_idempotent_and_rejects_new_work(tmp_path: Path) -> None:
    """Make lifecycle errors explicit without reopening a disposed connection."""
    store = SQLiteStore(tmp_path / "messages.db")
    await store.claim(message(), "claimed-key")
    await store.aclose()
    await store.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await store.claim(message(), "another-key")
    with pytest.raises(RuntimeError, match="closed"):
        await store.records()


@pytest.mark.asyncio
async def test_cancelled_claim_finishes_before_connection_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker thread may commit after cancellation; retain the lock until it stops."""
    path = tmp_path / "cancel.db"
    store = SQLiteStore(path)
    started = threading.Event()
    released = threading.Event()
    original_connect = store._connect

    def blocked_connect() -> sqlite3.Connection:
        """Make the in-flight database operation deterministic for cancellation."""
        started.set()
        if not released.wait(timeout=2):
            raise TimeoutError("test did not release database worker")
        return original_connect()

    monkeypatch.setattr(store, "_connect", blocked_connect)
    claim = asyncio.create_task(store.claim(message(), "cancelled-key"))
    assert await asyncio.to_thread(started.wait, 1)
    claim.cancel()
    closing = asyncio.create_task(store.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await claim
    await closing

    reopened = SQLiteStore(path)
    try:
        assert not await reopened.claim(message(), "cancelled-key")
        assert (await reopened.records())[0]["status"] == "claimed"
    finally:
        await reopened.aclose()
