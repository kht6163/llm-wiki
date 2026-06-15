"""Web security helpers: CSRF protection, login rate limiting, and response
security headers. These guard the privileged web surface (session-authenticated
humans); the MCP surface is guarded separately by per-request Bearer keys.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Image sources are left permissive so rendered markdown can embed remote images;
# scripts/styles are same-origin only ('unsafe-inline' is still required by the
# few inline handlers/blocks in the templates — tighten with nonces later).
CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https: http:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
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


# -- login rate limiting ---------------------------------------------------
class RateLimiter:
    """In-memory sliding-window limiter (single process). Tracks failures per key
    (IP and username) and blocks once a key exceeds ``max_attempts`` in the
    window. Successful logins reset the key."""

    def __init__(self, max_attempts: int = 8, window_s: float = 300.0):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] > self.window_s:
            dq.popleft()

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            self._prune(dq, now)
            return len(dq) < self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            self._prune(dq, now)
            dq.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# -- response headers ------------------------------------------------------
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security response headers to every response."""

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp
