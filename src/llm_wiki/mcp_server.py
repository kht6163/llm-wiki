"""HTTP MCP server (streamable-http). LLM clients authenticate with a per-user
API key via ``Authorization: Bearer <key>``; the resolved Principal's role gates
write tools. All tool bodies run in a worker thread so the event loop isn't
blocked by SQLite / embedding work.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP

from .runtime import AppContext
from .search import search as run_search
from .services.auth import Principal, principal_from_api_key
from .services.errors import UnauthorizedError, WikiError


def _bearer_token(ctx: Context) -> str | None:
    req = getattr(ctx.request_context, "request", None)
    if req is None:
        return None
    auth = req.headers.get("authorization")
    if not auth:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def create_mcp_server(app: AppContext) -> FastMCP:
    db, embedder, docs = app.db, app.embedder, app.docs
    mcp = FastMCP(name="llm-wiki", stateless_http=True, json_response=True)

    def _principal(token: str | None) -> Principal:
        p = principal_from_api_key(db, token)
        if not p:
            raise UnauthorizedError(
                "Missing or invalid API key. Send header 'Authorization: Bearer <api_key>'."
            )
        return p

    async def _call(token: str | None, fn: Callable[[Principal], Any]) -> dict:
        def impl() -> dict:
            try:
                return fn(_principal(token))
            except WikiError as e:
                return e.to_dict()
        return await anyio.to_thread.run_sync(impl)

    # ---- read tools (any authenticated role) ----------------------------
    @mcp.tool(description="Hybrid search (BM25 + embedding vector, RRF-fused). "
                          "mode: hybrid|bm25|vector. top_k is clamped to 1..50. Returns "
                          "ranked path/title/score/snippet. 'count' is the number of hits "
                          "returned; 'truncated' true means more matches likely exist beyond top_k.")
    async def search_documents(
        ctx: Context, query: str, mode: str = "hybrid", top_k: int = 10,
        folder: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        token = _bearer_token(ctx)

        def fn(_p: Principal) -> dict:
            results = run_search(db, embedder, query, mode=mode, top_k=top_k,
                                 folder=folder, tags=tags)
            return {"ok": True, "mode": mode, "count": len(results),
                    "truncated": len(results) >= max(1, min(int(top_k), 50)),
                    "results": [r.to_dict() for r in results]}
        return await _call(token, fn)

    @mcp.tool(description="Read a document's full content. The returned 'version' is "
                          "the base_version you must echo back to update_document.")
    async def read_document(ctx: Context, path: str) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, **docs.get(path)})

    @mcp.tool(description="List documents, optionally filtered by folder/tag. limit is "
                          "clamped to 1..1000; sort: updated_at|title|path. Returns 'count' "
                          "(this page), 'total' (all matches), and 'has_more' for paging.")
    async def list_documents(
        ctx: Context, folder: str | None = None, tag: str | None = None,
        limit: int = 100, offset: int = 0, sort: str = "updated_at",
    ) -> dict:
        token = _bearer_token(ctx)

        def fn(_p: Principal) -> dict:
            items = docs.list_docs(folder=folder, tag=tag, limit=limit, offset=offset, sort=sort)
            total = docs.count(folder=folder, tag=tag)
            return {"ok": True, "count": len(items), "total": total, "offset": offset,
                    "has_more": offset + len(items) < total, "documents": items}
        return await _call(token, fn)

    @mcp.tool(description="List the tag vocabulary with usage counts (most-used first). "
                          "Use this to discover exact tag strings for the 'tag'/'tags' filters.")
    async def get_tags(ctx: Context) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, "tags": docs.tags()})

    @mcp.tool(description="Outgoing links of a document (resolved + broken).")
    async def get_links(ctx: Context, path: str) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, **docs.links(path)})

    @mcp.tool(description="Documents that link TO this document (backlinks).")
    async def get_backlinks(ctx: Context, path: str) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, **docs.backlinks(path)})

    @mcp.tool(description="Revision history (versions, authors, timestamps) for a document.")
    async def get_revisions(ctx: Context, path: str, limit: int = 100) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, **docs.revisions(path, limit=limit)})

    @mcp.tool(description="Fetch the full content of a specific past revision.")
    async def get_revision(ctx: Context, path: str, version: int) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: {"ok": True, **docs.revision(path, version)})

    @mcp.tool(description="Link graph as {nodes, edges} (Cytoscape/D3). root=None for the "
                          "whole vault; otherwise BFS to 'depth' around the root document.")
    async def get_graph(
        ctx: Context, root: str | None = None, depth: int = 1,
        limit: int = 500, include_unresolved: bool = True,
    ) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda _p: docs.graph(
            root=root, depth=depth, limit=limit, include_unresolved=include_unresolved))

    # ---- write tools (editor/admin) -------------------------------------
    @mcp.tool(description="Create a new document. Fails with code 'conflict' if the path "
                          "already exists, 'forbidden' for viewer role.")
    async def create_document(
        ctx: Context, path: str, content: str,
        title: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda p: {"ok": True, **docs.create(p, path, content, title, tags)})

    @mcp.tool(description="Update a document. base_version is REQUIRED; if it does not match "
                          "the current version the update is rejected with code 'conflict' and "
                          "the current content, so you can re-read, reapply, and retry.")
    async def update_document(
        ctx: Context, path: str, base_version: int, content: str,
        title: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda p: {"ok": True, **docs.update(
            p, path, base_version, content, title, tags)})

    @mcp.tool(description="Delete (soft) a document. Pass base_version to guard against "
                          "deleting a version you haven't seen.")
    async def delete_document(ctx: Context, path: str, base_version: int | None = None) -> dict:
        token = _bearer_token(ctx)
        return await _call(token, lambda p: docs.delete(p, path, base_version))

    return mcp
