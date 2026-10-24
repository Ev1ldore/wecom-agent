"""Local MCP management operations; no desktop, model or message transport."""

import fcntl
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from .contact_book import ContactBook
from .management import Management, whitelist
from .store import Store, encoded

ContactId = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{24}$")]
MemoryKey = Annotated[str, Field(strict=True, min_length=1, max_length=40)]
MemoryValue = Annotated[str, Field(strict=True, min_length=1, max_length=300)]


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContactInput(EmptyInput):
    contact_id: ContactId


class KeyInput(ContactInput):
    key: MemoryKey


class SetInput(KeyInput):
    value: MemoryValue
    ttl_seconds: Annotated[StrictInt, Field(ge=1, le=31536000)] | None = None


# Ordered definitions keep tools/list deterministic.
DEFINITIONS = {
    "contacts_list": (EmptyInput, True, "列出手动登记的联系人和勾选状态"),
    "session_context_get": (ContactInput, True, "读取指定已授权联系人的短期上下文"),
    "session_context_reset": (
        ContactInput,
        False,
        "重置短期上下文，保留审计与长期记忆",
    ),
    "memory_list": (ContactInput, True, "读取指定已授权联系人的显式长期记忆"),
    "memory_set": (SetInput, False, "设置指定已授权联系人的显式长期记忆"),
    "memory_forget": (KeyInput, False, "遗忘指定已授权联系人的一项长期记忆"),
    "documents_list": (
        ContactInput,
        True,
        "列出指定已授权联系人的附件元数据，不读取全文",
    ),
}
ERRORS = {
    "INVALID_ARGUMENT": "工具名或参数无效",
    "NOT_FOUND": "联系人不存在",
    "NOT_ALLOWED": "该联系人未获授权",
    "BUSY": "配置或数据库正被占用，请稍后重试",
    "STORE_UNAVAILABLE": "本地配置或数据库不可用",
    "TIMEOUT": "工具执行超时，写入结果可能未知，请先读取核对",
}


class ToolFailure(ValueError):
    def __init__(self, code: str):
        self.code = code if code in ERRORS else "STORE_UNAVAILABLE"
        super().__init__(ERRORS[self.code])


class ToolBackendProtocol(Protocol):
    def call(self, name: str, arguments: dict) -> dict:
        """Apply one allowlisted local operation without retries."""
        raise NotImplementedError


def validate_arguments(name, arguments):
    if name not in DEFINITIONS or not isinstance(arguments, dict):
        raise ToolFailure("INVALID_ARGUMENT")
    try:
        return DEFINITIONS[name][0].model_validate(arguments).model_dump()
    except ValidationError:
        raise ToolFailure("INVALID_ARGUMENT") from None


class LocalTools:
    def __init__(self, config: Path, database: Path):
        self.book, self.database = ContactBook(config), Path(database)

    def call(self, name, arguments):
        args = validate_arguments(name, arguments)
        try:
            # Match WhitelistFile's writer lock so authorization stays stable
            # until this one operation commits. Never wait indefinitely.
            with Path(str(self.book.file.path) + ".lock").open("a") as config_lock:
                fcntl.flock(config_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                config = self.book.file.read()
                contacts = self.book.catalog(config)
                allowed = set(whitelist(config))
                if name == "contacts_list":
                    return {
                        "contacts": [
                            {
                                "id": c["id"],
                                "name": c["name"],
                                "selected": (c["conversation"], c["sender"]) in allowed,
                            }
                            for c in contacts
                        ]
                    }
                contact = next(
                    (c for c in contacts if c["id"] == args["contact_id"]), None
                )
                if contact is None:
                    raise ToolFailure("NOT_FOUND")
                pair = (contact["conversation"], contact["sender"])
                if pair not in allowed:
                    raise ToolFailure("NOT_ALLOWED")
                self.database.parent.mkdir(parents=True, exist_ok=True)
                with Path(str(self.database) + ".lock").open("a") as db_lock:
                    fcntl.flock(db_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    store = Store(self.database)
                    try:
                        manager = Management(
                            SimpleNamespace(store=store, allowed=allowed, config=config)
                        )
                        return self._dispatch(manager, encoded(pair), name, args)
                    finally:
                        store.close()
        except ToolFailure:
            raise
        except BlockingIOError:
            raise ToolFailure("BUSY") from None
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise ToolFailure("STORE_UNAVAILABLE") from None

    @staticmethod
    def _dispatch(manager, scope, name, args):
        if name == "session_context_get":
            return manager.short_term(scope)
        if name == "session_context_reset":
            manager.reset_short_term(scope)
            return {"reset": True}
        if name == "memory_list":
            return {"memory": manager.memories(scope)}
        if name == "memory_set":
            if not args["key"].strip() or not args["value"].strip():
                raise ToolFailure("INVALID_ARGUMENT")
            manager.set_memory(scope, args["key"], args["value"], args["ttl_seconds"])
            return {"saved": True}
        if name == "memory_forget":
            if not args["key"].strip():
                raise ToolFailure("INVALID_ARGUMENT")
            return {"removed": manager.forget_memory(scope, args["key"])}
        if name == "documents_list":
            return {
                "documents": [
                    {**doc, "name": Path(doc["name"]).name}
                    for doc in manager.documents(scope)
                ]
            }
        raise ToolFailure("INVALID_ARGUMENT")


def worker_main():
    """Single trusted child process; stdout holds only its bounded result."""
    try:
        raw = sys.stdin.buffer.read(100_001)
        if len(raw) > 100_000:
            raise ToolFailure("INVALID_ARGUMENT")
        payload = json.loads(raw)
        data = LocalTools(Path(sys.argv[1]), Path(sys.argv[2])).call(
            payload["name"], payload["arguments"]
        )
        result = {"ok": True, "data": data}
        if len(encoded(result).encode()) > 1_000_000:
            raise ToolFailure("STORE_UNAVAILABLE")
    except ToolFailure as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    except Exception:  # noqa: BLE001 -- Never expose raw local errors over MCP.
        result = {
            "ok": False,
            "error": {
                "code": "STORE_UNAVAILABLE",
                "message": ERRORS["STORE_UNAVAILABLE"],
            },
        }
    print(encoded(result), flush=True)


if __name__ == "__main__":
    worker_main()
