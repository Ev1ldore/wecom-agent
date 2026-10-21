"""Bridge the existing AgentLoop to shared assistant logic without live sends."""

import asyncio
import json
from pathlib import Path
from typing import Protocol

from adapters.inbox import Msg
from src.core.desktop_llm import ReplyModel

from .engine import Engine
from .media import Media
from .model import validate
from .store import Store


class DesktopAssistantProtocol(Protocol):
    async def reply_message(self, msg: Msg, key: str) -> str | None:
        """Produce contextual drafts for existing normalized desktop messages."""
        raise NotImplementedError

    async def observe_self(self, msg: Msg) -> None:
        """Pause only this verified peer's assistant session."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Close the isolated assistant state after active processing finishes."""
        raise NotImplementedError


class AsyncModelBridge:
    """Reuse HTTPReplyModel's timeout, one retry and safe transport logging."""

    def __init__(self, model: ReplyModel, loop: asyncio.AbstractEventLoop):
        self.model, self.loop = model, loop

    def generate(self, context: dict) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            self.model.reply(json.dumps(context, ensure_ascii=False)), self.loop
        )
        try:
            raw = future.result(timeout=23)
            return validate(json.loads(raw), context)
        except BaseException:
            future.cancel()
            raise


class DesktopAssistant:
    """Keep existing claim keys, timestamps, baseline and send guards unchanged.

    The sidecar DB owns only assistant memory/drafts. AgentLoop remains the owner
    of original desktop claims and dry-run destination checks. This bridge is
    explicitly not usable with the live transport branch.
    """

    def __init__(self, model: ReplyModel, path: Path, *, chat: str, sender: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.chat, self.sender = chat, sender
        self.store = Store(path)
        config = {
            "whitelist": [{"conversation": chat, "sender": sender}],
            "spam_terms": ["刷单返利", "博彩送彩金"],
        }
        self.engine = Engine(
            self.store,
            config,
            AsyncModelBridge(model, asyncio.get_running_loop()),
            Media(path.parent, {}),
        )
        self.lock = asyncio.Lock()

    async def _run(self, operation):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Never close a DB while its worker is still writing draft state.
                try:
                    await task
                finally:
                    raise

    async def reply_message(self, msg: Msg, key: str) -> str | None:
        if (
            msg.chat != self.chat
            or msg.sender != self.sender
            or msg.is_self
            or msg.chat_type != "single"
        ):
            return None

        def process():
            event = {
                "id": key,
                "conversation": msg.chat,
                "sender": msg.sender,
                "kind": "text",
                "text": msg.content,
            }
            status = self.engine.ingest(event)
            if status != "pending":
                return None
            self.engine.drain()
            row = self.store.rows(
                "SELECT id,status FROM messages WHERE payload=?",
                (json.dumps(event, ensure_ascii=False, sort_keys=True),),
            )[0]
            if row["status"] == "failed":
                raise ValueError("增强回复生成失败；未发送")
            results = self.store.rows(
                "SELECT text FROM outbox WHERE id=? AND status='simulated'",
                (row["id"],),
            )
            return results[0]["text"] if results else None

        return await self._run(process)

    async def observe_self(self, msg: Msg) -> None:
        if msg.chat == self.chat and msg.is_self:
            await self._run(
                lambda: self.store.pause(
                    json.dumps(
                        [self.chat, self.sender], ensure_ascii=False, sort_keys=True
                    ),
                    "人工接管",
                )
            )

    async def aclose(self) -> None:
        await self._run(self.store.close)
