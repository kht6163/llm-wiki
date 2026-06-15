"""FastAPI web UI: Obsidian-like markdown viewer/editor, revision history, link
graph, search, API-key self-service, and admin user management. Handlers are sync
so FastAPI runs them in a threadpool (off the event loop) while they do SQLite /
embedding work. Authorization is delegated to the shared service layer.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import bleach
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..markdown_render import render_markdown
from ..runtime import AppContext
from ..search import search as run_search
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
from ..services.errors import ConflictError, WikiError

_HERE = Path(__file__).parent


def create_web_app(app: AppContext) -> FastAPI:
    db, embedder, docs = app.db, app.embedder, app.docs
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["urlpath"] = lambda s: quote(str(s))
    # Search snippets carry FTS <mark> tags around raw document text; allow only
    # <mark> through so stored content can't inject HTML into the results page.
    templates.env.filters["snippet"] = lambda s: bleach.clean(s or "", tags=["mark"], strip=True)

    web = FastAPI(title="llm-wiki")
    secret = get_or_create_session_secret(db, app.settings.session_secret)
    web.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=14 * 86400)
    web.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def user(request: Request) -> Principal | None:
        return principal_from_session(db, request.session.get("sid"))

    def render(name: str, request: Request, status: int = 200, **kw) -> HTMLResponse:
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request, name, {"user": user(request), "flash": flash, **kw}, status_code=status
        )

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    # ---- auth -----------------------------------------------------------
    @web.get("/login", response_class=HTMLResponse)
    def login_get(request: Request):
        if user(request):
            return RedirectResponse("/", status_code=303)
        return render("login.html", request, error=None)

    @web.post("/login", response_class=HTMLResponse)
    def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
        p = authenticate(db, username, password)
        if not p:
            return render("login.html", request, status=401, error="Invalid username or password.")
        request.session["sid"] = create_session(db, p.user_id)
        return RedirectResponse("/", status_code=303)

    @web.get("/logout")
    def logout(request: Request):
        delete_session(db, request.session.get("sid"))
        request.session.clear()
        return login_redirect()

    @web.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True})

    # ---- documents ------------------------------------------------------
    @web.get("/", response_class=HTMLResponse)
    def home(request: Request, folder: str | None = None, tag: str | None = None):
        if not user(request):
            return login_redirect()
        items = docs.list(folder=folder, tag=tag, limit=1000)
        return render("list.html", request, items=items, folder=folder, tag=tag)

    @web.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", mode: str = "hybrid", top_k: int = 20):
        if not user(request):
            return login_redirect()
        results = []
        if q.strip():
            results = [r.to_dict() for r in run_search(db, embedder, q, mode=mode, top_k=top_k)]
        return render("search.html", request, q=q, mode=mode, results=results)

    @web.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request, root: str | None = None):
        if not user(request):
            return login_redirect()
        return render("graph.html", request, root=root or "")

    @web.get("/api/graph")
    def api_graph(request: Request, root: str | None = None, depth: int = 1, limit: int = 500):
        if not user(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return JSONResponse(docs.graph(root=root or None, depth=depth, limit=limit))

    @web.get("/go")
    def go(request: Request, target: str, **_):
        if not user(request):
            return login_redirect()
        frm = request.query_params.get("from", "")
        rel = docs.resolve_link(target, frm)
        if rel:
            return RedirectResponse("/doc/" + quote(rel), status_code=302)
        return RedirectResponse("/new?path=" + quote(target), status_code=302)

    @web.get("/new", response_class=HTMLResponse)
    def new_get(request: Request, path: str = ""):
        p = user(request)
        if not p:
            return login_redirect()
        return render("edit.html", request, is_new=True, path=path, title="", content="",
                      base_version=0, conflict=None, error=None, can_write=p.can_write)

    @web.post("/new")
    def new_post(request: Request, path: str = Form(...), content: str = Form(""), title: str = Form("")):
        p = user(request)
        if not p:
            return login_redirect()
        try:
            doc = docs.create(p, path, content, title=title or None)
        except WikiError as e:
            return render("edit.html", request, status=e.http_status, is_new=True, path=path,
                          title=title, content=content, base_version=0, conflict=None,
                          error=e.message, can_write=p.can_write)
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.get("/doc/{path:path}/edit", response_class=HTMLResponse)
    def edit_get(path: str, request: Request):
        p = user(request)
        if not p:
            return login_redirect()
        try:
            doc = docs.get(path)
        except WikiError:
            return RedirectResponse("/new?path=" + quote(path), status_code=303)
        return render("edit.html", request, is_new=False, path=doc["path"], title=doc["title"] or "",
                      content=doc["content"], base_version=doc["version"], conflict=None,
                      error=None, can_write=p.can_write)

    @web.post("/doc/{path:path}/edit")
    def edit_post(path: str, request: Request, content: str = Form(...),
                  base_version: int = Form(...), title: str = Form("")):
        p = user(request)
        if not p:
            return login_redirect()
        try:
            doc = docs.update(p, path, base_version, content, title=title or None)
        except ConflictError as e:
            return render("edit.html", request, status=409, is_new=False, path=path, title=title,
                          content=content, base_version=e.extra.get("current_version"),
                          conflict=e.extra, error=None, can_write=p.can_write)
        except WikiError as e:
            return render("edit.html", request, status=e.http_status, is_new=False, path=path,
                          title=title, content=content, base_version=base_version, conflict=None,
                          error=e.message, can_write=p.can_write)
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.post("/doc/{path:path}/delete")
    def delete_post(path: str, request: Request, base_version: int = Form(None)):
        p = user(request)
        if not p:
            return login_redirect()
        try:
            docs.delete(p, path, base_version)
        except WikiError as e:
            request.session["flash"] = f"Delete failed: {e.message}"
            return RedirectResponse("/doc/" + quote(path), status_code=303)
        return RedirectResponse("/", status_code=303)

    @web.get("/doc/{path:path}/history", response_class=HTMLResponse)
    def history(path: str, request: Request):
        if not user(request):
            return login_redirect()
        try:
            data = docs.revisions(path)
        except WikiError as e:
            return render("error.html", request, status=e.http_status, message=e.message)
        return render("history.html", request, path=data["path"],
                      current_version=data["current_version"], revisions=data["revisions"])

    @web.get("/doc/{path:path}/rev/{version}", response_class=HTMLResponse)
    def revision_view(path: str, version: int, request: Request):
        if not user(request):
            return login_redirect()
        try:
            rev = docs.revision(path, version)
        except WikiError as e:
            return render("error.html", request, status=e.http_status, message=e.message)
        html = render_markdown(rev["content"], rev["path"])
        return render("revision.html", request, rev=rev, html=html)

    @web.get("/doc/{path:path}", response_class=HTMLResponse)
    def view(path: str, request: Request):
        if not user(request):
            return login_redirect()
        try:
            doc = docs.get(path)
        except WikiError:
            return render("missing.html", request, path=path)
        html = render_markdown(doc["content"], doc["path"])
        backlinks = docs.backlinks(doc["path"])["backlinks"]
        outgoing = docs.links(doc["path"])["links"]
        return render("view.html", request, doc=doc, html=html, backlinks=backlinks, outgoing=outgoing)

    # ---- settings (per-user API keys) -----------------------------------
    @web.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request):
        p = user(request)
        if not p:
            return login_redirect()
        new_key = request.session.pop("new_key", None)
        return render("settings.html", request, keys=list_api_keys(db, p.user_id), new_key=new_key)

    @web.post("/settings/keys")
    def settings_create_key(request: Request, name: str = Form("key")):
        p = user(request)
        if not p:
            return login_redirect()
        request.session["new_key"] = create_api_key(db, p.user_id, name)
        return RedirectResponse("/settings", status_code=303)

    @web.post("/settings/keys/{key_id}/revoke")
    def settings_revoke_key(key_id: int, request: Request):
        p = user(request)
        if not p:
            return login_redirect()
        revoke_api_key(db, p.user_id, key_id)
        return RedirectResponse("/settings", status_code=303)

    # ---- admin ----------------------------------------------------------
    def _require_admin(request: Request):
        p = user(request)
        if not p:
            return None, login_redirect()
        if not p.can_admin:
            return None, render("error.html", request, status=403, message="Admin only.")
        return p, None

    @web.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request):
        p, resp = _require_admin(request)
        if resp:
            return resp
        return render("admin.html", request, users=users_svc.list_users(db))

    @web.post("/admin/users")
    def admin_create(request: Request, username: str = Form(...), password: str = Form(...),
                     role: str = Form("editor")):
        p, resp = _require_admin(request)
        if resp:
            return resp
        try:
            create_user(db, username, password, role)
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/role")
    def admin_role(uid: int, request: Request, role: str = Form(...)):
        p, resp = _require_admin(request)
        if resp:
            return resp
        try:
            users_svc.set_role(db, uid, role)
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/active")
    def admin_active(uid: int, request: Request, active: int = Form(...)):
        p, resp = _require_admin(request)
        if resp:
            return resp
        try:
            users_svc.set_active(db, uid, bool(active))
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/password")
    def admin_password(uid: int, request: Request, password: str = Form(...)):
        p, resp = _require_admin(request)
        if resp:
            return resp
        try:
            users_svc.set_password(db, uid, password)
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    @web.post("/admin/users/{uid}/delete")
    def admin_delete(uid: int, request: Request):
        p, resp = _require_admin(request)
        if resp:
            return resp
        try:
            users_svc.delete_user(db, uid)
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/admin/users", status_code=303)

    return web
