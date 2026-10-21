"""Exercise encrypted callbacks and a smoke-only reply consumer locally.

The temporary consumer only replies to explicitly allowlisted smoke message IDs
and the requested user. Production callback routes and mappings are reused.
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing, asynccontextmanager, suppress
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol
from unittest.mock import patch
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI
from pydantic import ValidationError
from redis import asyncio as redis
from redis.exceptions import RedisError
from structlog.contextvars import bound_contextvars
from wechatpy.enterprise.crypto import WeChatCrypto
from wechatpy.utils import WeChatSigner

from smoke.smoke_api import SecretRedactor
from src.config import Settings, get_settings
from src.core.logging import get_logger
from src.core.models import WecomMessage
from src.core.queue import MessageQueue
from src.gateway import wecom_callback
from src.gateway.wecom_client import WeComAPIError, WecomClient, WecomClientProtocol
from src.main import health
from src.main import lifespan as service_lifespan

FALLBACK_TEXT = "暂不支持该消息类型，请发送文本"
logger = get_logger(__name__)


class ReplyQueueProtocol(Protocol):
    """Expose only the stream consumption required by the smoke reply worker."""

    def consume(
        self, group: str, consumer: str, batch: int = 10, block_ms: int = 5000
    ) -> AsyncGenerator[WecomMessage, None]:
        """Return a closable stream of normalized messages."""
        raise NotImplementedError


class SmokeLoopError(Exception):
    """Report a failed local smoke stage without exposing request credentials."""

    def __init__(self, stage: str, errmsg: str, errcode: int | None = None) -> None:
        """Retain the failed stage and any sanitized upstream error fields."""
        self.stage = stage
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"{stage}: {errmsg}")


class SmokeReplyWorker:
    """Echo allowlisted text messages and send the requested non-text fallback."""

    def __init__(
        self,
        queue: ReplyQueueProtocol,
        client: WecomClientProtocol,
        user_id: str,
        expected_ids: frozenset[str],
        *,
        group: str,
        redactor: SecretRedactor,
    ) -> None:
        """Constrain real sends to this smoke run's recipient and message IDs."""
        self._queue = queue
        self._client = client
        self._user_id = user_id
        self._expected_ids = expected_ids
        self._group = group
        self._redactor = redactor

    async def run(self) -> None:
        """Consume until cancelled, retaining API errors as observable events."""
        async with aclosing(
            self._queue.consume(self._group, "smoke-worker", block_ms=500)
        ) as stream:
            async for message in stream:
                if (
                    message.from_user != self._user_id
                    or message.msg_id not in self._expected_ids
                ):
                    continue
                is_text = message.msg_type == "text"
                reply = (message.content or "") if is_text else FALLBACK_TEXT
                with bound_contextvars(msg_id=message.msg_id):
                    try:
                        await self._client.send_text(self._user_id, reply)
                    except WeComAPIError as exc:
                        logger.error(
                            "smoke_reply_failed",
                            action="reply_failed",
                            msg_id=message.msg_id,
                            errcode=exc.errcode,
                            errmsg=self._redactor.redact(exc.errmsg),
                        )
                    else:
                        logger.info(
                            "smoke_reply_done",
                            action="reply_done",
                            msg_id=message.msg_id,
                            kind="echo" if is_text else "fallback",
                        )


