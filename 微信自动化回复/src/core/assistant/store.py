import json
import sqlite3
import threading
import time
from typing import Protocol


class MemoryStoreProtocol(Protocol):
    def context(self, scope: str, window: int = 12) -> dict:
        """Read isolated persistent memory and bounded history."""
        raise NotImplementedError

    def execute(self, sql: str, args: tuple = ()):
        """Execute parameterized local state changes."""
        raise NotImplementedError

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        """Read local state with no external side effect."""
        raise NotImplementedError


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(scope TEXT PRIMARY KEY, revision INTEGER DEFAULT 0,
          paused INTEGER DEFAULT 0, reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, scope TEXT, payload TEXT,
          status TEXT, detail TEXT DEFAULT '', created REAL);
        CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY, scope TEXT, role TEXT,
          text TEXT, source TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS memory(scope TEXT, key TEXT, value TEXT, source TEXT,
          expires REAL, PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, scope TEXT, name TEXT,
          content TEXT, evidence TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, scope TEXT, text TEXT,
          intent TEXT, evidence TEXT, status TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, text TEXT, created REAL);
        """)
        self.db.commit()

    def execute(self, sql, args=()):
        with self.lock:
            result = self.db.execute(sql, args)
            self.db.commit()
            return result

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(x) for x in self.db.execute(sql, args).fetchall()]

    def pause(self, scope, reason):
        self.execute(
            "INSERT INTO sessions(scope,paused,reason) VALUES(?,1,?) "
            "ON CONFLICT(scope) DO UPDATE SET paused=1,reason=excluded.reason",
            (scope, reason),
        )

    def recover(self):
        with self.lock:
            for row in self.rows("SELECT * FROM outbox WHERE status='sending'"):
                self.execute(
                    "UPDATE outbox SET status='unknown' WHERE id=?", (row["id"],)
                )
                self.execute(
                    "UPDATE messages SET status='unknown' WHERE id=?", (row["id"],)
                )
                self.pause(row["scope"], "发送中断，需核查；不会自动重发")
            self.execute(
                "UPDATE messages SET status='pending' WHERE status='processing'"
            )

    def context(self, scope, window=12):
        history = self.rows(
            "SELECT role,text,source FROM history WHERE scope=? ORDER BY seq", (scope,)
        )
        old, recent = history[:-window], history[-window:]
        # Extractive summary preserves provenance, does not invent semantic facts.
        summary = [
            {"source": x["source"], "role": x["role"], "excerpt": x["text"][:240]}
            for x in old[-20:]
        ]
        memory = self.rows(
            "SELECT key,value,source FROM memory WHERE scope=? AND "
            "(expires IS NULL OR expires>?)",
            (scope, time.time()),
        )
        return {"recent": recent, "older_excerpts": summary, "memory": memory}

    def dump(self):
        return {
            name: self.rows(f"SELECT * FROM {name}")
            for name in (
                "sessions",
                "messages",
                "memory",
                "documents",
                "outbox",
                "receipts",
            )
        }

    def close(self):
        self.db.close()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
