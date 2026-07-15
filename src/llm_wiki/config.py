"""Application configuration loaded from environment / .env via pydantic-settings.

Validation runs at construction: bad ports, a duplicate GUI/MCP port, or an
unknown log level fail fast with a readable ``ConfigError`` instead of surfacing
as a confusing runtime crash much later.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_OIDC_ROLES = frozenset({"admin", "editor", "viewer"})


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
    request_max_bytes: int = 16 * 1024 * 1024

    # Storage
    vault_path: Path = Path("./vault")
    db_path: Path = Path("./data/llm_wiki.db")

    # Embeddings (local HuggingFace sentence-transformers model)
    embedding_model: str = "intfloat/multilingual-e5-base"
    # Optional HuggingFace revision (commit/tag/branch).  At runtime the resolved model
    # commit is persisted in the DB binding, so an upstream model update cannot mix old
    # passage vectors with query vectors from different weights unnoticed.
    embedding_revision: str = ""
    # When false: skip model load / vector index init; BM25 search still works.
    # Related/RAG/vector mode return empty or structured unavailable.
    embedding_enabled: bool = True

    # Display name for the knowledge base — used as the H1 of the llms.txt /
    # llms-full.txt corpus exports (the agent-facing "what is this vault" line).
    site_title: str = "llm-wiki"

    # Hybrid-search fusion tuning. Defaults match the long-standing hardcoded values;
    # tune per corpus (recall vs. latency) without a code change. rrf_k is the
    # Reciprocal-Rank-Fusion constant (larger flattens rank influence); the candidate_*
    # knobs set the BM25 over-fetch (k = max(top_k*factor, min)) and the vector_* knobs
    # the KNN over-fetch (k_vec = min(k*factor, cap)) that keeps ~k distinct docs after
    # chunk-dedup/filtering.
    rrf_k: int = 60
    search_candidate_factor: int = 4
    search_candidate_min: int = 40
    search_vector_factor: int = 3
    search_vector_cap: int = 600
    # Lightweight reranking layered on RRF (in RRF-score units, ≈1/rrf_k). Title boosts
    # lift a doc whose title exactly/prefix-matches the query; proximity rewards a close
    # vector hit (0 = off). Defaults nudge exact-title matches without scrambling results.
    search_title_exact_boost: float = 0.05
    search_title_prefix_boost: float = 0.015
    search_proximity_weight: float = 0.0

    # Web session signing secret (empty -> generated and persisted in DB meta)
    session_secret: str = ""

    # Mark the session cookie Secure (HTTPS-only). Keep False for local http;
    # set COOKIE_SECURE=true when serving behind TLS / a reverse proxy.
    cookie_secure: bool = False

    # Reverse-proxy trust for X-Forwarded-For/-Proto (uvicorn `forwarded_allow_ips`).
    # Behind a proxy this MUST name the proxy's address so per-client login/MCP
    # throttling and audit logs see the real client IP — otherwise every client
    # collapses to the proxy's IP (one shared rate-limit bucket = self-inflicted
    # lockout, and an untraceable trail). "*" trusts every immediate peer (only safe
    # when nothing untrusted can reach the port directly); "" disables the headers.
    # Default "127.0.0.1" matches a same-host proxy.
    forwarded_allow_ips: str = "127.0.0.1"

    # Application log level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
    log_level: str = "INFO"

    # Optional path to a rotating log file (in addition to stderr). Empty = stderr only.
    log_file: str = ""

    # Seconds to let in-flight requests finish on shutdown before uvicorn force-closes
    # them. Kept under a typical orchestrator kill grace (e.g. Kubernetes' 30s SIGTERM
    # -> SIGKILL) so the process exits cleanly instead of being hard-killed mid-write.
    shutdown_grace_s: int = 25

    # OIDC / SSO (authorization-code + PKCE). Default OFF; local username/password
    # always remains available. MCP stays API-key only (no OIDC on the agent surface).
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_default_role: str = "viewer"
    oidc_username_claim: str = "preferred_username"
    oidc_auto_provision: bool = True
    # Comma-separated email domains allowed to sign in via OIDC (empty = any domain).
    oidc_allowed_email_domains: str = ""
    oidc_require_email_verified: bool = True

    @field_validator("shutdown_grace_s")
    @classmethod
    def _grace_in_range(cls, v: int) -> int:
        if not (1 <= int(v) <= 300):
            raise ValueError(f"shutdown_grace_s must be between 1 and 300 (got {v})")
        return int(v)

    @field_validator("request_max_bytes")
    @classmethod
    def _request_max_bytes_in_range(cls, v: int) -> int:
        minimum = 1 * 1024 * 1024
        maximum = 100 * 1024 * 1024
        if not (minimum <= int(v) <= maximum):
            raise ValueError(
                f"request_max_bytes must be between {minimum} and {maximum} (got {v})"
            )
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

    @field_validator("embedding_revision")
    @classmethod
    def _clean_embedding_revision(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("rrf_k", "search_candidate_factor", "search_candidate_min",
                      "search_vector_factor", "search_vector_cap")
    @classmethod
    def _search_tuning_in_range(cls, v: int, info) -> int:
        bounds = {
            "rrf_k": (1, 1000),
            "search_candidate_factor": (1, 50),
            "search_candidate_min": (1, 2000),
            "search_vector_factor": (1, 50),
            "search_vector_cap": (1, 10000),
        }
        lo, hi = bounds[info.field_name]
        if not (lo <= int(v) <= hi):
            raise ValueError(f"{info.field_name} must be between {lo} and {hi} (got {v})")
        return int(v)

    @field_validator("search_title_exact_boost", "search_title_prefix_boost",
                     "search_proximity_weight")
    @classmethod
    def _rerank_weight_in_range(cls, v: float, info) -> float:
        if not (0.0 <= float(v) <= 10.0):
            raise ValueError(f"{info.field_name} must be between 0.0 and 10.0 (got {v})")
        return float(v)

    @field_validator("oidc_default_role")
    @classmethod
    def _oidc_role(cls, v: str) -> str:
        role = (v or "viewer").strip().lower()
        if role not in _OIDC_ROLES:
            raise ValueError(
                f"oidc_default_role must be one of {sorted(_OIDC_ROLES)} (got {v!r})"
            )
        return role

    @field_validator(
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_redirect_uri",
        "oidc_scopes",
        "oidc_username_claim",
        "oidc_allowed_email_domains",
    )
    @classmethod
    def _strip_oidc_str(cls, v: str) -> str:
        return (v or "").strip()

    @model_validator(mode="after")
    def _distinct_ports_and_oidc(self) -> Settings:
        if self.gui_port == self.mcp_port:
            raise ValueError(
                f"GUI_PORT and MCP_PORT must differ (both are {self.gui_port})"
            )
        if self.oidc_enabled:
            missing = [
                name
                for name, value in (
                    ("oidc_issuer", self.oidc_issuer),
                    ("oidc_client_id", self.oidc_client_id),
                    ("oidc_redirect_uri", self.oidc_redirect_uri),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "OIDC_ENABLED requires "
                    + ", ".join(m.upper() for m in missing)
                )
            if not _is_safe_oidc_redirect_uri(self.oidc_redirect_uri):
                raise ValueError(
                    "oidc_redirect_uri must be https://… or http://127.0.0.1|localhost "
                    f"for local development (got {self.oidc_redirect_uri!r})"
                )
            if not self.oidc_scopes:
                raise ValueError("oidc_scopes must not be empty when OIDC is enabled")
            if not self.oidc_username_claim:
                raise ValueError(
                    "oidc_username_claim must not be empty when OIDC is enabled"
                )
        return self

    def ensure_dirs(self) -> None:
        try:
            self.vault_path.mkdir(parents=True, exist_ok=True)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ConfigError(f"Cannot create data directories: {e}") from e


def _is_safe_oidc_redirect_uri(uri: str) -> bool:
    """Allow https anywhere, or http only to loopback hosts for local dev."""
    parts = urlsplit(uri)
    if not parts.scheme or not parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return True
    if parts.scheme == "http":
        return host in {"127.0.0.1", "localhost", "::1"}
    return False


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration (.env / environment):\n{e}") from e
