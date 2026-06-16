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
from .search import search_page as run_search_page
from .services import audit
from .services.auth import Principal, principal_from_api_key
from .services.errors import ForbiddenError, UnauthorizedError, ValidationError, WikiError
from .util import normalize_client_ip

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
    return normalize_client_ip(getattr(client, "host", None))


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
            except Exception:
                # An unexpected error must still reach the agent as the structured
                # envelope (not a raw protocol error), and the metric must reflect it.
                # Log the traceback server-side; never leak internals to the client.
                outcome = "internal"
                log.exception("tool=%s actor=%s crashed", tool, actor)
                return {"ok": False,
                        "error": {"code": "internal", "message": "Internal server error."}}
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
                          "the top_k cap — raise top_k to see more. Echoes 'mode' and 'top_k'. "
                          "Each hit may include 'heading' (the matched section) and 'heading_path' "
                          "(its breadcrumb); pass 'heading' to read_document(section=) to read just "
                          "that section. Rejects an empty query with code 'validation' (so 0 "
                          "results means 'no matches', never 'bad query').")
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
            if not query or not query.strip():
                raise ValidationError("query must not be empty.")
            results, truncated = run_search_page(db, embedder, query, mode=mode, top_k=top_k,
                                                 folder=folder, tags=tags)
            capped = max(1, min(int(top_k), 50))
            return {"ok": True, "mode": mode, "top_k": capped, "count": len(results),
                    "truncated": truncated,
                    "results": [r.to_dict() for r in results]}
        return await _call(ctx, fn, "search_documents")

    @mcp.tool(description="Retrieve and assemble citation-tagged context for a question — a "
                          "one-call RAG primitive. Ranks documents with the same hybrid "
                          "retriever as search, then concatenates each top document's most "
                          "relevant passage (in rank order) up to 'max_chars'/'max_sources'. "
                          "Returns 'context' (assembled text with [n] markers), 'sources' (what "
                          "each [n] cites: path, heading, version, score), and 'truncated' (more "
                          "relevant content existed beyond the budget). Prefer this over "
                          "search+read round-trips when you just need grounded context to answer. "
                          "Rejects an empty question with code 'validation'.")
    async def assemble_context(
        ctx: Context,
        question: str,
        max_chars: Annotated[int, Field(ge=200, le=24000,
                             description="Context character budget (200..24000).")] = 6000,
        max_sources: Annotated[int, Field(ge=1, le=20,
                              description="Max documents cited (1..20).")] = 8,
        mode: Annotated[Literal["hybrid", "bm25", "vector"],
                        Field(description="Ranking mode.")] = "hybrid",
        folder: Annotated[str | None, Field(description="Restrict to this folder subtree.")] = None,
        tags: Annotated[list[str] | None, Field(description="Require ALL of these tags.")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            return {"ok": True, **docs.assemble_context(
                question, max_chars=max_chars, max_sources=max_sources,
                mode=mode, folder=folder, tags=tags)}
        return await _call(ctx, fn, "assemble_context")

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
        def fn(_p: Principal) -> dict:
            items = docs.recent_changes(limit=limit, since=since, until=until)
            return {"ok": True, "count": len(items), "limit": limit, "since": since,
                    "until": until, "has_more": len(items) >= limit, "documents": items}
        return await _call(ctx, fn, "list_recent_changes")

    @mcp.tool(description="Recent document activity across the whole vault (newest first): who/what "
                          "created, edited, moved, deleted, reconciled, or uploaded — and over which "
                          "surface ('via' is web=human, mcp=agent, cli). Unlike list_recent_changes "
                          "(current docs by updated_at) this includes deletes/moves and the human-vs-"
                          "agent axis, so an agent can reconcile what changed since its last run. "
                          "Optional filters: 'since'/'until' (ISO-8601 on the event timestamp), 'via', "
                          "and 'action' (must be one of the document actions). Security/account events "
                          "are not exposed here.")
    async def list_activity(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=500, description="Max events (1..500).")] = 100,
        since: Annotated[str | None, Field(description="ISO-8601 lower bound on the event time.")] = None,
        until: Annotated[str | None, Field(description="ISO-8601 upper bound on the event time.")] = None,
        via: Annotated[Literal["web", "mcp", "cli"] | None,
                       Field(description="Restrict to one surface.")] = None,
        action: Annotated[str | None,
                          Field(description="One document action, e.g. 'doc_update'.")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            if action is not None and action not in audit.DOC_ACTIONS:
                raise ValidationError(
                    f"action must be one of {audit.DOC_ACTIONS} (security events are not exposed).")
            events = audit.recent(db, limit=limit, since=since, until=until, via=via,
                                  action=action, actions=audit.DOC_ACTIONS)
            return {"ok": True, "count": len(events), "limit": limit, "since": since,
                    "until": until, "via": via, "actions": list(audit.DOC_ACTIONS),
                    "events": events}
        return await _call(ctx, fn, "list_activity")

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

    @mcp.tool(description="Documents semantically similar to this one (nearest by embedding "
                          "vectors, NOT by explicit links — complements get_backlinks/get_links "
                          "for discovery). Each result has a cosine-similarity 'score' (higher = "
                          "closer). Empty if the document has not been embedded yet.")
    async def get_related_documents(
        ctx: Context, path: str,
        limit: Annotated[int, Field(ge=1, le=50, description="Max related docs (1..50).")] = 8,
    ) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.related(path, limit=limit)},
                           "get_related_documents")

    @mcp.tool(description="Revision history (versions, authors, timestamps) for a document, "
                          "newest first.")
    async def get_revisions(
        ctx: Context, path: str,
        limit: Annotated[int, Field(ge=1, le=500, description="Max revisions (1..500).")] = 100,
    ) -> dict:
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

    @mcp.tool(description="Replace a document's full body (editor/admin only; viewer gets "
                          "'forbidden'). base_version is REQUIRED; if it does not match the "
                          "current version the update is rejected with code 'conflict' + current "
                          "content, so you can re-read, reapply, and retry. For small edits prefer "
                          "patch_document / replace_section (cheaper).")
    async def update_document(
        ctx: Context, path: str, base_version: int, content: str,
        title: str | None = None, tags: list[str] | None = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.update(
            p, path, base_version, content, title, tags)}, "update_document")

    @mcp.tool(description="Find-and-replace in a document (token-cheap edit; editor/admin only). "
                          "mode='literal' (default) matches a substring; mode='regex' matches a "
                          "Python regex (re.MULTILINE; 'replace' may use \\1 backrefs). Set "
                          "'occurrence' (1-based) to target exactly the Nth match — the way out of "
                          "'appears N times' when content is repetitive; otherwise 'count' bounds "
                          "how many matches may be replaced. Fails 'not_found' if absent, "
                          "'validation' on an out-of-range occurrence / bad regex / too many "
                          "matches. base_version optional (defaults to current); runs through the "
                          "same optimistic-locking update.")
    async def patch_document(
        ctx: Context, path: str, find: str, replace: str,
        base_version: int | None = None,
        count: Annotated[int, Field(ge=1, description="Max occurrences to replace / allow.")] = 1,
        mode: Annotated[Literal["literal", "regex"],
                        Field(description="Match 'find' literally or as a regex.")] = "literal",
        occurrence: Annotated[int | None, Field(ge=1,
                              description="Replace only this 1-based match.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.patch(
            p, path, find, replace, base_version=base_version, count=count,
            mode=mode, occurrence=occurrence)}, "patch_document")

    @mcp.tool(description="Append a text block to a document (editor/admin only) — the natural "
                          "journaling primitive for logs/daily notes. With 'ensure_heading' the "
                          "block goes at the end of that heading's section, creating the heading "
                          "if missing; without it, at the very end. No base_version needed: it "
                          "reads the current version server-side, so a plain append is one call "
                          "and won't spuriously conflict.")
    async def append_to_document(
        ctx: Context, path: str, text: str,
        ensure_heading: Annotated[str | None,
                                  Field(description="Append under this heading (created if absent).")] = None,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.append_to_document(
            p, path, text, ensure_heading=ensure_heading, base_version=base_version)},
            "append_to_document")

    @mcp.tool(description="Restore a past revision's content as a new edit (editor/admin only) — a "
                          "one-call server-side undo. The old body is loaded server-side (it never "
                          "travels through you), so reverting a large document is cheap. Pass "
                          "base_version to reject with 'conflict' if the document changed since you "
                          "looked; omit to revert on top of the current version.")
    async def restore_revision(
        ctx: Context, path: str, version: int,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.restore_revision(
            p, path, version, base_version=base_version)}, "restore_revision")

    @mcp.tool(description="Replace the body under a heading (the heading line is kept; "
                          "editor/admin only). Token-cheap; reads latest server-side. Pass "
                          "base_version to reject the edit with 'conflict' if the document changed "
                          "since you read it; omit to apply on top of the current version.")
    async def replace_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.replace_section(
            p, path, heading, text, base_version=base_version)}, "replace_section")

    @mcp.tool(description="Append text to the end of a heading's section (before the next "
                          "same/higher heading; editor/admin only). Token-cheap. Pass base_version "
                          "to reject with 'conflict' if the document changed since you read it.")
    async def append_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.append_section(
            p, path, heading, text, base_version=base_version)}, "append_section")

    @mcp.tool(description="Add and/or remove tags on a document without rewriting its body "
                          "(editor/admin only). Adjusts the frontmatter 'tags' list; returns the "
                          "document's resulting tags. (Tags written inline as #hashtags in the "
                          "body are managed by editing the body.)")
    async def patch_tags(
        ctx: Context, path: str,
        add: Annotated[list[str] | None, Field(description="Tags to add.")] = None,
        remove: Annotated[list[str] | None, Field(description="Tags to remove.")] = None,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.patch_tags(p, path, add=add, remove=remove)},
                           "patch_tags")

    @mcp.tool(description="Rename/move a document to a new path, preserving history and "
                          "re-resolving links (editor/admin only). Fails 'conflict' if the "
                          "destination exists. Set fix_references=true to ALSO rewrite the link "
                          "text in other documents that pointed at the old path (otherwise those "
                          "references go broken until repaired); the result then includes a "
                          "'references' summary.")
    async def move_document(
        ctx: Context, path: str, new_path: str,
        fix_references: Annotated[bool, Field(
            description="Rewrite inbound links in other docs to the new path.")] = False,
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.move(
            p, path, new_path, fix_references=fix_references)}, "move_document")

    @mcp.tool(description="Rewrite the link TEXT in other documents that pointed at 'old_path' so "
                          "it points at 'new_path' (editor/admin only) — the cleanup a move leaves "
                          "behind. Use after move_document (or to repair links from the broken-"
                          "links list). Only currently-broken references keyed to the old "
                          "path/name are touched. Returns {from, to, docs_rewritten, "
                          "links_rewritten, skipped_conflicts} — one conflicted document is skipped, "
                          "not fatal.")
    async def rename_references(ctx: Context, old_path: str, new_path: str) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **docs.rename_references(p, old_path, new_path)},
                           "rename_references")

    @mcp.tool(description="Delete (soft) a document (editor/admin only). Pass base_version to "
                          "guard against deleting a version you haven't seen. Returns "
                          "{ok, path, deleted: true} on success.")
    async def delete_document(ctx: Context, path: str, base_version: int | None = None) -> dict:
        return await _call(ctx, lambda p: docs.delete(p, path, base_version), "delete_document")

    def _apply_op(principal: Principal, raw: dict) -> dict:
        """Dispatch one batch operation to the matching single-document service call
        (each keeps its own CAS guard, audit entry, and live-change event)."""
        op = raw.get("op")
        if op == "rename_references":
            old, new = raw.get("old_path") or raw.get("path"), raw.get("new_path")
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValidationError("'rename_references' requires 'old_path'/'path' and 'new_path'.")
            return docs.rename_references(principal, old, new)
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise ValidationError("each operation needs a 'path' string.")
        if op == "create":
            return docs.create(principal, path, raw.get("content", ""), raw.get("title"), raw.get("tags"))
        if op == "update":
            return docs.update(principal, path, raw.get("base_version"), raw.get("content", ""),
                               raw.get("title"), raw.get("tags"))
        if op == "patch":
            return docs.patch(principal, path, raw.get("find", ""), raw.get("replace", ""),
                              base_version=raw.get("base_version"), count=raw.get("count", 1),
                              mode=raw.get("mode", "literal"), occurrence=raw.get("occurrence"))
        if op == "replace_section":
            return docs.replace_section(principal, path, raw.get("heading", ""), raw.get("text", ""),
                                        base_version=raw.get("base_version"))
        if op == "append_section":
            return docs.append_section(principal, path, raw.get("heading", ""), raw.get("text", ""),
                                       base_version=raw.get("base_version"))
        if op == "append":
            return docs.append_to_document(principal, path, raw.get("text", ""),
                                           ensure_heading=raw.get("ensure_heading"),
                                           base_version=raw.get("base_version"))
        if op == "patch_tags":
            return docs.patch_tags(principal, path, add=raw.get("add"), remove=raw.get("remove"))
        if op == "move":
            if not raw.get("new_path"):
                raise ValidationError("'move' requires 'new_path'.")
            return docs.move(principal, path, raw["new_path"],
                             fix_references=bool(raw.get("fix_references", False)))
        if op == "delete":
            return docs.delete(principal, path, raw.get("base_version"))
        if op == "restore":
            if raw.get("version") is None:
                raise ValidationError("'restore' requires 'version'.")
            return docs.restore_revision(principal, path, raw["version"],
                                         base_version=raw.get("base_version"))
        raise ValidationError(f"unknown op {op!r}.")

    @mcp.tool(description="Apply many single-document edits in ONE call (editor/admin only). "
                          "'operations' is a list of {op, path, ...args}; op is one of create, "
                          "update, patch, replace_section, append_section, append, patch_tags, "
                          "move, delete, restore, rename_references — each takes the same args as "
                          "its standalone tool. Returns a per-op report [{op, path, ok, version?, "
                          "error?}] plus {applied, failed, stopped_early}. Ops are NOT one "
                          "transaction: each commits independently with its own CAS guard. With "
                          "stop_on_error=true (default) the first failure stops the rest (already-"
                          "applied ops stay); false keeps going best-effort. Use for retag/relink/"
                          "rename sweeps across many documents.")
    async def edit_documents(
        ctx: Context,
        operations: Annotated[list[dict[str, Any]],
                              Field(description="Edit operations, applied in order.")],
        stop_on_error: Annotated[bool, Field(
            description="Stop at the first failing op (already-applied ops are kept).")] = True,
    ) -> dict:
        def fn(principal: Principal) -> dict:
            if not principal.can_write:
                raise ForbiddenError(f"Role '{principal.role}' cannot modify documents.")
            if not operations:
                raise ValidationError("operations must be a non-empty list.")
            if len(operations) > 100:
                raise ValidationError("too many operations (max 100 per call).")
            results: list[dict] = []
            for raw in operations:
                if not isinstance(raw, dict):
                    results.append({"op": None, "path": None, "ok": False,
                                    "error": {"code": "validation", "message": "operation must be an object"}})
                    if stop_on_error:
                        break
                    continue
                op, path = raw.get("op"), raw.get("path")
                try:
                    res = _apply_op(principal, raw)
                    results.append({"op": op, "path": res.get("path", path), "ok": True,
                                    "version": res.get("version")})
                except WikiError as e:
                    results.append({"op": op, "path": path, "ok": False, "error": e.to_dict()["error"]})
                    if stop_on_error:
                        break
                except Exception as e:  # bad op shape (missing field, unsafe path, …)
                    log.warning("edit_documents op=%s path=%s rejected: %s", op, path, e)
                    results.append({"op": op, "path": path, "ok": False,
                                    "error": {"code": "validation", "message": str(e)[:200] or "invalid operation"}})
                    if stop_on_error:
                        break
            applied = sum(1 for r in results if r["ok"])
            return {"ok": True, "applied": applied, "failed": len(results) - applied,
                    "stopped_early": len(results) < len(operations), "results": results}
        return await _call(ctx, fn, "edit_documents")

    return mcp
