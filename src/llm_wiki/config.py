"""Application configuration loaded from environment / .env via pydantic-settings.

Validation runs at construction: bad ports, a duplicate GUI/MCP port, or an
unknown log level fail fast with a readable ``ConfigError`` instead of surfacing
as a confusing runtime crash much later.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(RuntimeError):
    """Raised when the resolved configuration is invalid (bad .env / environment)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Servers
    host: str = "127.0.0.1"
    gui_port: int = 8080
    mcp_port: int = 8081

    # Storage
    vault_path: Path = Path("./vault")
    db_path: Path = Path("./data/llm_wiki.db")

    # Embeddings (local HuggingFace sentence-transformers model)
    embedding_model: str = "intfloat/multilingual-e5-base"

    # Web session signing secret (empty -> generated and persisted in DB meta)
    session_secret: str = ""

    # Mark the session cookie Secure (HTTPS-only). Keep False for local http;
    # set COOKIE_SECURE=true when serving behind TLS / a reverse proxy.
    cookie_secure: bool = False

    # Application log level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    log_level: str = "INFO"

    # Optional path to a rotating log file (in addition to stderr). Empty = stderr only.
    log_file: str = ""

    # Seconds to let in-flight requests finish on shutdown before uvicorn force-closes
    # them. Kept under a typical orchestrator kill grace (e.g. Kubernetes' 30s SIGTERM
    # -> SIGKILL) so the process exits cleanly instead of being hard-killed mid-write.
    shutdown_grace_s: int = 25

    @field_validator("shutdown_grace_s")
    @classmethod
    def _grace_in_range(cls, v: int) -> int:
        if not (1 <= int(v) <= 300):
            raise ValueError(f"shutdown_grace_s must be between 1 and 300 (got {v})")
        return int(v)

    @field_validator("gui_port", "mcp_port")
    @classmethod
    def _port_in_range(cls, v: int, info) -> int:
        if not (1 <= int(v) <= 65535):
            raise ValueError(f"{info.field_name} must be between 1 and 65535 (got {v})")
        return int(v)

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, v: str) -> str:
        level = (v or "INFO").upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_LOG_LEVELS)} (got {v!r})")
        return level

    @field_validator("host", "embedding_model")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        if not (v or "").strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return v

    @model_validator(mode="after")
    def _distinct_ports(self) -> Settings:
        if self.gui_port == self.mcp_port:
            raise ValueError(
                f"GUI_PORT and MCP_PORT must differ (both are {self.gui_port})"
            )
        return self

    def ensure_dirs(self) -> None:
        try:
            self.vault_path.mkdir(parents=True, exist_ok=True)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ConfigError(f"Cannot create data directories: {e}") from e


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration (.env / environment):\n{e}") from e
