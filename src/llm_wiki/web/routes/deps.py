"""Shared dependency bag passed to every ``register_*`` route module."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebDeps:
    """Shared dependencies injected into every route module's ``register_*``."""

    app: Any
    db: Any
    embedder: Any
    docs: Any
    secret: str
    user: Callable[..., Any]
    render: Callable[..., Any]
    embed_resolver: Callable[..., Any]
    login_redirect: Callable[..., Any]
    require_user: Callable[..., Any]
    require_admin: Callable[..., Any]
    principal_web_or_bearer: Callable[..., Any]
    require_user_or_bearer: Callable[..., Any]
    audit_write_rejection: Callable[..., Any]
    write_action_for_path: Callable[..., Any]
    login_limiter: Any
    key_limiter: Any
    read_limiter: Any
