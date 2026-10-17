"""FastAPI entry point and callback resource lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis import asyncio as redis

from src.config import get_settings
from src.core.queue import MessageQueue
from src.gateway.wecom_callback import router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared callback clients on startup and close them on shutdown."""
    settings = get_settings()
    queue = MessageQueue(settings.redis_url, settings.queue_key)
    dedup_redis = redis.from_url(settings.redis_url, socket_connect_timeout=1)
    app.state.queue = queue
    app.state.callback_redis = dedup_redis
    try:
        yield
    finally:
        try:
            await dedup_redis.aclose()
        finally:
            await queue.aclose()


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the HTTP application is running."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=get_settings().app_port)
