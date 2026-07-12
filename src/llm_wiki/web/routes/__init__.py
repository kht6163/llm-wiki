"""Web route modules. ``create_web_app`` builds a ``WebDeps`` bag and calls each
``register_*`` so handlers close over the same locals as the monolithic app."""
from __future__ import annotations

from .api import register_api
from .auth_pages import register_auth_pages
from .deps import WebDeps
from .docs_pages import register_docs_pages
from .health import register_health
from .search_graph import register_search_graph
from .settings_admin import register_settings_admin

__all__ = [
    "WebDeps",
    "register_api",
    "register_auth_pages",
    "register_docs_pages",
    "register_health",
    "register_search_graph",
    "register_settings_admin",
]
