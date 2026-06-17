"""FastAPI web UI: Obsidian-like markdown viewer/editor, revision history, link
graph, search, API-key self-service, and admin user management. Handlers are sync
so FastAPI runs them in a threadpool (off the event loop) while they do SQLite /
embedding work. Authorization is delegated to the shared service layer.
"""
from __future__ import annotations

import asyncio
import difflib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import bleach
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.middleware.sessions import SessionMiddleware

from ..markdown_render import render_markdown
from ..metrics import BUILD_INFO, PrometheusMiddleware, collect_index_gauges, render_latest
from ..runtime import AppContext
from ..services import audit
from ..services import users as users_svc
from ..services.auth import (
    Principal,
    authenticate,
    create_api_key,
    create_session,
    create_user,
    delete_session,
    get_or_create_session_secret,
    list_api_keys,
    principal_from_session,
    revoke_api_key,
)
from ..services.documents import ATTACH_MAX_BYTES
from ..services.errors import ConflictError, ForbiddenError, ValidationError, WikiError
from ..util import (
    PathError,
    clamp_int,
    content_disposition_attachment,
    normalize_client_ip,
    word_count,
)
from .security import (
    RateLimiter,
    SecurityHeadersMiddleware,
    enforce_csrf,
    get_csrf_token,
)

_HERE = Path(__file__).parent
_UPLOAD_CHUNK = 64 * 1024


class _AuthRequired(Exception):
    """Raised by the ``require_*`` route dependencies when there's no valid session.
    A registered handler turns it into a /login redirect (pages) or 401 JSON (/api),
    so individual routes no longer repeat the unauthenticated branch."""


async def _read_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read an upload in chunks, aborting as soon as it exceeds ``limit`` so a
    multi-GB body can't be buffered into memory before the size check. Returns None
    on overflow; peak memory stays at ~limit + one chunk."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _diff_lines(a_text: str, b_text: str) -> list[dict]:
    """Unified line diff classified for template rendering (text is escaped by Jinja)."""
    out: list[dict] = []
    for line in difflib.unified_diff(a_text.splitlines(), b_text.splitlines(), lineterm="", n=3):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append({"cls": cls, "text": line})
    return out


_ISO_UTC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})Z$")


def _human_dt(value: object) -> object:
    """Render a stored UTC ISO timestamp (``2026-06-16T00:44:33Z``) as a
    ``<time>`` element that static/datetime.js localizes to the viewer's
    timezone as ``YYYY-MM-DD HH:MM:SS``. Without JS it still shows the cleaned
    UTC value; anything that isn't our exact ISO format passes through."""
    if not value:
        return value
    s = str(value)
    m = _ISO_UTC_RE.match(s)
    if not m:
        return value
    utc_text = f"{m.group(1)} {m.group(2)}"
    return Markup('<time class="dt" datetime="{}">{}</time>').format(s, utc_text)


# Activity-feed time windows -> an ISO-8601 lower bound on the audit `ts` (which is
# stored UTC, so lexical >= comparison is correct). "all" means no lower bound.
_ACTIVITY_WINDOWS = ("today", "24h", "7d", "30d", "all")


