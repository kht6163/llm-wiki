"""Application logging setup. Configures the ``llm_wiki`` logger tree with a
single-line key=value-ish format suitable for log shipping; leaves uvicorn's own
loggers intact. Optionally tees to a size-rotated file."""
from __future__ import annotations

import contextvars
import logging
import uuid
from logging.config import dictConfig
from pathlib import Path

_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5

# Context-local correlation id for the request / tool call currently being handled.
# Set per request by the web RequestIdMiddleware and per tool call by the MCP _call
# wrapper; read by RequestIdFilter so every llm_wiki log line carries it. '-' when
# no request is in scope (startup, CLI, background work without an explicit id).
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def new_request_id() -> str:
    """A short, collision-resistant id to correlate the log lines of one request /
    tool call (and echo back to the client as X-Request-ID)."""
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    return _request_id.get()


def bind_request_id(value: str) -> contextvars.Token:
    """Bind the correlation id for the current context; returns a token to reset()."""
    return _request_id.set(value or "-")


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Inject the context-local request id so the formatter's ``%(request_id)s`` field
    is always populated (default '-'). Attached to the llm_wiki handlers, so every
    record they emit passes through it before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def configure_logging(level: str = "INFO", log_file: str = "") -> None:
    handlers: dict = {
        "console": {"class": "logging.StreamHandler", "formatter": "std",
                    "filters": ["request_id"]},
    }
    names = ["console"]
    if log_file:
        # Expand ~ for BOTH the mkdir and the handler filename. RotatingFileHandler
        # (open()) does not expand ~, so passing the raw string would create the parent
        # under $HOME yet write to a literal "./~/..." path — a crash or misplaced log
        # for the documented LOG_FILE=~/path pattern.
        log_path = str(Path(log_file).expanduser())
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "std",
            "filters": ["request_id"],
            "filename": log_path,
            "maxBytes": _MAX_BYTES,
            "backupCount": _BACKUPS,
            "encoding": "utf-8",
        }
        names.append("file")
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": RequestIdFilter},
        },
        "formatters": {
            "std": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"},
        },
        "handlers": handlers,
        "loggers": {
            "llm_wiki": {
                "handlers": names,
                "level": level.upper() if level else "INFO",
                "propagate": False,
            },
        },
    })


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"llm_wiki.{name}")
