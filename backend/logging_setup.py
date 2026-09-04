"""Structured JSON logging.

INSTRUCTIONS.md §12: JSON lines, a conversation id on every line once one
exists, and never transcript content at INFO. The conversation id is carried
in a ContextVar so call sites do not have to thread it through every function.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Set once per session; every log line emitted while handling that session
# carries it automatically.
conversation_id_var: ContextVar[str | None] = ContextVar(
    "conversation_id", default=None
)

# Attributes LogRecord always carries; anything else was passed via `extra`
# and should be surfaced in the JSON payload.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        conversation_id = conversation_id_var.get()
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn installs its own handlers; route them through ours so every line
    # on stdout is valid JSON for journald to collect.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
