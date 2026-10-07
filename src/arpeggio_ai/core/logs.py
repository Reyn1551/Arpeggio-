"""Structured logs: one JSON object per line in ``logs/YYYY-MM-DD.jsonl`` (UTC date).

Each line carries ``task_id`` and ``attempt_id`` from ``log_context``, so log lines join to
rows in the database (OBS-03). Usage::

    log = logging.getLogger(__name__)
    with log_context(task_id=task.id, attempt_id=attempt.id):
        log.info("step.recorded", extra={"seq": 3, "cost_usd": 0.01})

Extra fields with secret-looking names are written as ``[REDACTED]``. This is a safety net
only. Never pass secrets or full prompts to the logger in the first place.
"""

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arpeggio_ai.core.clock import format_utc

ROOT_LOGGER = "arpeggio_ai"
REDACTED = "[REDACTED]"
DIR_MODE = 0o700

_SECRET_NAME = re.compile(
    r"(^|_)(api_?key|key|token|secret|password|passwd|credentials?|authorization)$",
    re.IGNORECASE,
)
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "task_id",
    "attempt_id",
}

_task_id: ContextVar[str | None] = ContextVar("arpeggio_task_id", default=None)
_attempt_id: ContextVar[str | None] = ContextVar("arpeggio_attempt_id", default=None)


@contextmanager
def log_context(task_id: str | None = None, attempt_id: str | None = None) -> Iterator[None]:
    """Attach task and attempt IDs to every log line emitted inside the block."""
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    if task_id is not None:
        tokens.append((_task_id, _task_id.set(task_id)))
    if attempt_id is not None:
        tokens.append((_attempt_id, _attempt_id.set(attempt_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = record.__dict__
        entry: dict[str, Any] = {
            "ts": format_utc(datetime.fromtimestamp(record.created, UTC)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
            "task_id": fields.get("task_id") or _task_id.get(),
            "attempt_id": fields.get("attempt_id") or _attempt_id.get(),
        }
        for name, value in fields.items():
            if name not in _RESERVED:
                entry[name] = REDACTED if _SECRET_NAME.search(name) else value
        if record.exc_info and record.exc_info[1] is not None:
            entry["exc_type"] = type(record.exc_info[1]).__name__
            entry["exc"] = str(record.exc_info[1])
        return json.dumps(entry, default=str)


class _JsonLinesHandler(logging.FileHandler):
    """Marks the handlers this module owns, so reconfiguring replaces only those."""


def close_logging() -> None:
    """Detach and close the JSON-lines handler, if any."""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in [h for h in logger.handlers if isinstance(h, _JsonLinesHandler)]:
        logger.removeHandler(handler)
        handler.close()


def configure_logging(logs_dir: Path, level: int = logging.INFO) -> Path:
    """Send ``arpeggio_ai`` logs to today's JSON-lines file and return its path.

    Safe to call more than once. The file is opened on the first log line.
    """
    logs_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    path = logs_dir / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
    close_logging()
    handler = _JsonLinesHandler(path, encoding="utf-8", delay=True)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(ROOT_LOGGER)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return path
