"""Persist desktop message claims before any model call or outbound operation."""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

from adapters.inbox import Msg

T = TypeVar("T")


class StoreProtocol(Protocol):
    """Define durable claim and outcome operations consumed by the agent loop."""

    async def claim(self, msg: Msg, key: str) -> bool:
        """Persist a first claim, returning False for any previously claimed key."""
        raise NotImplementedError

    async def finish(
        self, key: str, status: str, reply: str = "", error: str = ""
    ) -> None:
        """Record the completed local result without releasing the claim."""
        raise NotImplementedError

    async def records(self) -> list[dict[str, Any]]:
        """Return stored messages and results in original claim order."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release local database resources."""
        raise NotImplementedError


class SQLiteStore:
    """Use a unique SQLite key to prevent retries across processes and restarts.

    A claim stays claimed even when interrupted or failed. The loop never
    automatically replays incomplete or failed records into an external send.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Keep a lazy connection and serialize its worker-thread operations."""
        self._db_path: str = str(db_path)
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._closed: bool = False

    def _connect(self) -> sqlite3.Connection:
        """Open and initialize the schema inside the serialized worker thread."""
        if self._connection is None:
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self._db_path, timeout=5.0, check_same_thread=False
            )
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._initialize_schema(connection)
            except BaseException:
                connection.close()
                raise
            self._connection = connection
        return self._connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        """Upgrade within one write transaction, keeping original claim rowids."""
        schema = """(
            key TEXT PRIMARY KEY,
            chat TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            ts INTEGER,
            time_precision TEXT NOT NULL DEFAULT 'second',
            time_display TEXT,
            chat_type TEXT NOT NULL,
            is_self INTEGER NOT NULL,
            status TEXT NOT NULL,
            reply TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )"""
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(desktop_messages)")
        }
        if not columns:
            connection.execute(f"CREATE TABLE desktop_messages {schema}")
            return
        if not columns["ts"]["notnull"] and {"time_precision", "time_display"}.issubset(
            columns
        ):
            return

        connection.execute(f"CREATE TABLE desktop_messages_migration {schema}")
        precision = "time_precision" if "time_precision" in columns else "'second'"
        display = "time_display" if "time_display" in columns else "NULL"
        connection.execute(
            f"""INSERT INTO desktop_messages_migration
            (rowid, key, chat, sender, content, ts, time_precision, time_display,
             chat_type, is_self, status, reply, error, created_at, updated_at)
            SELECT rowid, key, chat, sender, content, ts, {precision}, {display},
                   chat_type, is_self, status, reply, error, created_at, updated_at
            FROM desktop_messages ORDER BY rowid"""
        )
        connection.execute("DROP TABLE desktop_messages")
        connection.execute(
            "ALTER TABLE desktop_messages_migration RENAME TO desktop_messages"
        )

    async def _run(self, operation: Callable[[], T]) -> T:
        """Keep the connection lock until a cancelled worker operation finishes."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("SQLiteStore is closed")
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                finally:
                    raise

    async def claim(self, msg: Msg, key: str) -> bool:
        """Commit an atomic first claim with every normalized input field."""
        if not key:
            raise ValueError("A non-empty message key is required")

        def insert() -> bool:
            """Let SQLite uniqueness arbitrate concurrent store instances."""
            connection = self._connect()
            now = int(time.time())
            with connection:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO desktop_messages
                    (key, chat, sender, content, ts, time_precision, time_display,
                     chat_type, is_self,
                     status, reply, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', '', '', ?, ?)""",
                    (
                        key,
                        msg.chat,
                        msg.sender,
                        msg.content,
                        msg.ts,
                        msg.time_precision,
                        msg.time_display,
                        msg.chat_type,
                        int(msg.is_self),
                        now,
                        now,
                    ),
                )
            return cursor.rowcount == 1

        return await self._run(insert)

    async def finish(
        self, key: str, status: str, reply: str = "", error: str = ""
    ) -> None:
        """Persist the complete reply and safe error while retaining the unique key."""
        if not status.strip():
            raise ValueError("A non-empty outcome status is required")

        def update() -> None:
            """Refuse silently updating a missing claim."""
            connection = self._connect()
            with connection:
                cursor = connection.execute(
                    """UPDATE desktop_messages
                    SET status=?, reply=?, error=?, updated_at=? WHERE key=?""",
                    (status, reply, error, int(time.time()), key),
                )
                if cursor.rowcount != 1:
                    raise KeyError("No persisted claim for this message key")

        await self._run(update)

    async def records(self) -> list[dict[str, Any]]:
        """Read every input and outcome without altering processing eligibility."""

        def read() -> list[dict[str, Any]]:
            """Use rowid to keep deterministic ordering for equal creation seconds."""
            rows = (
                self._connect()
                .execute("SELECT * FROM desktop_messages ORDER BY rowid")
                .fetchall()
            )
            return [{**dict(row), "is_self": bool(row["is_self"])} for row in rows]

        return await self._run(read)

    async def aclose(self) -> None:
        """Close once after any outstanding database worker finishes."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                task = asyncio.create_task(asyncio.to_thread(self._connection.close))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    finally:
                        raise
                self._connection = None
