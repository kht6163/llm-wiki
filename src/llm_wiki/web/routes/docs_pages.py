"""Document list, create/edit/view/history, and related page routes."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ...markdown_render import render_markdown
from ...markdown_utils import document_properties
from ...services.auth import Principal
from ...services.documents import ProjectionPendingError
from ...services.errors import ConflictError, WikiError
from ...util import PathError, content_disposition_attachment, word_count
from ..helpers import _diff_lines
from .deps import WebDeps


def register_docs_pages(web: FastAPI, deps: WebDeps) -> None:
    docs = deps.docs
    render = deps.render
    embed_resolver = deps.embed_resolver
    require_user = deps.require_user
    require_user_or_bearer = deps.require_user_or_bearer
    audit_write_rejection = deps.audit_write_rejection
    write_action_for_path = deps.write_action_for_path

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

    @web.post("/daily")
    def daily(request: Request, p: Principal = Depends(require_user)):
        # Open (creating if absent) today's daily note and jump to it — the journaling
        # entry point. Idempotent: returns the existing note for any role; only creating
        # one needs write (a viewer hitting a missing note gets a flash + home).
        try:
            d = docs.daily_note(p)
        except ProjectionPendingError as e:
            request.session["flash"] = (
                "오늘 노트는 저장됐지만 파일 반영이 지연되고 있습니다. "
                "서버가 자동 복구합니다."
            )
            return RedirectResponse(
                "/doc/" + quote(e.extra.get("path") or "daily"), status_code=303
            )
        except WikiError as e:
            request.session["flash"] = e.message
            return RedirectResponse("/", status_code=303)
        return RedirectResponse("/doc/" + quote(d["path"]), status_code=303)

    @web.get("/go")
    def go(request: Request, target: str, _p: Principal = Depends(require_user), **_):
        frm = request.query_params.get("from", "")
        rel = docs.resolve_link(target, frm)
        if rel:
            return RedirectResponse("/doc/" + quote(rel), status_code=302)
        return RedirectResponse("/new?path=" + quote(target), status_code=302)

    @web.get("/new", response_class=HTMLResponse)
    def new_get(request: Request, path: str = "", template: str = "",
                p: Principal = Depends(require_user)):
        content = ""
        if template:
            try:
                content = docs._load_template_body(template)
            except WikiError:
                content = ""
        return render("edit.html", request, is_new=True, path=path, title="", content=content,
                      base_version=0, conflict=None, error=None, can_write=p.can_write,
                      folders=docs.list_folders(), templates=docs.list_templates(),
                      selected_template=template or "")

    @web.post("/new")
    def new_post(request: Request, path: str = Form(...), content: str = Form(""),
                 title: str = Form(""), template: str = Form(""),
                 p: Principal = Depends(require_user)):
        # Template select is for GET prefill; on POST, only apply when the body is still
        # empty so a user who edited after selecting a template keeps their edits.
        tpl = (template.strip() or None) if not (content or "").strip() else None
        try:
            doc = docs.create(p, path, content, title=title or None, template=tpl)
        except ProjectionPendingError as e:
            request.session["flash"] = (
                "문서는 저장됐지만 파일 반영이 지연되고 있습니다. 서버가 자동 복구합니다."
            )
            return RedirectResponse("/doc/" + quote(e.extra.get("path") or path), status_code=303)
        except PathError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            # Stay on the form with the typed content preserved (an invalid path is a
            # field error, not a dead end) instead of bouncing to the global error page.
            return render("edit.html", request, status=400, is_new=True, path=path,
                          title=title, content=content, base_version=0, conflict=None,
                          error=f"잘못된 경로입니다: {e}", can_write=p.can_write,
                          folders=docs.list_folders(), templates=docs.list_templates(),
                          selected_template=template or "")
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            return render("edit.html", request, status=e.http_status, is_new=True, path=path,
                          title=title, content=content, base_version=0, conflict=None,
                          error=e.message, can_write=p.can_write,
                          folders=docs.list_folders(), templates=docs.list_templates(),
                          selected_template=template or "")
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
        except ProjectionPendingError as e:
            request.session["flash"] = (
                "변경은 저장됐지만 파일 반영이 지연되고 있습니다. 같은 편집을 다시 저장하지 마세요."
            )
            return RedirectResponse("/doc/" + quote(e.extra.get("path") or path), status_code=303)
        except ConflictError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            try:
                merge_preview = docs.merge_preview(p, path, base_version, content, title)
            except (WikiError, RuntimeError):
                merge_preview = {
                    "base_version": base_version,
                    "current_version": e.extra.get("current_version"),
                    "updated_by": e.extra.get("updated_by"),
                    "updated_at": e.extra.get("updated_at"),
                    "current_via": e.extra.get("current_via"),
                    "base": None,
                    "base_title": None,
                    "mine": content,
                    "mine_title": title,
                    "current": e.extra.get("current_content") or "",
                    "current_title": e.extra.get("current_title"),
                    "merged_title": title if title == e.extra.get("current_title") else None,
                    "title_conflict": title != e.extra.get("current_title"),
                    "merged": None,
                    "conflicts": [],
                    "manual_only": True,
                }
            conflict = {
                **e.extra,
                "current_version": merge_preview["current_version"],
                "current_content": merge_preview["current"],
                "updated_by": merge_preview["updated_by"],
                "updated_at": merge_preview["updated_at"],
                "current_via": merge_preview["current_via"],
            }
            resolved_title = (
                merge_preview["merged_title"]
                if not merge_preview["title_conflict"]
                else title
            )
            return render("edit.html", request, status=409, is_new=False, path=path,
                          title=resolved_title,
                          content=content, base_version=merge_preview["current_version"],
                          conflict=conflict, error=None, can_write=p.can_write,
                          merge_preview=merge_preview,
                          conflict_diff=_diff_lines(content, merge_preview["current"]))
        except PathError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            return render("edit.html", request, status=400, is_new=False, path=path,
                          title=title, content=content, base_version=base_version, conflict=None,
                          error=f"잘못된 경로입니다: {e}", can_write=p.can_write)
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            return render("edit.html", request, status=e.http_status, is_new=False, path=path,
                          title=title, content=content, base_version=base_version, conflict=None,
                          error=e.message, can_write=p.can_write)
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.post("/doc/{path:path}/delete")
    def delete_post(path: str, request: Request, base_version: int = Form(None),
                    p: Principal = Depends(require_user)):
        try:
            docs.delete(p, path, base_version)
        except ProjectionPendingError:
            request.session["flash"] = (
                "삭제는 저장됐지만 파일 정리가 지연되고 있습니다. 서버가 자동 복구합니다."
            )
            return RedirectResponse("/", status_code=303)
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            request.session["flash"] = f"Delete failed: {e.message}"
            return RedirectResponse("/doc/" + quote(path), status_code=303)
        return RedirectResponse("/", status_code=303)

    @web.get("/doc/{path:path}/history", response_class=HTMLResponse)
    def history(path: str, request: Request, _p: Principal = Depends(require_user)):
        data = docs.revisions(path)
        return render("history.html", request, path=data["path"],
                      current_version=data["current_version"], revisions=data["revisions"])

    @web.get("/doc/{path:path}/raw")
    def raw(path: str, request: Request, _p: Principal = Depends(require_user_or_bearer)):
        # Dual auth (session OR Bearer): the raw .md is the target of every /llms.txt
        # link, so an API-key agent must be able to GET it the same way it fetched the index.
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
        html = render_markdown(rev["content"], rev["path"],
                               resolve_embed=embed_resolver(rev["path"]))
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
        except ProjectionPendingError as e:
            request.session["flash"] = (
                "복원은 저장됐지만 파일 반영이 지연되고 있습니다. 같은 복원을 반복하지 마세요."
            )
            return RedirectResponse(
                "/doc/" + quote(e.extra.get("path") or path), status_code=303
            )
        except ConflictError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            request.session["flash"] = "복원 실패: 그 사이 다른 변경이 있었습니다. 다시 시도하세요."
            return RedirectResponse("/doc/" + quote(path) + "/history", status_code=303)
        except WikiError as e:
            audit_write_rejection(
                request, p, e, action=write_action_for_path(request.url.path), target=path
            )
            request.session["flash"] = f"복원 실패: {e.message}"
            return RedirectResponse("/doc/" + quote(path) + "/history", status_code=303)
        request.session["flash"] = f"v{version} 내용으로 복원했습니다 (현재 v{doc['version']})."
        return RedirectResponse("/doc/" + quote(doc["path"]), status_code=303)

    @web.get("/doc/{path:path}", response_class=HTMLResponse)
    def view(path: str, request: Request, p: Principal = Depends(require_user)):
        try:
            doc = docs.get(path)
        except WikiError:
            return render("missing.html", request, path=path)
        html = render_markdown(doc["content"], doc["path"],
                               resolve_embed=embed_resolver(doc["path"]))
        backlinks = docs.backlinks(doc["path"], with_context=True)["backlinks"]
        outgoing = docs.links(doc["path"])["links"]
        stats = word_count(doc["content"])
        properties = document_properties(doc["content"])
        return render("view.html", request, doc=doc, html=html, backlinks=backlinks,
                      outgoing=outgoing, stats=stats, properties=properties,
                      favorite=docs.is_favorite(p.user_id, doc["path"]))

    @web.post("/doc/{path:path}/favorite")
    def doc_favorite(path: str, request: Request, p: Principal = Depends(require_user)):
        try:
            docs.toggle_favorite(p, path)
        except WikiError as e:
            request.session["flash"] = e.message
        return RedirectResponse("/doc/" + quote(path), status_code=303)
