"""Provide structured event logging with per-message context propagation."""

import logging
from typing import Any, Protocol

import structlog


class EventLoggerProtocol(Protocol):
    """Define the structured logger operations shared by service modules."""

    def bind(self, **fields: Any) -> "EventLoggerProtocol":
        """Return a logger carrying the supplied structured fields."""
        raise NotImplementedError

    def info(self, event: str, **fields: Any) -> None:
        """Record an informational event with structured fields."""
        raise NotImplementedError

    def warning(self, event: str, **fields: Any) -> None:
        """Record a warning event with structured fields."""
        raise NotImplementedError

    def error(self, event: str, **fields: Any) -> None:
        """Record an error event with structured fields."""
        raise NotImplementedError


def get_logger(name: str) -> EventLoggerProtocol:
    """Wrap a named stdlib logger without replacing application log handlers."""
    return structlog.wrap_logger(
        logging.getLogger(name),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
