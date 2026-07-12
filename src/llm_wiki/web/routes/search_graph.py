"""Search, graph, tags, broken links, trash, and activity feed pages."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...services import audit
from ...services.auth import Principal
from ...services.errors import ConflictError, ValidationError, WikiError
from ...util import PathError, clamp_int, normalize_rel_path
from ..helpers import _ACTIVITY_WINDOWS, _window_since
from .deps import WebDeps


def register_search_graph(web: FastAPI, deps: WebDeps) -> None:
    db = deps.db
    docs = deps.docs
    render = deps.render
    require_user = deps.require_user
    audit_write_rejection = deps.audit_write_rejection
    write_action_for_path = deps.write_action_for_path
    read_limiter = deps.read_limiter

    @web.get("/tags", response_class=HTMLResponse)
    def tags_page(request: Request, p: Principal = Depends(require_user)):
        return render("tags.html", request, tags=docs.tags(), can_write=p.can_write)

    @web.post("/tags/rename")
    def tags_rename(request: Request, old: str = Form(...), new: str = Form(""),
                    p: Principal = Depends(require_user)):
        try:
            result = docs.rename_tag(p, old, new)
            request.session["flash"] = (
                f"태그 이름 변경: #{result['sources'][0]} → #{result['dest']} "
                f"({result['docs_changed']}개 문서)"
            )
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=old
            )
            request.session["flash"] = f"태그 이름 변경 실패: {e.message}"
        return RedirectResponse("/tags", status_code=303)

    @web.post("/tags/merge")
    def tags_merge(request: Request, dest: str = Form(...),
                   sources: list[str] = Form(default=[]),
                   p: Principal = Depends(require_user)):
        try:
            result = docs.merge_tags(p, sources, dest)
            src_label = ", ".join(f"#{s}" for s in result["sources"])
            request.session["flash"] = (
                f"태그 병합: {src_label} → #{result['dest']} "
                f"({result['docs_changed']}개 문서)"
            )
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path),
                target=dest,
            )
            request.session["flash"] = f"태그 병합 실패: {e.message}"
        return RedirectResponse("/tags", status_code=303)

    @web.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", mode: str = "hybrid", top_k: int = 20,
                    folder: str | None = None, tag: list[str] = Query(default=[]),
                    page: int = 1, per_page: int | None = None,
                    _p: Principal = Depends(require_user)):
        top_k = clamp_int(top_k, 1, 50)
        requested_per_page = top_k if per_page is None else clamp_int(per_page, 1, 50)
        page = max(1, page)
        tags = [value for value in tag if value.strip()]
        results = []
        truncated = False
        workbench_page = None
        if q.strip():
            rkey = f"read:{_p.user_id}"
            if not read_limiter.allowed(rkey):
                return render("search.html", request, status=429, q=q, mode=mode, top_k=top_k,
                              folder=folder or "", tag=tags[0] if tags else "", results=[], truncated=False,
                              folders=docs.folders(),
                              error="검색 요청이 너무 잦습니다. 잠시 후 다시 시도하세요.")
            if read_limiter.record_failure(rkey):
                audit.record_tx(db, actor=_p.username, via="web", action="read_rate_limited",
                                outcome="blocked", detail="search")
            try:
                workbench_page = docs.search_workbench_page(
                    q, mode=mode, page=page, per_page=requested_per_page,
                    folder=folder or None, tags=tags)
                top_k = workbench_page.per_page
                mode = workbench_page.filters.mode
                results = [r.to_dict() for r in workbench_page.items]
                truncated = workbench_page.has_next
            except ValidationError as e:
                # A malformed query (e.g. operator-only, or has:<unknown>) is a client
                # error — re-render the form inline with the message, not the error page.
                return render("search.html", request, status=400, q=q, mode=mode, top_k=top_k,
                              folder=folder or "", tag=tags[0] if tags else "", results=[], truncated=False,
                              folders=docs.folders(), error=e.message)
        return render("search.html", request, q=q, mode=mode, top_k=top_k,
                      folder=folder or "", tag=tags[0] if tags else "", results=results,
                      truncated=truncated, search_page=workbench_page, folders=docs.folders())

    @web.get("/graph", response_class=HTMLResponse)
    def graph_page(request: Request, root: str | None = None,
                   _p: Principal = Depends(require_user)):
        return render("graph.html", request, root=root or "")

    @web.get("/broken-links", response_class=HTMLResponse)
    def broken_links_page(request: Request, limit: int = 500,
                          p: Principal = Depends(require_user)):
        data = docs.broken_links(limit=clamp_int(limit, 1, 2000))
        return render("broken_links.html", request, count=data["count"], links=data["links"],
                      can_write=p.can_write)

    @web.post("/broken-links/create")
    def broken_links_create(request: Request, target: str = Form(...),
                            p: Principal = Depends(require_user)):
        # Bare wiki names (e.g. "Ghost Note") and path-like targets both normalize
        # to a vault-relative ``.md`` path via the shared path helper.
        try:
            rel = normalize_rel_path(target)
        except PathError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=target
            )
            request.session["flash"] = f"문서 만들기 실패: {e}"
            return RedirectResponse("/broken-links", status_code=303)
        try:
            doc = docs.create(p, rel, "", title=None)
            request.session["flash"] = f"문서를 만들었습니다: {doc['path']}"
            return RedirectResponse("/doc/" + quote(doc["path"]) + "/edit", status_code=303)
        except ConflictError:
            # Already exists (race or re-click) — open the live document instead.
            try:
                existing = docs.get(rel)
                request.session["flash"] = f"이미 있는 문서입니다: {existing['path']}"
                return RedirectResponse("/doc/" + quote(existing["path"]), status_code=303)
            except WikiError:
                request.session["flash"] = f"이미 있는 경로입니다: {rel}"
                return RedirectResponse("/doc/" + quote(rel), status_code=303)
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=rel
            )
            request.session["flash"] = f"문서 만들기 실패: {e.message}"
            return RedirectResponse("/broken-links", status_code=303)

    @web.get("/trash", response_class=HTMLResponse)
    def trash_page(request: Request, p: Principal = Depends(require_user)):
        if not p.can_write:
            return render("error.html", request, status=403,
                          message="휴지통은 편집자 이상만 볼 수 있습니다.")
        return render("trash.html", request, items=docs.list_deleted(limit=200),
                      is_admin=p.can_admin)

    @web.post("/trash/{path:path}/restore")
    def trash_restore(path: str, request: Request, p: Principal = Depends(require_user)):
        try:
            docs.restore(p, path)
            request.session["flash"] = f"복원했습니다: {path}"
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            request.session["flash"] = f"복원 실패: {e.message}"
        return RedirectResponse("/trash", status_code=303)

    @web.post("/trash/{path:path}/purge")
    def trash_purge(path: str, request: Request, p: Principal = Depends(require_user)):
        try:
            docs.purge(p, path)
            request.session["flash"] = f"완전히 삭제했습니다: {path}"
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            request.session["flash"] = f"삭제 실패: {e.message}"
        return RedirectResponse("/trash", status_code=303)

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
        since = _window_since(window)
        events = audit.recent(db, limit=300, since=since,
                              via=via_f, action=(action or None), actions=scope)
        # Summary counts for the window (unfiltered by via so chips show full split).
        via_summary = audit.via_counts(db, since=since, actions=scope)
        return render("activity.html", request, events=events, window=window,
                      windows=_ACTIVITY_WINDOWS, via=via_f or "", action=action or "",
                      is_admin=p.can_admin, doc_actions=audit.DOC_ACTIONS,
                      via_summary=via_summary)
