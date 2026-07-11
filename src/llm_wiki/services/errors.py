"""Structured errors shared by the web + MCP surfaces. Each carries a stable
``code`` (for programmatic branching by an LLM) and an HTTP status."""
from __future__ import annotations


class WikiError(Exception):
    code = "error"
    http_status = 400
    # A machine-readable recovery hint for an LLM client: a stable token telling the
    # agent what to DO about this error (re-read, back off, fix args) without parsing
    # the human ``message``. None on the base class; each subclass sets its default.
    suggested_action: str | None = None

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.suggested_action:
            err["suggested_action"] = self.suggested_action
        err.update(self.extra)  # per-instance context (and an explicit override) wins
        return {"ok": False, "error": err}


class NotFoundError(WikiError):
    code = "not_found"
    http_status = 404
    suggested_action = "verify_path"  # check the path (list_documents/search) before retrying


class ForbiddenError(WikiError):
    code = "forbidden"
    http_status = 403
    suggested_action = "check_permissions"  # the principal's role can't do this; don't retry as-is


class UnauthorizedError(WikiError):
    code = "unauthorized"
    http_status = 401
    suggested_action = "check_credentials"  # missing/invalid API key — fix auth, don't hammer


class ValidationError(WikiError):
    code = "validation"
    http_status = 400
    suggested_action = "fix_request"  # the arguments are malformed; correct them per the message


class ConflictError(WikiError):
    code = "conflict"
    http_status = 409
    suggested_action = "re_read_and_retry"  # lost a CAS race — re-read, rebase, retry with new version


class RateLimitedError(WikiError):
    code = "rate_limited"
    http_status = 429
    suggested_action = "retry_after"  # backed off — pause and retry shortly


class EmbeddingUnavailableError(WikiError):
    code = "embedding_unavailable"
    http_status = 503
    suggested_action = "restart_service"
