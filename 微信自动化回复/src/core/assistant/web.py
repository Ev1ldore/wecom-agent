"""Loopback-only contact-selection UI, separate from callback and live runtimes."""

import argparse
import fcntl
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contact_book import ContactBook
from .management import Management, whitelist
from .store import Store, encoded

STATIC = Path(__file__).with_name("web_assets")


class WebFactoryProtocol(Protocol):
    def __call__(self, config: Path, database: Path) -> FastAPI:
        """Build a local app without desktop, LLM or callback side effects."""
        raise NotImplementedError


class ShutdownProtocol(Protocol):
    def __call__(self) -> None:
        """Request graceful exit of only the server instance that owns this app."""
        raise NotImplementedError


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    ids: list[str] = Field(max_length=500)


class NewContact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    conversation: str = Field(min_length=1, max_length=200)
    sender: str = Field(min_length=1, max_length=200)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=300)
    ttl: StrictInt | None = Field(default=None, ge=1, le=31536000)


@dataclass
class _Context:
    store: Store
    allowed: set
    config: dict


@contextmanager
def contact_manager(book, database, cid):
    config = book.file.read()
    contact = next((c for c in book.catalog(config) if c["id"] == cid), None)
    if contact is None:
        raise HTTPException(404, "联系人不存在")
    pair = (contact["conversation"], contact["sender"])
    if pair not in set(whitelist(config)):
        raise HTTPException(403, "请先勾选并保存该联系人")
    database.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(database) + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(
                409, "请先停止使用此数据库的离线监听，再管理记忆和附件"
            ) from None
        store = Store(database)
        try:
            yield (
                Management(_Context(store, set(whitelist(config)), config)),
                encoded(pair),
            )
        finally:
            store.close()


def create_app(
    config: Path, database: Path, *, shutdown: ShutdownProtocol | None = None
) -> FastAPI:
    book = ContactBook(Path(config))
    book.state()  # Validate config before serving; never silently replace it.
    database = Path(database)
    token = secrets.token_urlsafe(32)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shutdown_requested = False
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            if app.state.shutdown_requested and request.url.path != "/api/shutdown":
                return JSONResponse(
                    {"detail": "程序正在退出，无法继续修改"}, status_code=503
                )
            if not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), token
            ):
                return JSONResponse({"detail": "请刷新页面后重试"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站修改"}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 100_000:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        message = str(exc)
        return JSONResponse(
            {"detail": message}, status_code=409 if "其他窗口" in message else 400
        )

    @app.exception_handler(OSError)
    async def unavailable(request, exc):
        return JSONResponse(
            {"detail": "本地文件不可用，请检查配置文件和目录权限"}, status_code=503
        )

    @app.get("/")
    def page():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        return {
            **book.state(),
            "csrf_token": token,
            "shutdown_available": shutdown is not None,
            "shutdown_requested": app.state.shutdown_requested,
        }

    @app.post("/api/shutdown")
    async def stop():
        if shutdown is None:
            raise HTTPException(
                409, "当前启动方式不支持页面退出，请在运行终端按 Control+C"
            )
        if app.state.shutdown_requested:
            return JSONResponse({"status": "stopping"}, status_code=202)
        app.state.shutdown_requested = True
        return JSONResponse(
            {"status": "stopping"}, status_code=202, background=BackgroundTask(shutdown)
        )

    @app.post("/api/selection")
    def select(payload: Selection):
        return book.select(payload.ids, payload.revision)

    @app.post("/api/contacts")
    def add(payload: NewContact):
        return book.add(
            payload.name, payload.conversation, payload.sender, payload.revision
        )

    @app.get("/api/contacts/{cid}/details")
    def details(cid: str):
        with contact_manager(book, database, cid) as (manager, scope):
            return {
                "memory": manager.memories(scope),
                "short_term": manager.short_term(scope),
                "documents": manager.documents(scope),
            }

    @app.post("/api/contacts/{cid}/session/reset")
    def reset_session(cid: str):
        with contact_manager(book, database, cid) as (manager, scope):
            manager.reset_short_term(scope)
            return {"reset": True, "short_term": manager.short_term(scope)}

    @app.post("/api/contacts/{cid}/memory")
    def memory(cid: str, payload: MemoryInput):
        with contact_manager(book, database, cid) as (manager, scope):
            manager.set_memory(scope, payload.key, payload.value, payload.ttl)
            return {"memory": manager.memories(scope)}

    @app.delete("/api/contacts/{cid}/memory/{key}")
    def forget_memory(cid: str, key: str):
        with contact_manager(book, database, cid) as (manager, scope):
            return {"removed": manager.forget_memory(scope, key)}

    @app.delete("/api/contacts/{cid}/documents/{document_id}")
    def forget_document(cid: str, document_id: str):
        with contact_manager(book, database, cid) as (manager, scope):
            return {"removed": manager.forget_document(scope, document_id)}

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="本机联系人与白名单管理；不启动微信自动回复"
    )
    parser.add_argument("--config", default="assistant.local.json")
    parser.add_argument("--db", default="logs/assistant.sqlite3")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须为1024至65535")
    import uvicorn

    def request_shutdown():
        # This closure owns exactly this uvicorn instance; no process scanning,
        # signals, force exit, or impact on any separate listener/client.
        server.should_exit = True

    app = create_app(Path(args.config), Path(args.db), shutdown=request_shutdown)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    server.run()


if __name__ == "__main__":
    main()
