"""Web security helpers: CSRF protection, login rate limiting, and response
security headers. These guard the privileged web surface (session-authenticated
humans); the MCP surface is guarded separately by per-request Bearer keys.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..logconf import bind_request_id, new_request_id, reset_request_id
from ..ratelimit import RateLimiter

# Re-exported for callers that still import RateLimiter from this module.
__all__ = ["RateLimiter", "RequestBodyLimitMiddleware", "RequestIdMiddleware",
           "SecurityHeadersMiddleware", "build_csp", "enforce_csrf", "get_csrf_token"]

_log = logging.getLogger("llm_wiki.web")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Read-only POST endpoints that change no state: they only render the caller's own
# input back as sanitized HTML. Exempting them from CSRF avoids silent failures when
# the browser's Origin doesn't match (e.g. behind a proxy or accessed via a different
# host) — the markdown preview must work regardless of how the page was reached.
CSRF_EXEMPT_PATHS = frozenset({"/api/preview"})

# Images may be embedded by rendered markdown over HTTPS (or inline data: URIs);
# plain http: is excluded so a document can't pull insecure/mixed-content images.
def build_csp(nonce: str | None = None) -> str:
    """The site Content-Security-Policy. ``script-src`` is same-origin only — NO
    'unsafe-inline'; the handful of inline <script> blocks instead carry a per-request
    nonce (rendered pages pass ``nonce`` here), and inline on* handlers were refactored
    to addEventListener so none remain. ``style-src`` keeps 'unsafe-inline' because the
    vendored editor bundle injects <style> at runtime. A response with no nonce (JSON
    APIs, static files) gets the strict no-inline form."""
    script_src = "script-src 'self'" + (f" 'nonce-{nonce}'" if nonce else "")
    return (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        f"{script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "  # same-origin fetch + WebSocket (live change stream)
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )


# -- CSRF ------------------------------------------------------------------
def get_csrf_token(request: Request) -> str:
    """Return this session's CSRF token, minting+storing one on first use."""
    token = request.session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf"] = token
    return token


def _same_origin(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        # No Origin/Referer (e.g. a non-browser client). The synchronizer-token
        # check below still applies, and such a client has no session token.
        return True
    return urlsplit(source).netloc == request.url.netloc


async def enforce_csrf(request: Request) -> None:
    """Global dependency: on unsafe methods require a same-origin request *and* a
    valid per-session synchronizer token (form field ``csrf_token`` or header
    ``X-CSRF-Token``). Safe methods pass straight through."""
    if request.method in SAFE_METHODS:
        return
    if request.url.path in CSRF_EXEMPT_PATHS:
        return
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected (CSRF).")
    expected = request.session.get("_csrf")
    sent: str | None = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not hmac.compare_digest(sent, str(expected)):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token.")


# -- request correlation ---------------------------------------------------
class RequestBodyLimitMiddleware:
    """Reject HTTP request bodies that exceed ``max_bytes`` before parsing."""

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope.get("headers", [])
             if key.lower() == b"content-length"),
            None,
        )
        try:
            declared = int(content_length) if content_length is not None else None
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        rejected = False
        response_started = False

        async def limited_receive():
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    if not response_started:
                        await self._reject(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            nonlocal response_started
            if rejected and not response_started:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not rejected:
                raise

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = JSONResponse({"detail": "Request body too large."}, status_code=413)
        await response(scope, receive, send)


class RequestIdMiddleware:
    """Bind a per-request correlation id into the logging context so every llm_wiki
    log line for the request carries it, echo it back as the ``X-Request-ID`` response
    header, and log any unhandled exception with that id before it becomes a 500 — so an
    operator can trace one failing request across the (web + agent) log stream.

    An inbound ``X-Request-ID`` is honoured (so a fronting proxy / caller can correlate
    end-to-end); otherwise a fresh id is minted. The id is also stashed on ``scope['state']``
    so the 500 handler — which runs outside this middleware, after the contextvar is reset —
    can still surface it to the client.

    Pure ASGI (not BaseHTTPMiddleware) on purpose: the contextvar is set in the SAME task
    that runs the endpoint, so it propagates to sync handlers dispatched to the threadpool
    (which copy the context) — a BaseHTTPMiddleware would run the endpoint in a child task
    and the binding would not reliably reach it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = None
        for k, v in scope.get("headers") or []:
            if k == b"x-request-id":
                inbound = v.decode("latin-1").strip()[:64] or None
                break
        rid = inbound or new_request_id()
        scope.setdefault("state", {})["request_id"] = rid
        token = bind_request_id(rid)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("X-Request-ID", rid)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The exception is still re-raised (ServerErrorMiddleware turns it into the
            # 500); we just make sure a server-side log line carries the id first, while
            # the contextvar is still bound.
            _log.exception("unhandled error: method=%s path=%s",
                           scope.get("method"), scope.get("path"))
            raise
        finally:
            reset_request_id(token)


# -- response headers ------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security response headers to every response. Pass ``hsts=True``
    (wired to ``cookie_secure``, i.e. an HTTPS deployment) to also emit
    Strict-Transport-Security; it is omitted on plain HTTP, where a browser would
    ignore it anyway and where pinning a host with no TLS would be a footgun."""

    def __init__(self, app, hsts: bool = False) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # Strict default (no inline) for responses that didn't set their own — JSON APIs,
        # static files. Rendered HTML pages set a nonce'd CSP in render() before this runs,
        # and setdefault leaves that intact.
        resp.headers.setdefault("Content-Security-Policy", build_csp())
        if self.hsts:
            # 1 year, include subdomains. Only sent when serving over HTTPS so it can't
            # strand a plain-HTTP host. (Add 'preload' only if you submit to the list.)
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp
