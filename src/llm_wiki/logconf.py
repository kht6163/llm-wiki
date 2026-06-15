"""Application logging setup. Configures the ``llm_wiki`` logger tree with a
single-line key=value-ish format suitable for log shipping; leaves uvicorn's own
loggers intact. Optionally tees to a size-rotated file."""
from __future__ import annotations

import logging
from logging.config import dictConfig
from pathlib import Path

_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def configure_logging(level: str = "INFO", log_file: str = "") -> None:
    handlers: dict = {
        "console": {"class": "logging.StreamHandler", "formatter": "std"},
    }
    names = ["console"]
    if log_file:
        Path(log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "std",
            "filename": log_file,
            "maxBytes": _MAX_BYTES,
            "backupCount": _BACKUPS,
            "encoding": "utf-8",
        }
        names.append("file")
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
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
