"""Generate desktop replies with bounded, credential-safe compatible HTTP calls."""

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import resources
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from structlog.contextvars import get_contextvars

from src.core.logging import get_logger

REQUEST_TIMEOUT_SECONDS = 10.0
logger = get_logger(__name__)
_request_active: ContextVar[bool] = ContextVar(
    "desktop_llm_request_active", default=False
)


class ReplyModel(Protocol):
    """Expose the text-only model operation required by the desktop worker."""

    async def reply(self, content: str) -> str:
        """Return a nonempty reply or raise a safe model error."""
        raise NotImplementedError


class LLMError(Exception):
    """Report model configuration or request failure without private inputs."""


class _TransportLogFilter(logging.Filter):
    """Hide HTTPX and HTTPCore diagnostics only in this operation's context."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep unrelated task logs while hiding URLs, headers and body details."""
        transport_logger = record.name == "httpx" or record.name.startswith(
            ("httpx.", "httpcore")
        )
        return not (_request_active.get() and transport_logger)


@contextmanager
def _quiet_transport_logs() -> Iterator[None]:
    """Add context-aware filters to handlers without changing global log levels."""
    log_filter = _TransportLogFilter()
    targets: list[logging.Filterer] = [
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
    ]
    backends = [logging.getLogger()]
    backends.extend(
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    )
    for backend in backends:
        targets.extend(backend.handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    targets = list({id(target): target for target in targets}.values())
    for target in targets:
        target.addFilter(log_filter)
    token = _request_active.set(True)
    try:
        yield
    finally:
        _request_active.reset(token)
        for target in targets:
            target.removeFilter(log_filter)


def _completion_url(base_url: str) -> str:
    """Preserve a configured API prefix and reject credential-bearing URLs."""
    failed = False
    try:
        parsed = urlsplit(base_url)
        url = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not url.host
            or parsed.username is not None
            or parsed.password is not None
            or "?" in base_url
            or "#" in base_url
        ):
            failed = True
    except (ValueError, httpx.InvalidURL):
        failed = True
    if failed:
        raise LLMError(
            "LLM base_url must be HTTP(S) without userinfo, query or fragment"
        )
    normalized = base_url.rstrip("/")
    suffix = "/chat/completions"
    return normalized if normalized.endswith(suffix) else normalized + suffix


def _load_template(template_name: str) -> str:
    """Read only a plain TXT filename from the packaged prompts directory."""
    if not isinstance(template_name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*\.txt", template_name
    ):
        raise LLMError("LLM prompt template must be a plain .txt filename")
    template: str | None = None
    try:
        template = (
            resources.files("prompts")
            .joinpath(template_name)
            .read_text(encoding="utf-8")
            .strip()
        )
    except (OSError, UnicodeError, ModuleNotFoundError):
        pass
    if not template:
        raise LLMError("LLM prompt template is missing, empty or unreadable")
    return template


def _reply_text(payload: Any) -> str:
    """Require one nonempty textual assistant choice without exposing its data."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response has no nonempty textual assistant choice")
    return content


class HTTPReplyModel:
    """Call a compatible chat-completions endpoint with one bounded retry."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        template_name: str = "desktop_reply.txt",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Validate configuration and load the named template before opening HTTP."""
        for field, value in (
            ("base_url", base_url),
            ("api_key", api_key),
            ("model", model),
        ):
            if not isinstance(value, str) or not value.strip():
                raise LLMError(f"LLM {field} must not be empty")
        self._endpoint = _completion_url(base_url.strip())
        self._template = _load_template(template_name)
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._owns_http_client = http_client is None
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        """Enter a reusable model scope while rejecting an already closed client."""
        if self._closed or self._http.is_closed:
            raise LLMError("LLM client is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close only an internally owned HTTP client, including error exits."""
        self._closed = True
        if self._owns_http_client:
            await self._http.aclose()

    async def reply(self, content: str) -> str:
        """Return assistant text after at most two ten-second HTTP attempts."""
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM input must not be empty")
        if self._closed or self._http.is_closed:
            raise LLMError("LLM client is closed")
        msg_id = get_contextvars().get("msg_id") or f"llm-{uuid4().hex}"
        log = logger.bind(msg_id=msg_id)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._template},
                {"role": "user", "content": content},
            ],
        }
        for attempt in range(2):
            error_type = "unknown"
            status_code: int | None = None
            try:
                with _quiet_transport_logs():
                    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                        response = await self._http.post(
                            self._endpoint,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                            json=payload,
                            timeout=REQUEST_TIMEOUT_SECONDS,
                            follow_redirects=False,
                        )
                        response.raise_for_status()
                        text = _reply_text(response.json())
                log.info("llm_reply_generated")
                return text
            except Exception as exc:  # noqa: BLE001 -- External failures become safe errors.
                error_type = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
            log.warning(
                "llm_request_failed",
                attempt=attempt + 1,
                retry=attempt == 0,
                error=error_type,
                status_code=status_code,
            )
            if attempt == 1:
                log.error("llm_reply_failed", error=error_type, status_code=status_code)
                raise LLMError(f"LLM request failed after 2 attempts ({error_type})")
        raise AssertionError("LLM retry loop exhausted without a result")
