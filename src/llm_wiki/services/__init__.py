"""Service layer: shared business logic + authorization, used by both the web UI
and the MCP server so the two surfaces can never drift."""
from .documents import DocumentService
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
    WikiError,
)

__all__ = [
    "DocumentService",
    "WikiError",
    "NotFoundError",
    "ForbiddenError",
    "UnauthorizedError",
    "ValidationError",
    "ConflictError",
]
