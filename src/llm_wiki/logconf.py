"""Application logging setup. Configures the ``llm_wiki`` logger tree with a
single-line key=value-ish format suitable for log shipping; leaves uvicorn's own
loggers intact."""
from __future__ import annotations

import logging
from logging.config import dictConfig


def configure_logging(level: str = "INFO") -> None:
    dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "std"},
        },
        "loggers": {
            "llm_wiki": {
                "handlers": ["console"],
                "level": level.upper() if level else "INFO",
                "propagate": False,
            },
        },
    })


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"llm_wiki.{name}")