def build_encrypted_callback(
    settings: Settings,
    user_id: str,
    msg_type: str,
    content: str | None,
    msg_id: str,
    create_time: int,
) -> tuple[dict[str, str], bytes]:
    """Use the existing SDK to encrypt XML and compute the callback signature."""
    root = ElementTree.Element("xml")
    fields = {
        "ToUserName": settings.wecom_corpid,
        "FromUserName": user_id,
        "CreateTime": str(create_time),
        "MsgType": msg_type,
        "MsgId": msg_id,
    }
    if content is not None:
        fields["Content"] = content
    if msg_type == "image":
        fields["MediaId"] = "smoke-image-placeholder"
    for name, value in fields.items():
        ElementTree.SubElement(root, name).text = value
    plaintext = ElementTree.tostring(root, encoding="unicode")
    timestamp, nonce = str(create_time), secrets.token_hex(8)
    encryption_error: SmokeLoopError | None = None
    try:
        encrypted_xml = WeChatCrypto(
            settings.wecom_token, settings.wecom_aes_key, settings.wecom_corpid
        ).encrypt_message(plaintext, nonce, timestamp)
    except (AssertionError, ValueError, TypeError):
        encryption_error = SmokeLoopError(
            "encrypt",
            "EncodingAESKey 应为 43 个字符，且 Base64 解码后为 32 字节；"
            "请检查 WECOM_AES_KEY 配置。",
        )
    if encryption_error is not None:
        raise encryption_error from None
    encrypted = ElementTree.fromstring(encrypted_xml).findtext("Encrypt")
    if not encrypted:
        raise SmokeLoopError("encrypt", "SDK returned no Encrypt field")
    signer = WeChatSigner()
    signer.add_data(settings.wecom_token, timestamp, nonce, encrypted)
    envelope = ElementTree.Element("xml")
    ElementTree.SubElement(envelope, "ToUserName").text = settings.wecom_corpid
    ElementTree.SubElement(envelope, "Encrypt").text = encrypted
    return {
        "msg_signature": signer.signature,
        "timestamp": timestamp,
        "nonce": nonce,
    }, ElementTree.tostring(envelope, encoding="utf-8")


def _reply_events(log_text: str, msg_id: str) -> list[dict[str, Any]]:
    """Read only structured reply events, ignoring ordinary server log lines."""
    events = []
    for line in log_text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("msg_id") == msg_id
            and event.get("action") in {"reply_done", "reply_failed"}
        ):
            events.append(event)
    return events


async def _wait_for_reply(
    read_logs: Callable[[], str], msg_id: str, stage: str
) -> None:
    """Require a reply_done log within ten seconds and surface API errors."""
    try:
        async with asyncio.timeout(10):
            while True:
                for event in _reply_events(await asyncio.to_thread(read_logs), msg_id):
                    if event["action"] == "reply_failed":
                        raise SmokeLoopError(
                            stage,
                            event.get("errmsg", "API send failed"),
                            event.get("errcode"),
                        )
                    return
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise SmokeLoopError(stage, "No reply_done event within 10 seconds") from None


async def _post_callback(
    client: httpx.AsyncClient, query: dict[str, str], body: bytes
) -> None:
    """Post to the local callback with one bounded transport retry."""
    for attempt in range(2):
        try:
            async with asyncio.timeout(5):
                response = await client.post(
                    "/wecom/callback",
                    params=query,
                    content=body,
                    headers={"Content-Type": "application/xml"},
                    timeout=5,
                )
                response.raise_for_status()
                if response.status_code != 200 or response.content != b"":
                    raise SmokeLoopError(
                        "callback", "Expected HTTP 200 with empty body"
                    )
                return
        except (httpx.HTTPError, TimeoutError):
            if attempt == 1:
                raise SmokeLoopError(
                    "callback", "Local HTTP request failed after retry"
                ) from None


@asynccontextmanager
async def _live_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Add a narrowly scoped smoke consumer around the existing service lifespan."""
    settings = get_settings()
    user_id = os.environ.get("WECOM_SMOKE_USER_ID", "")
    expected_ids = frozenset(json.loads(os.environ.get("WECOM_SMOKE_MSG_IDS", "[]")))
    if not user_id or user_id == "<USER_ID>" or len(expected_ids) != 2:
        raise RuntimeError(
            "Explicit smoke recipient and two allowlisted IDs are required"
        )
    group = f"wecom-smoke-{uuid4().hex}"
    redactor = SecretRedactor(settings)
    async with (
        service_lifespan(app),
        httpx.AsyncClient(
            timeout=10, event_hooks={"response": [redactor.capture_token]}
        ) as http_client,
        WecomClient(
            settings.wecom_corpid,
            settings.wecom_secret,
            settings.wecom_agent_id,
            http_client=http_client,
        ) as client,
    ):
        await app.state.callback_redis.xgroup_create(
            settings.queue_key, group, id="$", mkstream=True
        )
        worker = SmokeReplyWorker(
            app.state.queue,
            client,
            user_id,
            expected_ids,
            group=group,
            redactor=redactor,
        )
        task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            try:
                await app.state.callback_redis.xgroup_destroy(settings.queue_key, group)
            except RedisError:
                logger.warning("smoke_group_cleanup_failed", msg_id=None)


def create_app() -> FastAPI:
    """Create the unchanged HTTP routes plus a smoke-only background consumer."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    app = FastAPI(lifespan=_live_lifespan)
    app.include_router(wecom_callback.router)
    app.add_api_route("/health", health, methods=["GET"])
    return app


