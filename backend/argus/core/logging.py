"""Structured logging setup.

structlog and the standard library share one handler and one renderer, so uvicorn, SQLAlchemy and
Alembic lines come out in the same format (JSON in production) as the application's own events,
and every line carries the bound context (``request_id`` for requests, ``run_id`` for runs).
"""

from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

import structlog

# uvicorn installs its own handlers before the app is imported; they are reset to propagate to root.
_FOREIGN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    level_no = logging.getLevelName(level.upper())
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level_no)
    for name in _FOREIGN_LOGGERS:
        foreign = logging.getLogger(name)
        foreign.handlers.clear()
        foreign.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def _rebind(values: dict[str, Any]) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**values)


def fresh_log_context(**values: Any) -> contextvars.Context:
    """A copy of the current context whose log context holds only ``values``.

    Pass it as ``asyncio.create_task(..., context=...)`` for background work (council runs) so its
    lines carry its own ids instead of the id of the request that happened to start it.
    """
    ctx = contextvars.copy_context()
    ctx.run(_rebind, values)
    return ctx
