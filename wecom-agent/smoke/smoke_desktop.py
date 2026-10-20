"""Smoke-test desktop filtering, mocked LLM HTTP and durable dry-run replies."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import httpx

from adapters.inbox import Inbox, Msg, application_chat
from src.core.desktop_llm import HTTPReplyModel
from src.core.desktop_loop import AgentLoop, message_key
from src.core.desktop_store import SQLiteStore

DEMO_USER_ID = "LiChengYun"
DEMO_AGENT_ID = 1000001
DEMO_CONTENT = "这是一条离线 Demo 测试消息，不包含实际收件箱内容。"
DEMO_REPLY = "离线 Demo 模拟回复：已收到测试问题。\n这是完整的第二行测试回复。"


class DesktopSmokeProtocol(Protocol):
    """Define an offline desktop smoke operation reusable by CLI entry points."""

    async def __call__(self) -> None:
        """Verify local dry-run behavior without a desktop or real external calls."""
        raise NotImplementedError


class DemoInbox:
    """Return only explicitly supplied synthetic messages, without desktop access."""

    def __init__(self, messages: list[Msg]) -> None:
        """Keep the fixed smoke samples and count local polling calls."""
        self._messages = messages
        self.polls: int = 0

    async def poll_unread(self) -> list[Msg]:
        """Return a fresh sample list without consulting the operating system."""
        self.polls += 1
        return list(self._messages)


class NoSendSender:
    """Make any attempted chat-send operation an immediately observable failure."""

    def __init__(self) -> None:
        """Start with no outbound calls; no network client is created."""
        self.calls: int = 0

    async def send_text(self, user_id: str, content: str) -> None:
        """Count and reject every send instead of contacting a chat service."""
        self.calls += 1
        raise AssertionError("Desktop smoke must never invoke an outbound sender")


async def run_desktop_smoke() -> None:
    """Verify six synthetic messages, one model request and durable restart dedup."""
    chat = application_chat(DEMO_AGENT_ID, DEMO_USER_ID)
    legal = Msg(
        chat=chat,
        sender=DEMO_USER_ID,
        content=DEMO_CONTENT,
        ts=1700000001,
    )
    samples = [
        legal,
        legal.model_copy(update={"ts": 1700000002}),
        legal.model_copy(update={"sender": f"agent:{DEMO_AGENT_ID}", "is_self": True}),
        legal.model_copy(update={"sender": "demo-other-member"}),
        legal.model_copy(update={"chat_type": "group"}),
        legal.model_copy(
            update={"chat": application_chat(DEMO_AGENT_ID + 1, DEMO_USER_ID)}
        ),
    ]
    sender = NoSendSender()
    emitted: list[str] = []
    http_calls = 0

    def model_http(request: httpx.Request) -> httpx.Response:
        """Exercise the real HTTP adapter through an in-process transport only."""
        nonlocal http_calls
        http_calls += 1
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["messages"][1] == {"role": "user", "content": DEMO_CONTENT}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": DEMO_REPLY}}]}
        )

    with TemporaryDirectory(prefix="wecom-desktop-smoke-") as directory:
        path = Path(directory, "desktop.db")
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(model_http)) as http,
            HTTPReplyModel(
                "https://desktop-smoke.invalid/v1",
                "desktop-smoke-fake-key",
                "desktop-smoke-fake-model",
                http_client=http,
            ) as llm,
        ):
            inbox: Inbox = DemoInbox(samples)
            store = SQLiteStore(path)
            try:
                loop = AgentLoop(
                    inbox,
                    llm,
                    sender,
                    store,
                    allowed_chat=chat,
                    allowed_sender=DEMO_USER_ID,
                    self_senders=frozenset({f"agent:{DEMO_AGENT_ID}"}),
                    dry_run=True,
                    emit=emitted.append,
                )
                stats = await loop.run_once()
                assert stats == {
                    "polled": 6,
                    "filtered": 4,
                    "duplicate": 1,
                    "dry_run": 1,
                    "sent": 0,
                    "llm_failed": 0,
                    "send_failed": 0,
                    "emit_failed": 0,
                    "deferred": 0,
                    "paused": False,
                }
                assert stats["filtered"] + stats["duplicate"] == 5
                assert http_calls == 1 and sender.calls == 0
                rows = await store.records()
                assert len(rows) == 1
                row = rows[0]
                assert all(
                    row[field] == value
                    for field, value in legal.model_dump(exclude={"dedupe_key"}).items()
                )
                assert row["key"] == message_key(legal)
                assert row["status"] == "dry_run"
                assert row["reply"] == DEMO_REPLY
                assert row["error"] == ""
                assert path.is_file()
            finally:
                await store.aclose()

            restarted = SQLiteStore(path)
            try:
                restarted_loop = AgentLoop(
                    DemoInbox([legal]),
                    llm,
                    sender,
                    restarted,
                    allowed_chat=chat,
                    allowed_sender=DEMO_USER_ID,
                    self_senders=frozenset({f"agent:{DEMO_AGENT_ID}"}),
                    dry_run=True,
                    emit=emitted.append,
                )
                repeated = await restarted_loop.run_once()
                assert repeated["polled"] == 1
                assert repeated["duplicate"] == 1
                assert (
                    repeated["filtered"] == repeated["dry_run"] == repeated["sent"] == 0
                )
                assert await restarted.records() == rows
                assert http_calls == 1 and sender.calls == 0
            finally:
                await restarted.aclose()

    assert emitted == [f"[DRY-RUN] 将发送至 {DEMO_USER_ID}：{DEMO_REPLY}"]
    print(f"Demo / fake inbox / mock LLM: {emitted[0]}")
    print(
        "desktop smoke: PASS (fake inbox; mock LLM HTTP; real SQLite; "
        "1 dry_run; 4 filtered + 1 duplicate; restart dedup; zero real sends)"
    )


def main() -> int:
    """Run the standalone offline demo and return zero only after every assertion."""
    runner: DesktopSmokeProtocol = run_desktop_smoke
    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
