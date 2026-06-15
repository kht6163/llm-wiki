"""HTTP MCP server (streamable-http). LLM clients authenticate with a per-user
API key via ``Authorization: Bearer <key>``; the resolved Principal's role gates
write tools. All tool bodies run in a worker thread so the event loop isn't
blocked by SQLite / embedding work.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .metrics import MCP_CALLS, MCP_LATENCY
from .ratelimit import RateLimiter
from .runtime import AppContext
from .search import search as run_search
from .services.auth import Principal, principal_from_api_key
from .services.errors import UnauthorizedError, WikiError

log = logging.getLogger("llm_wiki.mcp")


def _request(ctx: Context):
    # ctx.request_context raises if accessed outside an active request (e.g. a
    # direct call_tool in tests). Treat that as "no request".
    try:
        return getattr(ctx.request_context, "request", None)
    except Exception:
        return None


def _bearer_token(ctx: Context) -> str | None:
    req = _request(ctx)
    if req is None:
        return None
    auth = req.headers.get("authorization")
    if not auth:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


def _client_ip(ctx: Context) -> str:
    req = _request(ctx)
    client = getattr(req, "client", None) if req is not None else None
    return getattr(client, "host", None) or "?"


def create_mcp_server(app: AppContext) -> FastMCP:
    db, embedder, docs = app.db, app.embedder, app.docs
    mcp = FastMCP(name="llm-wiki", stateless_http=True, json_response=True)
    # Throttle Bearer-auth failures per client IP so a leaked endpoint can't be
    # used to brute-force API keys (the web login is limited separately).
    auth_limiter = RateLimiter(max_attempts=10, window_s=300.0)

    def _principal(token: str | None) -> Principal:
        p = principal_from_api_key(db, token)
        if not p:
            raise UnauthorizedError(
                "Missing or invalid API key. Send header 'Authorization: Bearer <api_key>'."
            )
        return p

    async def _call(ctx: Context, fn: Callable[[Principal], Any], tool: str = "?") -> dict:
        token = _bearer_token(ctx)
        ip_key = f"ip:{_client_ip(ctx)}"

        def impl() -> dict:
            t0 = time.monotonic()
            actor = (token or "")[:12] or "-"  # key prefix only — never log the full token
            outcome = "ok"
            try:
                if not auth_limiter.allowed(ip_key):
                    outcome = "rate_limited"
                    return UnauthorizedError(
                        "Too many failed authentication attempts; wait and retry."
                    ).to_dict()
                try:
                    principal = _principal(token)
                except UnauthorizedError as e:
                    auth_limiter.record_failure(ip_key)
                    outcome = e.code
                    return e.to_dict()
                auth_limiter.reset(ip_key)
                res = fn(principal)
                if isinstance(res, dict) and res.get("ok") is False:
                    outcome = res.get("error", {}).get("code", "error")
                return res
            except WikiError as e:
                outcome = e.code
                return e.to_dict()
            finally:
                dt = time.monotonic() - t0
                MCP_CALLS.labels(tool, outcome).inc()
                MCP_LATENCY.labels(tool).observe(dt)
                log.info("tool=%s actor=%s outcome=%s ms=%d",
                         tool, actor, outcome, int(dt * 1000))
        return await anyio.to_thread.run_sync(impl)

    # ---- read tools (any authenticated role) ----------------------------
    @mcp.tool(description="Hybrid search (BM25 + embedding vector, RRF-fused). 'count' is the "
                          "number of hits returned. 'truncated' true means the result set hit "
                          "the top_k cap — raise top_k to see more. Echoes 'mode' and 'top_k'.")
    async def search_documents(
        ctx: Context,
        query: str,
        mode: Annotated[Literal["hybrid", "bm25", "vector"],
                        Field(description="Ranking mode.")] = "hybrid",
        top_k: Annotated[int, Field(ge=1, le=50, description="Max hits (1..50).")] = 10,
        folder: Annotated[str | None, Field(description="Restrict to this folder subtree.")] = None,
        tags: Annotated[list[str] | None, Field(description="Require ALL of these tags.")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            results = run_search(db, embedder, query, mode=mode, top_k=top_k,
                                 folder=folder, tags=tags)
            capped = max(1, min(int(top_k), 50))
            return {"ok": True, "mode": mode, "top_k": capped, "count": len(results),
                    "truncated": len(results) >= capped,
                    "results": [r.to_dict() for r in results]}
        return await _call(ctx, fn, "search_documents")

    @mcp.tool(description="Read a document. The returned 'version' is the base_version you "
                          "echo back to update_document/patch_document. Pass 'section' to read "
                          "just one heading's subtree, or 'max_chars' to cap a long body "
                          "(sets 'truncated'). Use get_outline first to discover headings.")
    async def read_document(
        ctx: Context,
        path: str,
        section: Annotated[str | None, Field(description="Heading text to read in isolation.")] = None,
        max_chars: Annotated[int | None, Field(description="Truncate the body to this many chars.")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            d = docs.get_section(path, section) if section else docs.get(path)
            body = d.get("content")
            if max_chars and isinstance(body, str) and len(body) > max_chars:
                d = {**d, "content": body[:max_chars], "truncated": True, "full_length": len(body)}
            return {"ok": True, **d}
        return await _call(ctx, fn, "read_document")

    @mcp.tool(description="The heading outline of a document: a flat list of {level, text, line} "
                          "for navigation and for picking exact 'heading' values to pass to "
                          "read_document(section=)/replace_section/append_section.")
    async def get_outline(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.outline(path)}, "get_outline")

    @mcp.tool(description="List documents, optionally filtered by folder/tag. Returns 'count' "
                          "(this page), 'total' (all matches), 'has_more' for paging, and echoes "
                          "'sort'/'offset'.")
    async def list_documents(
        ctx: Context,
        folder: str | None = None,
        tag: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Page size (1..1000).")] = 100,
        offset: Annotated[int, Field(ge=0, description="Paging offset.")] = 0,
        sort: Annotated[Literal["updated_at", "title", "path"],
                        Field(description="Sort order.")] = "updated_at",
    ) -> dict:
        def fn(_p: Principal) -> dict:
            items = docs.list_docs(folder=folder, tag=tag, limit=limit, offset=offset, sort=sort)
            total = docs.count(folder=folder, tag=tag)
            return {"ok": True, "count": len(items), "total": total, "offset": offset,
                    "sort": sort, "has_more": offset + len(items) < total, "documents": items}
        return await _call(ctx, fn, "list_documents")

    @mcp.tool(description="Most-recently-updated documents, optionally within an ISO-8601 "
                          "updated_at window (since/until, e.g. '2026-06-01').")
    async def list_recent_changes(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=200, description="Max documents (1..200).")] = 20,
        since: Annotated[str | None, Field(description="ISO-8601 lower bound on updated_at.")] = None,
        until: Annotated[str | None, Field(description="ISO-8601 upper bound on updated_at.")] = None,
    ) -> dict:
        return await _call(
            ctx,
            lambda _p: {"ok": True, "documents": docs.recent_changes(limit=limit, since=since, until=until)},
            "list_recent_changes",
        )

    @mcp.tool(description="Vault-wide broken (unresolved) links: each wikilink/markdown link "
                          "that points at a non-existent document. Use after renames/cleanup to "
                          "find dangling references to fix.")
    async def list_broken_links(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=2000, description="Max broken links (1..2000).")] = 200,
    ) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.broken_links(limit=limit)},
                           "list_broken_links")

    @mcp.tool(description="List the tag vocabulary with usage counts (most-used first). "
                          "Use this to discover exact tag strings for the 'tag'/'tags' filters.")
    async def get_tags(ctx: Context) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, "tags": docs.tags()}, "get_tags")

    @mcp.tool(description="Outgoing links of a document (resolved + broken).")
    async def get_links(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.links(path)}, "get_links")

    @mcp.tool(description="Documents that link TO this document (backlinks).")
    async def get_backlinks(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.backlinks(path)}, "get_backlinks")

    @mcp.tool(description="Revision history (versions, authors, timestamps) for a document.")
    async def get_revisions(ctx: Context, path: str, limit: int = 100) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.revisions(path, limit=limit)},
                           "get_revisions")

    @mcp.tool(description="Fetch the full content of a specific past revision.")
    async def get_revision(ctx: Context, path: str, version: int) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.revision(path, version)},
                           "get_revision")

    @mcp.tool(description="Link graph as {nodes, edges} (Cytoscape/D3). root=None for the "
                          "whole vault; otherwise BFS to 'depth' around the root document.")
    async def get_graph(
        ctx: Context, root: str | None = None,
        depth: Annotated[int, Field(ge=1, le=3, description="BFS depth around root (1..3).")] = 1,
        limit: Annotated[int, Field(ge=1, le=2000, description="Max nodes (1..2000).")] = 500,
        include_unresolved: bool = True,
    ) -> dict:
        return await _call(ctx, lambda _p: docs.graph(
            root=root, depth=depth, limit=limit, include_unresolved=include_unresolved), "get_graph")

    # ---- write tools (editor/admin) -------------------------------------
    @mcp.tool(description="Create a new document. Fails with code 'conflict' if the path "
                          "already exists, 'forbidden' for viewer role.")
    async def create_document(
        ctx: Context, path: str, content: str,
        title: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.create(p, path, content, title, tags)},
                           "create_document")

    @mcp.tool(description="Replace a document's full body. base_version is REQUIRED; if it does "
                          "not match the current version the update is rejected with code "
                          "'conflict' + current content, so you can re-read, reapply, and retry. "
                          "For small edits prefer patch_document / replace_section (cheaper).")
    async def update_document(
        ctx: Context, path: str, base_version: int, content: str,
        title: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.update(
            p, path, base_version, content, title, tags)}, "update_document")

    @mcp.tool(description="Find-and-replace a unique substring in a document (token-cheap edit). "
                          "Fails 'not_found' if absent, 'validation' if it appears more than "
                          "'count' times. base_version optional (defaults to current); the edit "
                          "runs through the same optimistic-locking update.")
    async def patch_document(
        ctx: Context, path: str, find: str, replace: str,
        base_version: int | None = None,
        count: Annotated[int, Field(ge=1, description="Max occurrences to replace / allow.")] = 1,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.patch(
            p, path, find, replace, base_version=base_version, count=count)}, "patch_document")

    @mcp.tool(description="Replace the body under a heading (the heading line is kept). "
                          "Token-cheap; reads latest server-side. Pass base_version to reject the "
                          "edit with 'conflict' if the document changed since you read it; omit "
                          "to apply on top of the current version.")
    async def replace_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.replace_section(
            p, path, heading, text, base_version=base_version)}, "replace_section")

    @mcp.tool(description="Append text to the end of a heading's section (before the next "
                          "same/higher heading). Token-cheap. Pass base_version to reject with "
                          "'conflict' if the document changed since you read it.")
    async def append_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.append_section(
            p, path, heading, text, base_version=base_version)}, "append_section")

    @mcp.tool(description="Add and/or remove tags on a document without rewriting its body. "
                          "Adjusts the frontmatter 'tags' list; returns the document's resulting "
                          "tags. (Tags written inline as #hashtags in the body are managed by "
                          "editing the body.)")
    async def patch_tags(
        ctx: Context, path: str,
        add: Annotated[list[str] | None, Field(description="Tags to add.")] = None,
        remove: Annotated[list[str] | None, Field(description="Tags to remove.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.patch_tags(p, path, add=add, remove=remove)},
                           "patch_tags")

    @mcp.tool(description="Rename/move a document to a new path, preserving history and "
                          "re-resolving links. Fails 'conflict' if the destination exists.")
    async def move_document(ctx: Context, path: str, new_path: str) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.move(p, path, new_path)},
                           "move_document")

    @mcp.tool(description="Delete (soft) a document. Pass base_version to guard against "
                          "deleting a version you haven't seen.")
    async def delete_document(ctx: Context, path: str, base_version: int | None = None) -> dict:
        return await _call(ctx, lambda p: docs.delete(p, path, base_version), "delete_document")

    return mcp
