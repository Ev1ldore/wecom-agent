"""User-managed local allowlists and scoped memory/document administration."""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol


class WhitelistProtocol(Protocol):
    def read(self) -> dict:
        """Read a configuration with a validated exact-match whitelist."""
        raise NotImplementedError

    def change(self, conversation: str, sender: str, *, remove: bool = False) -> list:
        """Atomically add or remove one exact local event identity."""
        raise NotImplementedError


def identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("会话与发送者必须为1至200字符的非空字符串")
    if value != value.strip() or value == "*":
        raise ValueError("身份不得包含首尾空白，也不接受通配符 *")
    return value


def whitelist(config):
    if not isinstance(config, dict) or not isinstance(config.get("whitelist"), list):
        raise ValueError("配置必须包含 whitelist 数组；空数组表示不允许任何会话")  # noqa: TRY004 -- Public config validation uses ValueError.
    result = []
    for item in config["whitelist"]:
        if not isinstance(item, dict) or set(item) != {"conversation", "sender"}:
            raise ValueError("白名单每项必须仅包含 conversation 和 sender")
        pair = (identity(item["conversation"]), identity(item["sender"]))
        if pair not in result:
            result.append(pair)
    return result


class WhitelistFile:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        data = json.loads(self.path.read_text())
        whitelist(data)
        return data

    @staticmethod
    def revision(config):
        raw = json.dumps(config, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()

    def update(self, transform, *, revision=None):
        with Path(str(self.path) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = self.read()
            if revision is not None and self.revision(config) != revision:
                raise ValueError("配置已被其他窗口修改，请刷新后重试")
            transform(config)
            whitelist(config)
            name = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.path.parent, delete=False
                ) as stream:
                    name = stream.name
                    json.dump(config, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, self.path)
                name = None
            finally:
                if name is not None:
                    Path(name).unlink(missing_ok=True)
            return config

    def change(self, conversation, sender, *, remove=False):
        pair = (identity(conversation), identity(sender))

        def transform(config):
            pairs = whitelist(config)
            if remove:
                pairs = [item for item in pairs if item != pair]
            elif pair not in pairs:
                pairs.append(pair)
            config["whitelist"] = [{"conversation": c, "sender": s} for c, s in pairs]

        return self.update(transform)["whitelist"]


class ManagementProtocol(Protocol):
    def memories(self, scope: str) -> list:
        """List active explicit memories for one authorized scope."""
        raise NotImplementedError

    def set_memory(
        self, scope: str, key: str, value: str, ttl: int | None = None
    ) -> None:
        """Persist one explicit memory with an optional lifetime in seconds."""
        raise NotImplementedError

    def forget_memory(self, scope: str, key: str) -> int:
        """Remove one scoped explicit memory, preserving audit history."""
        raise NotImplementedError

    def short_term(self, scope: str) -> dict:
        """Inspect bounded, nonexpired context for one allowed conversation."""
        raise NotImplementedError

    def reset_short_term(self, scope: str) -> None:
        """Start a new context without removing explicit memory or audit."""
        raise NotImplementedError

    def documents(self, scope: str) -> list:
        """List parsed document metadata for one authorized scope."""
        raise NotImplementedError

    def forget_document(self, scope: str, document_id: str) -> int:
        """Remove one retrieval document, preserving its original local file."""
        raise NotImplementedError


class Management:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def authorize(self, scope):
        pair = json.loads(scope)
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("无效会话身份")
        if tuple(pair) not in self.engine.allowed:
            raise ValueError("该会话不在当前白名单中")

    def memories(self, scope):
        self.authorize(scope)
        return self.store.rows(
            "SELECT key,value,source,expires FROM memory WHERE scope=? "
            "AND (expires IS NULL OR expires>?) ORDER BY key",
            (scope, time.time()),
        )

    def set_memory(self, scope, key, value, ttl=None):
        self.authorize(scope)
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            raise ValueError("记忆名称必须为1至40字符")
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise ValueError("记忆值必须为1至300字符")
        if ttl is not None and (type(ttl) is not int or not 0 < ttl <= 31536000):
            raise ValueError("有效期必须为1至31536000秒")
        with self.store.lock:
            self.store.execute(
                "INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?)",
                (
                    scope,
                    key.strip(),
                    value.strip(),
                    "manual:local",
                    None if ttl is None else time.time() + ttl,
                ),
            )
            self.invalidate(scope)

    def invalidate(self, scope):
        self.store.execute(
            "UPDATE sessions SET revision=revision+1 WHERE scope=?", (scope,)
        )

    def forget_memory(self, scope, key):
        self.authorize(scope)
        if not isinstance(key, str) or not key.strip():
            raise ValueError("必须提供记忆名称")
        with self.store.lock:
            count = self.store.execute(
                "DELETE FROM memory WHERE scope=? AND key=?", (scope, key.strip())
            ).rowcount
            self.invalidate(scope)
            return count

    def short_term(self, scope):
        self.authorize(scope)
        config = getattr(self.engine, "config", {})
        context = self.store.context(
            scope,
            config.get("history_window", 12),
            ttl_seconds=config.get("short_term_ttl_seconds", 1800),
            max_chars=config.get("short_term_max_chars", 12000),
        )
        return {key: context[key] for key in ("recent", "older_excerpts")}

    def reset_short_term(self, scope):
        self.authorize(scope)
        self.store.reset_context(scope)

    def documents(self, scope):
        self.authorize(scope)
        return self.store.rows(
            "SELECT id,name,evidence FROM documents WHERE scope=? ORDER BY rowid",
            (scope,),
        )

    def forget_document(self, scope, document_id):
        self.authorize(scope)
        with self.store.lock:
            count = self.store.execute(
                "DELETE FROM documents WHERE scope=? AND id=?", (scope, document_id)
            ).rowcount
            self.invalidate(scope)
            return count