def _window_since(window: str) -> str | None:
    now = datetime.now(UTC)
    if window == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "24h":
        start = now - timedelta(days=1)
    elif window == "30d":
        start = now - timedelta(days=30)
    elif window == "all":
        return None
    else:  # default / "7d"
        start = now - timedelta(days=7)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_build_info(embedder) -> None:
    """Publish static runtime facts as the llmwiki_build_info metric (set once)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            ver = version("llm-wiki")
        except PackageNotFoundError:
            ver = "unknown"
        BUILD_INFO.info({
            "version": ver,
            "embedding_model": embedder.model_name,
            "embedding_dim": str(embedder.dim),
        })
    except Exception:
        pass  # info metric is best-effort; never block app construction


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

    # A global dependency enforces CSRF (same-origin + per-session token) on every
    # unsafe method; safe methods pass through. Forms carry the token via a hidden
    # field rendered from render()'s context.
    web = FastAPI(title="llm-wiki", dependencies=[Depends(enforce_csrf)])
    secret = get_or_create_session_secret(db, app.settings.session_secret)
    web.add_middleware(SecurityHeadersMiddleware)
    web.add_middleware(PrometheusMiddleware)
    web.add_middleware(
        SessionMiddleware, secret_key=secret, same_site="lax",
        https_only=app.settings.cookie_secure, max_age=14 * 86400,
    )
    web.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    login_limiter = RateLimiter()

    def user(request: Request) -> Principal | None:
        return principal_from_session(db, request.session.get("sid"))

    def render(name: str, request: Request, status: int = 200, **kw) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        p = user(request)
        ctx: dict = {"user": p, "flash": flash, "csrf_token": get_csrf_token(request)}
        # The app shell (left file tree + tag list) renders on every authenticated
        # page, so the navigation tree is a common context entry. Anonymous pages
        # (login/error before auth) skip the DB work.
        if p is not None:
            ctx.setdefault("nav_tree", docs.tree())
            ctx.setdefault("nav_tags", docs.tags()[:40])
        ctx.update(kw)
        return templates.TemplateResponse(request, name, ctx, status_code=status)

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

    @web.exception_handler(PathError)
    async def _on_path_error(request: Request, exc: PathError):
        # An unsafe/malformed path is a client error, not a 500. API routes get JSON;
        # pages get the HTML error template.
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
        if request.url.path.startswith("/api/"):
            return JSONResponse(exc.to_dict(), status_code=exc.http_status)
        return render("error.html", request, status=exc.http_status, message=exc.message)

    # ---- auth -----------------------------------------------------------
    @web.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        if user(request):
            return RedirectResponse("/", status_code=303)
        return render("login.html", request, error=None)

    @web.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
        ip = normalize_client_ip(request.client.host if request.client else None)
        uname = (username or "").strip().lower()
        # Throttle by client IP only. A per-username counter would let anyone lock a
        # known account (e.g. admin) out from its own clean IP just by spamming bad
        # passwords — the lockout itself becomes the DoS. Failed attempts are still
        # audit-logged with the username so a spray is detectable.
        ip_key = f"ip:{ip}"
        if not login_limiter.allowed(ip_key):
            return render("login.html", request, status=429,
                          error="Too many attempts. Please wait a few minutes and try again.")
        p = authenticate(db, username, password)
        if not p:
            just_blocked = login_limiter.record_failure(ip_key)
            audit.record_tx(db, actor=uname or "-", via="web", action="login_failed",
                            target=None, outcome="error", detail=f"ip={ip}")
            if just_blocked:
                # The failure that just crossed the throttle threshold — record one
                # block event (subsequent attempts short-circuit at the allowed() gate
                # above without re-auditing) so a brute-force surfaces in the admin feed
                # instead of leaving only a wall of identical login_failed rows.
                audit.record_tx(db, actor=uname or "-", via="web", action="login_blocked",
                                target=None, outcome="blocked", detail=f"ip={ip}")
            return render("login.html", request, status=401, error="Invalid username or password.")
        login_limiter.reset(ip_key)
        audit.record_tx(db, actor=p.username, via="web", action="login", detail=f"ip={ip}")
        # Drop any pre-login session state (fixation hardening), then bind the new
        # session. A fresh CSRF token is minted on the next rendered page.
        request.session.clear()
        request.session["sid"] = create_session(db, p.user_id)
        return RedirectResponse("/", status_code=303)

    @web.get("/logout")
    def logout(request: Request):
        delete_session(db, request.session.get("sid"))
        request.session.clear()
        return login_redirect()

    @web.get("/healthz")
    def healthz():
        # Liveness: cheap, always ok if the process is up.
        return JSONResponse({"ok": True})

    @web.get("/readyz")
    def readyz():
        # Readiness: DB reachable AND the embedding model loaded. Orchestrators
        # should route traffic only once this returns 200. Also surfaces index health
        # (embedding backlog / pending writes / broken links) for at-a-glance ops.
        details: dict = {}
        try:
            details = collect_index_gauges(db)
            ready = embedder.is_loaded
        except Exception:
            ready = False
        code = 200 if ready else 503
        return JSONResponse({
            "ok": ready, "ready": ready, "model_loaded": embedder.is_loaded,
            "embedding_model": embedder.model_name, **details,
        }, status_code=code)

    @web.get("/metrics")
    def metrics():
        # Prometheus exposition over the shared process registry (web + MCP). Like
        # /healthz this is unauthenticated; restrict it at the network layer if the
        # port is exposed beyond the scrape target. Refresh point-in-time gauges from
        # the DB at scrape time.
        try:
            collect_index_gauges(db)
        except Exception:
            pass  # never let a metrics refresh failure 500 the scrape endpoint
        body, ctype = render_latest()
        return Response(content=body, media_type=ctype)

    # ---- documents ------------------------------------------------------
    @web.get("/", response_class=HTMLResponse)
    def home(request: Request, folder: str | None = None, tag: str | None = None,
             sort: str = "updated_at", page: int = 1, _p: Principal = Depends(require_user)):
        per_page = 50
        page = max(1, int(page))
        offset = (page - 1) * per_page
        items = docs.list_docs(folder=folder, tag=tag, limit=per_page, offset=offset, sort=sort)
        total = docs.count(folder=folder, tag=tag)
        # Folder counts come from a dedicated query (not the current page) so the
        # sidebar totals stay correct regardless of which page is shown.
        folders = docs.folder_counts()
        return render("list.html", request, items=items, folder=folder, tag=tag, sort=sort,
                      folders=folders, page=page, per_page=per_page, total=total,
                      has_prev=page > 1, has_next=offset + len(items) < total)

    @web.get("/tags", response_class=HTMLResponse)
    def tags_page(request: Request, _p: Principal = Depends(require_user)):
        return render("tags.html", request, tags=docs.tags())

    @web.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", mode: str = "hybrid", top_k: int = 20,
                    folder: str | None = None, tag: str | None = None,
                    _p: Principal = Depends(require_user)):
        top_k = clamp_int(top_k, 1, 50)
        tags = [tag] if tag and tag.strip() else None
        results = []
        truncated = False
        if q.strip():
            hits, truncated = docs.search_page(
                q, mode=mode, top_k=top_k, folder=folder or None, tags=tags)
            results = [r.to_dict() for r in hits]
        return render("search.html", request, q=q, mode=mode, top_k=top_k,
                      folder=folder or "", tag=tag or "", results=results,
                      truncated=truncated, folders=docs.folders())

    @web.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request, root: str | None = None,
                   _p: Principal = Depends(require_user)):
        return render("graph.html", request, root=root or "")

    @web.get("/broken-links", response_class=HTMLResponse)
    def broken_links_page(request: Request, limit: int = 500,
                          _p: Principal = Depends(require_user)):
        data = docs.broken_links(limit=clamp_int(limit, 1, 2000))
        return render("broken_links.html", request, count=data["count"], links=data["links"])

    @web.get("/activity", response_class=HTMLResponse)
    def activity_page(request: Request, window: str = "7d", via: str | None = None,
                      action: str | None = None, p: Principal = Depends(require_user)):
        # "Who/what changed the vault, and over which surface." Editors see document
        # activity; admins additionally see security/account events (login, keys,
        # role changes) since those are theirs to audit.
        if not p.can_write:
            return render("error.html", request, status=403,
                          message="활동 피드는 편집자 이상만 볼 수 있습니다.")
        window = window if window in _ACTIVITY_WINDOWS else "7d"
        via_f = via if via in ("web", "mcp", "cli") else None
        scope = None if p.can_admin else audit.DOC_ACTIONS
        events = audit.recent(db, limit=300, since=_window_since(window),
                              via=via_f, action=(action or None), actions=scope)
        return render("activity.html", request, events=events, window=window,
                      windows=_ACTIVITY_WINDOWS, via=via_f or "", action=action or "",
                      is_admin=p.can_admin, doc_actions=audit.DOC_ACTIONS)

    @web.get("/api/graph")
    def api_graph(request: Request, root: str | None = None, depth: int = 1, limit: int = 500,
                  _p: Principal = Depends(require_user)):
        return JSONResponse(docs.graph(root=root or None, depth=depth, limit=limit))

    @web.get("/api/complete")
    def api_complete(request: Request, q: str = "", _p: Principal = Depends(require_user)):
        return JSONResponse({"ok": True, "items": docs.complete(q, limit=12)})

    @web.get("/api/tree")
    def api_tree(request: Request, _p: Principal = Depends(require_user)):
        # Live tree payload so the sidebar can refresh after a folder/doc change
        # without a full page reload.
        return JSONResponse({"ok": True, "tree": docs.tree()})

    @web.post("/api/folders")
    def api_folder_create(request: Request, path: str = Form(...),
                          p: Principal = Depends(require_user)):
        return JSONResponse({"ok": True, **docs.create_folder(p, path)})

    @web.post("/api/folders/{path:path}/delete")
    def api_folder_delete(path: str, request: Request, p: Principal = Depends(require_user)):
        return JSONResponse({"ok": True, **docs.delete_folder(p, path)})

    @web.post("/api/doc/{path:path}/move")
    def api_doc_move(path: str, request: Request, new_path: str = Form(...),
                     p: Principal = Depends(require_user)):
        # Rewrite inbound link text too, so a move in the UI doesn't silently
        # leave dangling references behind.
        doc = docs.move(p, path, new_path, fix_references=True)
        return JSONResponse({"ok": True, "path": doc["path"],
                             "references": doc.get("references")})

    @web.post("/api/doc/{path:path}/toggle-task")
    def api_toggle_task(path: str, request: Request, index: int = Form(...),
                        base_version: int = Form(None), p: Principal = Depends(require_user)):
        doc = docs.toggle_task(p, path, index=index, base_version=base_version)
        return JSONResponse({"ok": True, "version": doc["version"]})

    @web.post("/api/preview")
    def api_preview(request: Request, content: str = Form(""), path: str = Form("preview.md"),
                    _p: Principal = Depends(require_user)):
        return JSONResponse({"ok": True, "html": render_markdown(content, path or "preview.md")})

    @web.get("/api/doc/{path:path}/preview")
    def api_doc_preview(path: str, request: Request, _p: Principal = Depends(require_user)):
        # Plain-text title + excerpt for the list/search hover popover.
        return JSONResponse({"ok": True, **docs.preview(path)})

    @web.get("/api/doc/{path:path}/rendered")
    def api_doc_rendered(path: str, request: Request, _p: Principal = Depends(require_user)):
        # Live-refresh payload: the realtime client fetches this when a WebSocket
        # change event arrives and swaps the rendered body in place.
        doc = docs.get(path)
        return JSONResponse({
            "ok": True, "path": doc["path"], "version": doc["version"], "title": doc["title"],
            "updated_at": doc["updated_at"], "updated_by": doc["updated_by"],
            "last_via": doc.get("last_via"), "tags": doc["tags"],
            "html": render_markdown(doc["content"], doc["path"]),
        })

    @web.post("/api/upload")
    async def api_upload(request: Request, file: UploadFile = File(...),
                         p: Principal = Depends(require_user)):
        data = await _read_capped(file, ATTACH_MAX_BYTES)
        if data is None:
            raise ValidationError(f"Attachment too large (limit {ATTACH_MAX_BYTES} bytes).")
        res = docs.save_attachment(p, file.filename or "file", data)
        audit.record_tx(db, actor=p.username, via="web", action="attachment_upload", target=res["path"])
        return JSONResponse({"ok": True, **res})

    @web.get("/attachments/{subpath:path}")
    def attachment(subpath: str, request: Request, _p: Principal = Depends(require_user)):
        target = docs.attachment_file(subpath)
        # Hardened CSP overrides the site default (which permits inline scripts):
        # an SVG opened directly as a document must not execute scripts. The explicit
        # script-src 'none' is unambiguous, sandbox strips same-origin/JS as defense
        # in depth, and Content-Disposition: inline keeps it from being treated as a
        # download. <img> embedding is governed by the embedding page's CSP (the
        # resource's own CSP is ignored for subresource loads), so images still render.
        return FileResponse(target, headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        })

    @web.get("/go")
    def go(request: Request, target: str, _p: Principal = Depends(require_user), **_):
        frm = request.query_params.get("from", "")
        rel = docs.resolve_link(target, frm)
        if rel:
            return RedirectResponse("/doc/" + quote(rel), status_code=302)
        return RedirectResponse("/new?path=" + quote(target), status_code=302)

    @web.get("/new", response_class=HTMLResponse)
    def new_get(request: Request, path: str = "", p: Principal = Depends(require_user)):
        return render("edit.html", request, is_new=True, path=path, title="", content="",
                      base_version=0, conflict=None, error=None, can_write=p.can_write,
                      folders=docs.folders())

    @web.post("/new")
    def new_post(request: Request, path: str = Form(...), content: str = Form(""),
                 title: str = Form(""), p: Principal = Depends(require_user)):
        try:
            doc = docs.create(p, path, content, title=title or None)
        except WikiError as e:
            return render("edit.html", request, status=e.http_status, is_new=True, path=path,
                          title=title, content=content, base_version=0, conflict=None,
                          error=e.message, can_write=p.can_write, folders=docs.folders())
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.get("/doc/{path:path}/edit", response_class=HTMLResponse)
    def edit_get(path: str, request: Request, p: Principal = Depends(require_user)):
        try:
            doc = docs.get(path)
        except WikiError:
            return RedirectResponse("/new?path=" + quote(path), status_code=303)
        return render("edit.html", request, is_new=False, path=doc["path"], title=doc["title"] or "",
                      content=doc["content"], base_version=doc["version"], conflict=None,
                      error=None, can_write=p.can_write)

    @web.post("/doc/{path:path}/edit")
    def edit_post(path: str, request: Request, content: str = Form(...),
                  base_version: int = Form(...), title: str = Form(""),
                  p: Principal = Depends(require_user)):
        try:
            doc = docs.update(p, path, base_version, content, title=title or None)
        except ConflictError as e:
            return render("edit.html", request, status=409, is_new=False, path=path, title=title,
                          content=content, base_version=e.extra.get("current_version"),
                          conflict=e.extra, error=None, can_write=p.can_write,
                          conflict_diff=_diff_lines(content, e.extra.get("current_content") or ""))
        except WikiError as e:
            return render("edit.html", request, status=e.http_status, is_new=False, path=path,
                          title=title, content=content, base_version=base_version, conflict=None,
                          error=e.message, can_write=p.can_write)
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.post("/doc/{path:path}/delete")
    def delete_post(path: str, request: Request, base_version: int = Form(None),
                    p: Principal = Depends(require_user)):
        try:
            docs.delete(p, path, base_version)
        except WikiError as e:
            request.session["flash"] = f"Delete failed: {e.message}"
            return RedirectResponse("/doc/" + quote(path), status_code=303)
        return RedirectResponse("/", status_code=303)

    @web.get("/doc/{path:path}/history", response_class=HTMLResponse)
    def history(path: str, request: Request, _p: Principal = Depends(require_user)):
        data = docs.revisions(path)
        return render("history.html", request, path=data["path"],
                      current_version=data["current_version"], revisions=data["revisions"])

    @web.get("/doc/{path:path}/raw")
    def raw(path: str, request: Request, _p: Principal = Depends(require_user)):
        doc = docs.get(path)
        filename = doc["path"].rsplit("/", 1)[-1]
        return PlainTextResponse(
            doc["content"], media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": content_disposition_attachment(filename)},
        )

    @web.get("/doc/{path:path}/rev/{version}", response_class=HTMLResponse)
    def revision_view(path: str, version: int, request: Request,
                      _p: Principal = Depends(require_user)):
        rev = docs.revision(path, version)
        html = render_markdown(rev["content"], rev["path"])
        return render("revision.html", request, rev=rev, html=html)

    @web.get("/doc/{path:path}/diff", response_class=HTMLResponse)
    def diff_view(path: str, request: Request, _p: Principal = Depends(require_user)):
        try:
            frm = int(request.query_params.get("from") or 0)
            to = int(request.query_params.get("to") or 0)
            a = docs.revision(path, frm)
            b = docs.revision(path, to)
        except (ValueError, WikiError) as e:
            msg = getattr(e, "message", "Invalid revision numbers.")
            return render("error.html", request, status=getattr(e, "http_status", 400), message=msg)
        current_version = docs.revisions(path, limit=1)["current_version"]
        return render("diff.html", request, path=a["path"], a=a, b=b,
                      current_version=current_version,
                      diff=_diff_lines(a["content"], b["content"]))

    @web.post("/doc/{path:path}/rev/{version}/restore")
    def restore_revision(path: str, version: int, request: Request,
                         p: Principal = Depends(require_user)):
        try:
            doc = docs.restore_revision(p, path, version)
        except ConflictError:
            request.session["flash"] = "복원 실패: 그 사이 다른 변경이 있었습니다. 다시 시도하세요."
            return RedirectResponse("/doc/" + quote(path) + "/history", status_code=303)
        except WikiError as e:
            request.session["flash"] = f"복원 실패: {e.message}"
            return RedirectResponse("/doc/" + quote(path) + "/history", status_code=303)
        request.session["flash"] = f"v{version} 내용으로 복원했습니다 (현재 v{doc['version']})."
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.get("/doc/{path:path}", response_class=HTMLResponse)
    def view(path: str, request: Request, _p: Principal = Depends(require_user)):
        try:
            doc = docs.get(path)
        except WikiError:
            return render("missing.html", request, path=path)
        html = render_markdown(doc["content"], doc["path"])
        backlinks = docs.backlinks(doc["path"])["backlinks"]
        outgoing = docs.links(doc["path"])["links"]
        stats = word_count(doc["content"])
        return render("view.html", request, doc=doc, html=html, backlinks=backlinks,
                      outgoing=outgoing, stats=stats)

    @web.get("/api/doc/{path:path}/related")
    def api_related(path: str, request: Request, _p: Principal = Depends(require_user)):
        # "관련 문서" runs several KNN scans; serve it lazily (fetched after page load by
        # related.js) so it stays off the synchronous critical path of the document view.
        try:
            related = docs.related(path, limit=6)["related"]
        except WikiError:
            related = []
        return JSONResponse({"ok": True, "related": related})

    # ---- settings (per-user API keys) -----------------------------------
    @web.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request, p: Principal = Depends(require_user)):
        return render("settings.html", request, keys=list_api_keys(db, p.user_id), new_key=None)

    @web.post("/settings/keys", response_class=HTMLResponse)
    def settings_create_key(request: Request, name: str = Form("key"),
                            p: Principal = Depends(require_user)):
        # Render the freshly-minted key directly in the response instead of
        # round-tripping it through the (signed-but-not-encrypted) session cookie.
        token = create_api_key(db, p.user_id, name)
        audit.record_tx(db, actor=p.username, via="web", action="key_mint", target=name)
        return render("settings.html", request, keys=list_api_keys(db, p.user_id), new_key=token)

    @web.post("/settings/keys/{key_id}/revoke")
    def settings_revoke_key(key_id: int, request: Request, p: Principal = Depends(require_user)):
        revoke_api_key(db, p.user_id, key_id)
        audit.record_tx(db, actor=p.username, via="web", action="key_revoke", target=str(key_id))
        return RedirectResponse("/settings", status_code=303)

    # ---- admin (require_admin dependency: 403 for non-admins, redirect if anon) ----
    @web.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request, _p: Principal = Depends(require_admin)):
        return render("admin.html", request, users=users_svc.list_users(db))

    @web.post("/admin/users")
    def admin_create(request: Request, username: str = Form(...), password: str = Form(...),
                     role: str = Form("editor"), p: Principal = Depends(require_admin)):
        try:
            create_user(db, username, password, role)
            audit.record_tx(db, actor=p.username, via="web", action="user_create",
                            target=username, detail=f"role={role}")
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/role")
    def admin_role(uid: int, request: Request, role: str = Form(...),
                   p: Principal = Depends(require_admin)):
        try:
            users_svc.set_role(db, uid, role)
            audit.record_tx(db, actor=p.username, via="web", action="role_change",
                            target=str(uid), detail=f"role={role}")
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/active")
    def admin_active(uid: int, request: Request, active: int = Form(...),
                     p: Principal = Depends(require_admin)):
        try:
            users_svc.set_active(db, uid, bool(active))
            audit.record_tx(db, actor=p.username, via="web", action="user_active",
                            target=str(uid), detail=f"active={bool(active)}")
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/password")
    def admin_password(uid: int, request: Request, password: str = Form(...),
                       p: Principal = Depends(require_admin)):
        try:
            users_svc.set_password(db, uid, password)
            audit.record_tx(db, actor=p.username, via="web", action="password_change", target=str(uid))
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/delete")
    def admin_delete(uid: int, request: Request, p: Principal = Depends(require_admin)):
        try:
            users_svc.delete_user(db, uid)
            audit.record_tx(db, actor=p.username, via="web", action="user_delete", target=str(uid))
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    # ---- realtime change stream (WebSocket) -----------------------------
    async def ws_changes(websocket: WebSocket) -> None:
        # Session-authenticated, same-origin read-only stream of doc_changed events.
        # Registered as a raw Starlette route so the global CSRF dependency (which
        # injects a Request) doesn't apply to the WebSocket handshake.
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.url.netloc:
            await websocket.close(code=1008)  # cross-origin -> reject (WS hijack guard)
            return
        if not principal_from_session(db, websocket.session.get("sid")):
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
                done, _ = await asyncio.wait({recv, getev}, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    getev.cancel()
                    break  # any inbound frame/disconnect ends this read-only channel
                await websocket.send_json(getev.result())
        except WebSocketDisconnect:
            pass
        finally:
            recv.cancel()
            app.events.unsubscribe(q)

    web.router.add_websocket_route("/ws", ws_changes)

    return web
