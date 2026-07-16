"""JSON/API endpoints, attachments, and share-token minting."""
from __future__ import annotations

import anyio
from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from ...markdown_render import render_markdown
from ...services import audit
from ...services.auth import Principal
from ...services.errors import ForbiddenError, NotFoundError, ValidationError
from ...util import clamp_int
from ..helpers import _ATTACH_MIME, _read_capped
from .deps import WebDeps


def register_api(web: FastAPI, deps: WebDeps) -> None:
    db = deps.db
    docs = deps.docs
    secret = deps.secret
    embed_resolver = deps.embed_resolver
    require_user = deps.require_user

    @web.post("/api/doc/{path:path}/share")
    def mint_share_link(path: str, request: Request, p: Principal = Depends(require_user)):
        from ...services import share as share_svc

        if not p.can_write:
            raise ForbiddenError("Only editors can mint share links.")
        token = share_svc.mint_share_token(secret, path, db=db, principal=p)
        url = str(request.base_url).rstrip("/") + "/share/" + token
        links = share_svc.list_share_links(db, p, path)
        return JSONResponse(
            {"ok": True, "path": path, "token": token, "url": url, "link": links[0]}
        )

    @web.get("/api/doc/{path:path}/shares")
    def list_share_links(path: str, request: Request, p: Principal = Depends(require_user)):
        from ...services import share as share_svc

        return JSONResponse({"ok": True, "links": share_svc.list_share_links(db, p, path)})

    @web.post("/api/shares/{link_id}/revoke")
    def revoke_share_link(link_id: int, request: Request, p: Principal = Depends(require_user)):
        from ...services import share as share_svc

        return JSONResponse({"ok": True, **share_svc.revoke_share_link(db, p, link_id)})

    @web.get("/api/graph")
    def api_graph(
        request: Request,
        root: str | None = None,
        depth: int = 1,
        limit: int = 500,
        folder: str | None = None,
        tag: list[str] = Query(default=[]),
        include_unresolved: bool = True,
        _p: Principal = Depends(require_user),
    ):
        tags = [value for value in tag if value and value.strip()]
        return JSONResponse(
            docs.graph(
                root=root or None,
                depth=depth,
                limit=limit,
                folder=folder or None,
                tags=tags or None,
                include_unresolved=include_unresolved,
            )
        )

    @web.get("/api/complete")
    def api_complete(request: Request, q: str = "", _p: Principal = Depends(require_user)):
        return JSONResponse({"ok": True, "items": docs.complete(q, limit=12)})

    @web.get("/api/tree")
    def api_tree(request: Request, _p: Principal = Depends(require_user)):
        # Live tree payload so the sidebar can refresh after a folder/doc change
        # without a full page reload. Cached (invalidated on the same structural writes
        # that triggered this refresh), so repeated refreshes don't re-scan the vault.
        return JSONResponse({"ok": True, "tree": docs.nav_tree()})

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

    @web.post("/api/doc/{path:path}/properties")
    async def api_doc_properties(path: str, request: Request, p: Principal = Depends(require_user)):
        # Replace the whole editable frontmatter property set in one revision. JSON body:
        # {base_version?, properties: [{key, values: [..] | "a, b"}]}.
        data = await request.json()
        base_version = data.get("base_version")
        props: list[tuple[str, list[str]]] = []
        for item in data.get("properties") or []:
            key = str((item or {}).get("key") or "")
            values = (item or {}).get("values")
            if isinstance(values, str):
                values = [v for v in (s.strip() for s in values.split(",")) if v]
            elif isinstance(values, list):
                values = [str(v) for v in values]
            else:
                values = []
            props.append((key, values))
        doc = await anyio.to_thread.run_sync(
            lambda: docs.replace_properties(p, path, props, base_version=base_version)
        )
        return JSONResponse({"ok": True, "version": doc["version"]})

    @web.post("/api/preview")
    def api_preview(request: Request, content: str = Form(""), path: str = Form("preview.md"),
                    _p: Principal = Depends(require_user)):
        target = path or "preview.md"
        return JSONResponse({"ok": True, "html": render_markdown(
            content, target, resolve_embed=embed_resolver(target))})

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
            "html": render_markdown(doc["content"], doc["path"],
                                    resolve_embed=embed_resolver(doc["path"])),
        })

    @web.post("/api/upload")
    async def api_upload(request: Request, file: UploadFile = File(...),
                         p: Principal = Depends(require_user)):
        # Resolve via web.app so tests can monkeypatch ATTACH_MAX_BYTES there
        # (same binding surface as the pre-split nested handlers).
        from llm_wiki.web import app as web_app

        limit = web_app.ATTACH_MAX_BYTES
        data = await _read_capped(file, limit)
        if data is None:
            raise ValidationError(f"Attachment too large (limit {limit} bytes).")
        def persist_upload() -> dict:
            result = docs.save_attachment(p, file.filename or "file", data)
            audit.record_tx(
                db,
                actor=p.username,
                via="web",
                action="attachment_upload",
                target=result["path"],
            )
            return result

        res = await anyio.to_thread.run_sync(persist_upload)
        return JSONResponse({"ok": True, **res})

    @web.get("/attachments/{subpath:path}")
    def attachment(subpath: str, request: Request, _p: Principal = Depends(require_user)):
        target, data = docs.attachment_bytes(subpath)
        # Serve with an explicit, known Content-Type so nosniff has a correct type to
        # pin (unknown -> octet-stream, never a guessed renderable type).
        media = _ATTACH_MIME.get(target.suffix.lower(), "application/octet-stream")
        # Hardened CSP overrides the site default for this resource: an SVG opened
        # directly as a document must not execute scripts at all. The explicit
        # script-src 'none' is unambiguous, sandbox strips same-origin/JS as defense
        # in depth, and Content-Disposition: inline keeps it from being treated as a
        # download. <img> embedding is governed by the embedding page's CSP (the
        # resource's own CSP is ignored for subresource loads), so images still render.
        return Response(data, media_type=media, headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
        })

    @web.get("/api/doc/{path:path}/related")
    def api_related(path: str, request: Request, _p: Principal = Depends(require_user)):
        # "관련 문서" runs several KNN scans; serve it lazily (fetched after page load by
        # related.js) so it stays off the synchronous critical path of the document view.
        try:
            related = docs.related(path, limit=6)["related"]
        except NotFoundError:
            related = []
        return JSONResponse({"ok": True, "related": related})

    @web.get("/api/doc/{path:path}/activity")
    def api_doc_activity(
        path: str,
        request: Request,
        limit: int = 30,
        _p: Principal = Depends(require_user),
    ):
        # Per-document audit timeline (lazy-loaded by activity.js). Session auth only;
        # scoped to DOC_TIMELINE_ACTIONS and path / move-from / move-to targets.
        events = audit.recent(
            db,
            limit=clamp_int(limit, 1, 100),
            target_path=path,
            actions=audit.DOC_TIMELINE_ACTIONS,
        )
        return JSONResponse({"ok": True, "path": path, "events": events})
