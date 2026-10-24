import json
import sqlite3
import threading
import time
from typing import Protocol


class MemoryStoreProtocol(Protocol):
    def context(
        self,
        scope: str,
        window: int = 12,
        *,
        ttl_seconds: int = 1800,
        max_chars: int = 12000,
    ) -> dict:
        """Read isolated persistent memory and bounded history."""
        raise NotImplementedError

    def reset_context(self, scope: str) -> None:
        """Exclude prior history without deleting audit or explicit memory."""
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
        CREATE TABLE IF NOT EXISTS context_resets(scope TEXT PRIMARY KEY, after_seq INTEGER);
        CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY, scope TEXT, role TEXT,
          text TEXT, source TEXT, created REAL);
        CREATE INDEX IF NOT EXISTS history_scope_seq ON history(scope,seq);
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

    def reset_context(self, scope):
        with self.lock, self.db:
            boundary = self.db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM history WHERE scope=?", (scope,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO context_resets VALUES(?,?) ON CONFLICT(scope) "
                "DO UPDATE SET after_seq=excluded.after_seq",
                (scope, boundary),
            )
            self.db.execute(
                "UPDATE sessions SET revision=revision+1 WHERE scope=?", (scope,)
            )

    def context(self, scope, window=12, *, ttl_seconds=1800, max_chars=12000):
        if any(type(v) is not int for v in (window, ttl_seconds, max_chars)) or not (
            1 <= window <= 100
            and 60 <= ttl_seconds <= 86400
            and 1000 <= max_chars <= 64000
        ):
            raise ValueError("短期记忆参数超出范围")
        with self.lock:
            history = self.rows(
                "SELECT role,text,source FROM history WHERE scope=? AND created>? "
                "AND seq>COALESCE((SELECT after_seq FROM context_resets WHERE scope=?),0) "
                "ORDER BY seq DESC LIMIT ?",
                (scope, time.time() - ttl_seconds, scope, window + 20),
            )
            # Newest complete entries take priority; truncation is explicit.
            recent, summary, remaining = [], [], max_chars
            for index, item in enumerate(history):
                if remaining <= 0:
                    break
                limit = min(remaining, 1000 if index < window else 240)
                text = item["text"][:limit]
                remaining -= len(text)
                if index < window:
                    recent.append(
                        {
                            **item,
                            "text": text,
                            "truncated": len(text) < len(item["text"]),
                        }
                    )
                else:
                    summary.append(
                        {
                            "source": item["source"],
                            "role": item["role"],
                            "excerpt": text,
                        }
                    )
            memory = self.rows(
                "SELECT key,value,source FROM memory WHERE scope=? AND "
                "(expires IS NULL OR expires>?)",
                (scope, time.time()),
            )
        return {
            "recent": recent[::-1],
            "older_excerpts": summary[::-1],
            "memory": memory,
        }

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