async def _start_server(
    log_path: Path, env: dict[str, str]
) -> asyncio.subprocess.Process:
    """Launch local Uvicorn without blocking the asynchronous smoke runner."""
    output = await asyncio.to_thread(log_path.open, "w")
    try:
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "smoke.smoke_loop:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--no-access-log",
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
        )
    finally:
        await asyncio.to_thread(output.close)


async def run_live_loop(settings: Settings, user_id: str) -> None:
    """Start local Uvicorn, send signed text/image/replay and inspect reply logs."""
    if not user_id.strip() or user_id == "<USER_ID>":
        raise ValueError("An actual case-sensitive user_id is required")
    probe = redis.from_url(
        settings.redis_url, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        for attempt in range(2):
            try:
                async with asyncio.timeout(2):
                    await probe.ping()
                break
            except (RedisError, TimeoutError):
                if attempt == 1:
                    raise SmokeLoopError("redis", "Redis is not reachable") from None
        latest = await probe.xrevrange(settings.queue_key, count=1)
        cursor = latest[0][0].decode() if latest else "0-0"
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", 8000))
            except OSError:
                raise SmokeLoopError(
                    "startup", "Local port 8000 is already in use"
                ) from None
        text_id, image_id = uuid4().hex, uuid4().hex
        now = int(time.time())
        text_query, text_body = build_encrypted_callback(
            settings, user_id, "text", f"smoke-loop-{now}", text_id, now
        )
        image_query, image_body = build_encrypted_callback(
            settings, user_id, "image", None, image_id, now
        )
        env = os.environ.copy()
        env.update(
            {key.upper(): str(value) for key, value in settings.model_dump().items()}
        )
        env["WECOM_SMOKE_USER_ID"] = user_id
        env["WECOM_SMOKE_MSG_IDS"] = json.dumps([text_id, image_id])
        with TemporaryDirectory(prefix="wecom-smoke-") as directory:
            log_path = Path(directory, "server.log")
            process = await _start_server(log_path, env)
            try:
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:8000", timeout=2
                ) as local:
                    async with asyncio.timeout(10):
                        while True:
                            if process.returncode is not None:
                                raise SmokeLoopError(
                                    "startup", "Uvicorn exited before health check"
                                )
                            try:
                                health_response = await local.get("/health", timeout=2)
                                if health_response.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(0.1)
                    read_logs = lambda: log_path.read_text()
                    await _post_callback(local, text_query, text_body)
                    await _wait_for_reply(read_logs, text_id, "echo")
                    print("smoke_loop: echo API accepted; action=reply_done verified")
                    await _post_callback(local, image_query, image_body)
                    await _wait_for_reply(read_logs, image_id, "image")
                    print(
                        "smoke_loop: image fallback API accepted; action=reply_done verified"
                    )
                    await _post_callback(local, text_query, text_body)
                    rows = await probe.xrange(settings.queue_key, min=f"({cursor}")
                    matching = [
                        WecomMessage.model_validate_json(fields[b"payload"]).msg_id
                        for _, fields in rows
                    ]
                    if matching.count(text_id) != 1 or matching.count(image_id) != 1:
                        raise SmokeLoopError(
                            "dedup", "Replay produced an extra Stream message"
                        )
                    if (
                        len(_reply_events(await asyncio.to_thread(read_logs), text_id))
                        != 1
                    ):
                        raise SmokeLoopError(
                            "dedup", "Replay produced an extra reply event"
                        )
                    print("smoke_loop: dedup PASS; no second entry or reply")
                    print(
                        "smoke_loop: client receipt still requires human confirmation"
                    )
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
    finally:
        await probe.aclose()


