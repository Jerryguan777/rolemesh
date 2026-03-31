"""Structured logging configuration using structlog."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def _setup_structlog() -> None:
    """Configure structlog with console rendering."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


_setup_structlog()


def get_logger(**kwargs: object) -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(**kwargs)  # type: ignore[no-any-return]
