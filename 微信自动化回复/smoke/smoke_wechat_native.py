"""Exercise the built-in screenshot receive-to-send path with all UI/LLM mocked."""

import base64
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

import httpx

from adapters.personal_tail import PersonalTailInbox
from adapters.wechat_native import IDENTITY, NativeRiChat
from adapters.wechat_ocr import OCRObservation as O
from adapters.wechat_ocr import WeChatWindow
from src.core.desktop_llm import HTTPReplyModel
from src.core.desktop_loop import AgentLoop, message_key
from src.core.desktop_store import SQLiteStore


class NativeSmokeProtocol(Protocol):
    """Expose the offline native pipeline smoke operation."""

    async def __call__(self) -> None:
        raise NotImplementedError


async def run_native_smoke() -> None:
    """Prove fresh input, model HTTP, one fake send and restart suppression."""
    draft = "离线测试答复"
    window = WeChatWindow(1, 2, 436, 158, 598, 640)
    baseline = [
        O("Ri", 0.025, 0.077, 0.04, 0.02),
        O("18:36", 0.47, 0.2, 0.06, 0.02),
        O("旧消息", 0.13, 0.3, 0.12, 0.025),
        O("发送", 0.9, 0.943, 0.05, 0.025),
    ]
    new = [*baseline, O("新问题", 0.13, 0.45, 0.12, 0.025)]
    typed = [*new, O(draft, 0.04, 0.81, 0.4, 0.03)]
    after = [*new, O(draft, 0.65, 0.65, 0.23, 0.025, bubble_side="self")]
    views = {b"base": baseline, b"new": new, b"type": typed, b"after": after}
    sequence = iter([b"base", b"new", b"new", b"type", b"after", b"after", b"after"])
    operations = []
    requests = []

    class Backend:
        async def request(self, op, *, window=None, text=""):
            operations.append(op)
            result = {"window": asdict(WeChatWindow(1, 2, 436, 158, 598, 640))}
            if op == "capture":
                result["png"] = base64.b64encode(next(sequence)).decode()
            return result

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": draft}}]})

    chat = NativeRiChat(
        Backend(), live=True, recognizer=lambda p: views[p], color_marker=lambda p, r: r
    )
    assert window == WeChatWindow(1, 2, 436, 158, 598, 640)
    with tempfile.TemporaryDirectory() as folder:
        store = SQLiteStore(Path(folder) / "native.db")
        try:
            tail = PersonalTailInbox(chat, key_of=message_key)
            for msg in await tail.baseline():
                key = message_key(msg)
                assert await store.claim(msg, key)
                await store.finish(key, "baseline")
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
                HTTPReplyModel(
                    "https://mock.invalid/v1",
                    "fake",
                    "fake",
                    template_name="wechat_ocr_reply.txt",
                    http_client=http,
                ) as model,
            ):
                loop = AgentLoop(
                    tail,
                    model,
                    chat,
                    store,
                    allowed_chat=IDENTITY,
                    allowed_sender=IDENTITY,
                    self_senders=frozenset({"desktop:self"}),
                    dry_run=False,
                )
                assert (await loop.run_once())["sent"] == 0
                assert (await loop.run_once())["sent"] == 1
                assert (await loop.run_once())["filtered"] == 1
                restarted = PersonalTailInbox(chat, key_of=message_key)
                assert len(await restarted.baseline()) == 3
                assert await restarted.poll_unread() == []
            assert len(requests) == 1
            assert operations.count("type") == operations.count("send") == 1
            assert [r["status"] for r in await store.records()] == ["baseline", "sent"]
        finally:
            await store.aclose()
    print(
        "native wx2 smoke: PASS (mock screenshots + mock LLM HTTP + real SQLite; "
        "new input only; one fake send; own-message filter; restart baseline; real sends=0)"
    )