async def run_offline_loop() -> None:
    """Run real crypto, callback and Stream flow with fake Redis/HTTP only."""
    import base64

    from fakeredis import FakeAsyncRedis

    settings = Settings(
        wecom_corpid="offline-corp",
        wecom_secret="offline-secret",
        wecom_token="offline-callback-token",
        wecom_aes_key=base64.b64encode(bytes(range(32))).decode().rstrip("="),
        wecom_agent_id=1000001,
        queue_key="smoke:offline:stream",
        _env_file=None,
    )
    api_calls = {"token": 0, "send": 0, "delivered": 0}

    def api(request: httpx.Request) -> httpx.Response:
        """Simulate one token-expiry recovery while never opening a network socket."""
        if request.url.path.endswith("gettoken"):
            api_calls["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"offline-access-{api_calls['token']}",
                    "expires_in": 7200,
                },
            )
        api_calls["send"] += 1
        if api_calls["send"] == 1:
            return httpx.Response(200, json={"errcode": 42001, "errmsg": "expired"})
        api_calls["delivered"] += 1
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    fake = FakeAsyncRedis(decode_responses=False)
    with patch("src.core.queue.redis.from_url", return_value=fake):
        queue = MessageQueue(settings.redis_url, settings.queue_key)
    sink = StringIO()
    handler = logging.StreamHandler(sink)
    backend = logging.getLogger(__name__)
    original_level = backend.level
    backend.addHandler(handler)
    backend.setLevel(logging.INFO)
    task = None
    try:
        redactor = SecretRedactor(settings)
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(api),
                event_hooks={"response": [redactor.capture_token]},
            ) as http_client,
            WecomClient(
                settings.wecom_corpid,
                settings.wecom_secret,
                settings.wecom_agent_id,
                http_client=http_client,
            ) as client,
        ):
            app = FastAPI()
            app.include_router(wecom_callback.router)
            app.state.queue, app.state.callback_redis = queue, fake
            ids = frozenset({"offline-text", "offline-image"})
            worker = SmokeReplyWorker(
                queue,
                client,
                "offline-user",
                ids,
                group="offline-group",
                redactor=redactor,
            )
            task = asyncio.create_task(worker.run())
            with patch.object(wecom_callback, "get_settings", return_value=settings):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://offline",
                ) as local:
                    text_query, text_body = build_encrypted_callback(
                        settings,
                        "offline-user",
                        "text",
                        "offline echo",
                        "offline-text",
                        1700000000,
                    )
                    image_query, image_body = build_encrypted_callback(
                        settings,
                        "offline-user",
                        "image",
                        None,
                        "offline-image",
                        1700000000,
                    )
                    await _post_callback(local, text_query, text_body)
                    await _wait_for_reply(sink.getvalue, "offline-text", "echo")
                    await _post_callback(local, image_query, image_body)
                    await _wait_for_reply(sink.getvalue, "offline-image", "image")
                    await _post_callback(local, text_query, text_body)
                    assert await fake.xlen(settings.queue_key) == 2
                    assert len(_reply_events(sink.getvalue(), "offline-text")) == 1
                    assert api_calls == {"token": 2, "send": 3, "delivered": 2}
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            task = None
        print(
            "smoke.py: PASS (offline echo/image/dedup; token refresh/cache verified; no real sends)"
        )
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        backend.removeHandler(handler)
        backend.setLevel(original_level)
        handler.close()
        await queue.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicitly requested real local loop without echoing credentials."""
    parser = argparse.ArgumentParser(description="Real local WeCom callback smoke")
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args(argv)
    if not args.user_id.strip() or args.user_id == "<USER_ID>":
        parser.error("An actual case-sensitive user_id is required")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        asyncio.run(run_live_loop(get_settings(), args.user_id))
    except ValidationError as exc:
        fields = [
            ".".join(map(str, error["loc"]))
            for error in exc.errors(include_input=False, include_url=False)
        ]
        print("【配置问题】missing/invalid fields: " + ", ".join(fields))
        return 1
    except SmokeLoopError as exc:
        print(
            f"smoke_loop: FAIL stage={exc.stage}; errcode={exc.errcode}; errmsg={exc.errmsg}"
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - Never print unexpected exceptions with secrets.
        print(f"smoke_loop: FAIL ({type(exc).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
