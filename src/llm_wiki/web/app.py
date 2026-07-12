"""FastAPI web UI: Obsidian-like markdown viewer/editor, revision history, link
graph, search, API-key self-service, and admin user management. Handlers are sync
so FastAPI runs them in a threadpool (off the event loop) while they do SQLite /
embedding work. Authorization is delegated to the shared service layer.

Route handlers live in ``web.routes.*``; this module builds middleware, shared
helpers (``render``, auth deps), exception handlers, and registers route modules.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

import bleach
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..logconf import get_request_id
from ..metrics import PrometheusMiddleware
from ..runtime import AppContext
from ..services import audit
from ..services.auth import (
    Principal,
    get_or_create_session_secret,
    principal_from_api_key,
    principal_from_session,
)
from ..services.documents import ATTACH_MAX_BYTES
from ..services.errors import ForbiddenError, WikiError
from ..util import PathError, contains_cjk
from .helpers import (
    _ACTIVITY_WINDOWS,
    _ATTACH_MIME,
    WS_SESSION_RECHECK_S,
    _AuthRequired,
    _diff_lines,
    _human_dt,
    _read_capped,
    _set_build_info,
    _window_since,
)
from .routes import (
    WebDeps,
    register_api,
    register_auth_pages,
    register_docs_pages,
    register_health,
    register_search_graph,
    register_settings_admin,
)
from .security import (
    RateLimiter,
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    build_csp,
    enforce_csrf,
    get_csrf_token,
)

# Re-export helpers for tests that import them from this module.
__all__ = [
    "ATTACH_MAX_BYTES",
    "WS_SESSION_RECHECK_S",
    "_ACTIVITY_WINDOWS",
    "_ATTACH_MIME",
    "_AuthRequired",
    "_diff_lines",
    "_human_dt",
    "_read_capped",
    "_set_build_info",
    "_window_since",
    "create_web_app",
]

_HERE = Path(__file__).parent
log = logging.getLogger("llm_wiki.web")


def create_web_app(app: AppContext) -> FastAPI:
    db, embedder, docs = app.db, app.embedder, app.docs
    _set_build_info(embedder)
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["urlpath"] = lambda s: quote(str(s))
    # Search snippets carry FTS <mark> tags around raw document text; allow only
    # <mark> through so stored content can't inject HTML into the results page.
    templates.env.filters["snippet"] = lambda s: bleach.clean(s or "", tags=["mark"], strip=True)
    templates.env.filters["dt"] = _human_dt

    # Cache-busting for static assets: append the file's mtime as ?v=… so a changed
    # CSS/JS file gets a fresh URL and the browser can't serve a stale copy (no more
    # "hard-refresh to see the fix"). Templates reference assets via {{ static(...) }}.
    _static_dir = _HERE / "static"

    def _static_url(path: str) -> str:
        rel = str(path).lstrip("/")
        try:
            ver = int((_static_dir / rel).stat().st_mtime)
        except OSError:
            ver = 0
        return f"/static/{rel}?v={ver}"

    templates.env.globals["static"] = _static_url
    templates.env.globals["contains_cjk"] = contains_cjk

    # A global dependency enforces CSRF (same-origin + per-session token) on every
    # unsafe method; safe methods pass through. Forms carry the token via a hidden
    # field rendered from render()'s context.
    web = FastAPI(title="llm-wiki", dependencies=[Depends(enforce_csrf)])
    secret = get_or_create_session_secret(db, app.settings.session_secret)
    web.add_middleware(SecurityHeadersMiddleware, hsts=app.settings.cookie_secure)
    web.add_middleware(PrometheusMiddleware)
    # SessionMiddleware always sets the cookie HttpOnly; same_site=lax + (in HTTPS
    # deployments) Secure round out the session-cookie hardening.
    web.add_middleware(
        SessionMiddleware, secret_key=secret, same_site="lax",
        https_only=app.settings.cookie_secure, max_age=14 * 86400,
    )
    web.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=app.settings.request_max_bytes
    )
    # Outermost (added last): bind the correlation id before any other middleware runs
    # so all of them log under it and the X-Request-ID header is set on every response.
    web.add_middleware(RequestIdMiddleware)
    web.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    login_limiter = RateLimiter()
    # Cap API-key minting per user so a hijacked session can't fan out a pile of
    # long-lived keys (which, by policy, only password-change/deactivation revokes).
    key_limiter = RateLimiter(max_attempts=10, window_s=300.0)
    # Bound per-user query-embedding searches. The query encoder runs under a process-wide
    # lock in this single server process, so a flood of distinct queries serializes CPU and
    # starves other searches + the post-write embed worker. Generous for humans (60/min),
    # caps a runaway client. (Mirrors the MCP read throttle on the agent surface.)
    read_limiter = RateLimiter(max_attempts=60, window_s=60.0)

    def user(request: Request) -> Principal | None:
        return principal_from_session(db, request.session.get("sid"))

    def render(name: str, request: Request, status: int = 200, **kw) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        p = user(request)
        # Per-request nonce: the few inline <script> blocks carry it (script-src drops
        # 'unsafe-inline'), so a sanitizer/template slip can't get injected inline script
        # to run. render() is the single HTML hook, so this also stamps the page's CSP.
        nonce = secrets.token_urlsafe(16)
        ctx: dict = {"user": p, "flash": flash, "csrf_token": get_csrf_token(request),
                     "csp_nonce": nonce}
        # The app shell (left file tree + tag list) renders on every authenticated
        # page, so the navigation tree is a common context entry. Anonymous pages
        # (login/error before auth) skip the DB work.
        if p is not None:
            # Cached (generation-invalidated on structural writes) — paid once per write,
            # not per page. /tags and /api/tree still read canonical DB via tree()/tags().
            ctx.setdefault("nav_tree", docs.nav_tree())
            ctx.setdefault("nav_tags", docs.nav_tags())
            # Favourites are per-user (can't share the global nav cache), so read them
            # per request — a small indexed lookup keyed by user_id.
            ctx.setdefault("nav_favorites", docs.list_favorites(p.user_id))
        ctx.update(kw)
        resp = templates.TemplateResponse(request, name, ctx, status_code=status)
        # Stamp this page's CSP with the nonce (overrides the middleware's strict default).
        resp.headers["Content-Security-Policy"] = build_csp(nonce)
        return resp

    def embed_resolver(from_path: str):
        """A render_markdown embed resolver bound to a document's folder: resolves an
        ``![[target]]`` to the live target doc {path,title,content}, or None. Embeds are
        always rendered against the current vault (Obsidian semantics), even in a past
        revision view."""
        def resolve(target: str) -> dict | None:
            rel = docs.resolve_link(target, from_path)
            if not rel:
                return None
            try:
                d = docs.get(rel)
            except WikiError:
                return None
            return {"path": d["path"], "title": d["title"], "content": d["content"]}
        return resolve

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    # ---- auth dependencies (centralize the per-route auth/permission gate) ----
    def require_user(request: Request) -> Principal:
        """Route dependency: the authenticated Principal, or raise to the handler
        (login redirect / 401). Declaring it on a route makes auth impossible to
        forget — there's no inline check to omit."""
        p = user(request)
        if p is None:
            raise _AuthRequired()
        return p

    def require_admin(request: Request) -> Principal:
        """Authenticated AND admin, else a 403 (logged-in) or login redirect."""
        p = require_user(request)
        if not p.can_admin:
            raise ForbiddenError("Admin only.")
        return p

    def principal_web_or_bearer(request: Request) -> Principal | None:
        """A logged-in human (session cookie) OR an agent presenting its MCP API key
        as a Bearer token. Bridges the two surfaces so agent-facing reads (llms.txt
        and the raw .md links it points at) work with a `curl -H 'Authorization:
        Bearer <key>'` exactly as they do in a browser."""
        p = user(request)
        if p is not None:
            return p
        authz = request.headers.get("authorization", "")
        if authz[:7].lower() == "bearer ":
            return principal_from_api_key(db, authz[7:].strip())
        return None

    def require_user_or_bearer(request: Request) -> Principal:
        """Route dependency: a session OR Bearer-API-key Principal, else raise (login
        redirect for a browser, 401 for an `/api/` path) — see principal_web_or_bearer."""
        p = principal_web_or_bearer(request)
        if p is None:
            raise _AuthRequired()
        return p

    def audit_write_rejection(
        request: Request,
        principal: Principal,
        exc: Exception,
        *,
        action: str = "write_rejected",
        target: str | None = None,
    ) -> None:
        """Persist a rejected web write without copying request bodies or secrets."""
        code = str(getattr(exc, "code", "bad_path"))
        outcome = code if code in {"forbidden", "conflict"} else "error"
        try:
            audit.record_tx(
                db,
                actor=principal.username,
                via="web",
                action=action,
                target=target or request.url.path,
                outcome=outcome,
                detail=f"code={code}",
            )
        except Exception:
            # There is no state change to roll back on a rejected attempt. Preserve
            # the original client error and surface the audit outage to operators.
            log.exception("failed to audit rejected web write")

    def write_action_for_path(path: str) -> str:
        if path == "/new" or path == "/broken-links/create":
            return "doc_create"
        if path == "/tags/rename":
            return "tag_rename"
        if path == "/tags/merge":
            return "tag_merge"
        if path.startswith("/trash/") and path.endswith("/restore"):
            return "doc_restore"
        if path.startswith("/trash/") and path.endswith("/purge"):
            return "doc_purge"
        if path.startswith("/api/doc/") and path.endswith("/move"):
            return "doc_move"
        if path.startswith("/api/doc/"):
            return "doc_update"
        if path == "/api/upload":
            return "attachment_upload"
        if path.startswith("/doc/") and path.endswith("/delete"):
            return "doc_delete"
        if path.startswith("/doc/") and "/rev/" in path and path.endswith("/restore"):
            return "doc_restore"
        if path.startswith("/doc/") and path.endswith("/edit"):
            return "doc_update"
        if path.startswith("/settings/keys"):
            return "key_change"
        return "write_rejected"

    @web.exception_handler(PathError)
    async def _on_path_error(request: Request, exc: PathError):
        # An unsafe/malformed path is a client error, not a 500. API routes get JSON;
        # pages get the HTML error template.
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            principal = user(request)
            if principal is not None:
                audit_write_rejection(
                    request,
                    principal,
                    exc,
                    action=write_action_for_path(request.url.path),
                )
        if request.url.path.startswith(("/api/", "/attachments/")):
            return JSONResponse({"ok": False, "error": "bad_path", "message": str(exc)}, status_code=400)
        return render("error.html", request, status=400, message=f"잘못된 경로입니다: {exc}")

    @web.exception_handler(_AuthRequired)
    async def _on_auth_required(request: Request, exc: _AuthRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @web.exception_handler(WikiError)
    async def _on_wiki_error(request: Request, exc: WikiError):
        # The default landing spot for a service error: routes that need bespoke
        # handling (inline conflict re-render, flash-and-redirect) catch their own
        # WikiError before it reaches here. API routes get the structured envelope;
        # pages get the HTML error template at the error's HTTP status.
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            principal = user(request)
            if principal is not None:
                audit_write_rejection(
                    request,
                    principal,
                    exc,
                    action=write_action_for_path(request.url.path),
                )
        response: Response
        if request.url.path.startswith("/api/"):
            response = JSONResponse(exc.to_dict(), status_code=exc.http_status)
        else:
            response = render(
                "error.html", request, status=exc.http_status, message=exc.message
            )
        response.headers["X-Error-Code"] = exc.code
        return response

    @web.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception):
        # Last-resort handler for a genuinely unhandled error (RequestIdMiddleware has
        # already logged the traceback under this id). Return the SAME id to the client
        # so a user can quote it and an operator can grep straight to the failing request.
        rid = getattr(request.state, "request_id", None) or get_request_id()
        if request.url.path.startswith(("/api/", "/attachments/")):
            resp: Response = JSONResponse(
                {"ok": False, "error": {"code": "internal",
                                        "message": "Internal server error.", "request_id": rid}},
                status_code=500)
        else:
            resp = render("error.html", request, status=500,
                          message=f"서버 오류가 발생했습니다. (요청 ID: {rid})")
        resp.headers["X-Request-ID"] = rid
        return resp

    deps = WebDeps(
        app=app,
        db=db,
        embedder=embedder,
        docs=docs,
        secret=secret,
        user=user,
        render=render,
        embed_resolver=embed_resolver,
        login_redirect=login_redirect,
        require_user=require_user,
        require_admin=require_admin,
        principal_web_or_bearer=principal_web_or_bearer,
        require_user_or_bearer=require_user_or_bearer,
        audit_write_rejection=audit_write_rejection,
        write_action_for_path=write_action_for_path,
        login_limiter=login_limiter,
        key_limiter=key_limiter,
        read_limiter=read_limiter,
    )
    register_auth_pages(web, deps)
    register_health(web, deps)
    register_docs_pages(web, deps)
    register_search_graph(web, deps)
    register_api(web, deps)
    register_settings_admin(web, deps)

    # ---- realtime change stream (WebSocket) -----------------------------
    async def ws_changes(websocket: WebSocket) -> None:
        # Session-authenticated, same-origin read-only stream of doc_changed events.
        # Registered as a raw Starlette route so the global CSRF dependency (which
        # injects a Request) doesn't apply to the WebSocket handshake.
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.url.netloc:
            await websocket.close(code=1008)  # cross-origin -> reject (WS hijack guard)
            return
        sid = websocket.session.get("sid")
        if not principal_from_session(db, sid):
            await websocket.close(code=1008)  # not authenticated
            return
        await websocket.accept()
        app.events.bind_loop(asyncio.get_running_loop())
        q = app.events.subscribe()
        # Signal that the subscription is live so a client (or test) knows any change
        # from here on will be delivered. Non-"doc_changed" frames are ignored client-side.
        await websocket.send_json({"type": "ready"})
        recv = asyncio.ensure_future(websocket.receive())
        try:
            while True:
                getev = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait(
                    {recv, getev},
                    timeout=WS_SESSION_RECHECK_S,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    getev.cancel()
                    if not principal_from_session(db, sid):
                        await websocket.close(code=1008)
                        break
                    continue
                if recv in done:
                    getev.cancel()
                    # Retrieve the result so an unexpected receive failure (a network
                    # error, not a clean disconnect) is logged rather than vanishing —
                    # recv.cancel() in finally is a no-op on an already-done task and
                    # would otherwise swallow it silently.
                    exc = recv.exception()
                    if exc is not None and not isinstance(exc, WebSocketDisconnect):
                        log.warning("websocket receive failed: %s", exc, exc_info=exc)
                    break  # any inbound frame/disconnect ends this read-only channel
                # Password changes and account deactivation delete the backing web
                # session.  The handshake check alone would leave an already-open
                # socket authorized forever, so revalidate before releasing each
                # event and close rather than disclose post-revocation changes.
                if not principal_from_session(db, sid):
                    await websocket.close(code=1008)
                    break
                await websocket.send_json(getev.result())
        except WebSocketDisconnect:
            pass
        finally:
            recv.cancel()
            app.events.unsubscribe(q)

    web.router.add_websocket_route("/ws", ws_changes)

    return web
