"""Structured errors shared by the web + MCP surfaces. Each carries a stable
``code`` (for programmatic branching by an LLM) and an HTTP status."""
from __future__ import annotations


class WikiError(Exception):
    code = "error"
    http_status = 400

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.extra}}


class NotFoundError(WikiError):
    code = "not_found"
    http_status = 404


class ForbiddenError(WikiError):
    code = "forbidden"
    http_status = 403


class UnauthorizedError(WikiError):
    code = "unauthorized"
    http_status = 401


class ValidationError(WikiError):
    code = "validation"
    http_status = 400


class ConflictError(WikiError):
    code = "conflict"
    http_status = 409
