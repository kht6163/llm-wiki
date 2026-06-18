"""HTTP MCP server (streamable-http). LLM clients authenticate with a per-user
API key via ``Authorization: Bearer <key>``; the resolved Principal's role gates
write tools. All tool bodies run in a worker thread so the event loop isn't
blocked by SQLite / embedding work.
"""
from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from .logconf import bind_request_id, new_request_id
from .metrics import MCP_CALLS, MCP_LATENCY
from .ratelimit import RateLimiter
from .runtime import AppContext
from .services import audit
from .services.auth import Principal, principal_from_api_key
from .services.errors import (
    ConflictError,
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
    ValidationError,
    WikiError,
)
from .util import clamp_int, normalize_client_ip

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


def _request_id(ctx: Context) -> str:
    """Correlation id for this tool call: honour an inbound ``X-Request-ID`` (so a
    caller/proxy can trace end-to-end), else mint one. Bound into the logging context
    so every llm_wiki log line for the call carries it."""
    req = _request(ctx)
    if req is not None:
        hdr = req.headers.get("x-request-id")
        if hdr and hdr.strip():
            return hdr.strip()[:64]
    return new_request_id()


def _shape_write(result: dict, return_content: str) -> dict:
    """Trim a write tool's echoed body unless the caller asked to keep it. Write results
    funnel through DocumentService.get() and carry the full ``content``; an agent that
    just made the edit rarely needs it back, so the default ('metadata') drops the body
    and adds a ``chars`` count + ``content_omitted`` flag, keeping responses token-cheap.
    Pass return_content='full' to get the body verbatim (e.g. to verify the result)."""
    if return_content == "full" or not isinstance(result, dict) or "content" not in result:
        return result
    body = result.get("content") or ""
    out = {k: v for k, v in result.items() if k != "content"}
    out["chars"] = len(body)
    out["content_omitted"] = True
    return out


def _shape_conflict(err: dict, return_content: str) -> dict:
    """Trim the full ``current_content`` body from a 409 conflict envelope unless the
    caller asked to keep it. On a lost CAS race an agent usually re-reads a section or
    backs off (current_via tells it which) rather than diffing the whole note, so the
    default ('metadata') drops the body and adds ``current_chars`` + ``content_omitted``
    — token-cheap, especially across edit_documents sweeps. The decision-relevant fields
    (current_version/current_title/current_via/updated_by/updated_at) are kept. Pass
    return_content='full' to get current_content verbatim."""
    if return_content == "full" or not isinstance(err, dict):
        return err
    e = err.get("error")
    if not isinstance(e, dict) or e.get("code") != "conflict" or "current_content" not in e:
        return err
    body = e.get("current_content") or ""
    trimmed = {k: v for k, v in e.items() if k != "current_content"}
    trimmed["current_chars"] = len(body)
    trimmed["content_omitted"] = True
    return {**err, "error": trimmed}


def create_mcp_server(app: AppContext) -> FastMCP:
    db, docs = app.db, app.docs
    mcp = FastMCP(name="llm-wiki", stateless_http=True, json_response=True)
    # Throttle Bearer-auth failures per client IP so a leaked endpoint can't be
    # used to brute-force API keys (the web login is limited separately).
    auth_limiter = RateLimiter(max_attempts=10, window_s=300.0)
    # Bound per-principal embedding-bearing reads (search / assemble_context). model.encode()
    # runs under a process-wide lock in this single server process, so a runaway agent or
    # leaked key issuing distinct queries would serialize CPU and starve other reads + the
    # post-write embed worker. Generous burst, then 'rate_limited'.
    read_limiter = RateLimiter(max_attempts=60, window_s=60.0)

    def _throttle_read(principal: Principal, tool: str) -> None:
        key = f"read:{principal.user_id}"
        if not read_limiter.allowed(key):
            raise RateLimitedError(
                "Read rate limit exceeded for this principal; pause and retry shortly.")
        if read_limiter.record_failure(key):
            # The request that just saturated the window — record one audit row (subsequent
            # over-limit calls short-circuit at allowed() above) so abuse surfaces without
            # write-amplifying. Never let an audit failure mask the read.
            try:
                audit.record_tx(db, actor=principal.username, via="mcp",
                                action="read_rate_limited", outcome="blocked", detail=f"tool={tool}")
            except Exception:
                log.exception("failed to audit read rate limit")

    def _principal(token: str | None) -> Principal:
        p = principal_from_api_key(db, token)
        if not p:
            raise UnauthorizedError(
                "Missing or invalid API key. Send header 'Authorization: Bearer <api_key>'."
            )
        return p

    async def _call(ctx: Context, fn: Callable[[Principal], Any], tool: str = "?",
                    shape: Callable[[dict], dict] | None = None) -> dict:
        token = _bearer_token(ctx)
        ip_key = f"ip:{_client_ip(ctx)}"
        rid = _request_id(ctx)

        def impl() -> dict:
            # impl runs in a worker thread with a per-call copy of this context, so
            # binding the id here scopes it to this call (no cross-call leak).
            bind_request_id(rid)
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
                    just_blocked = auth_limiter.record_failure(ip_key)
                    if just_blocked:
                        # Persist exactly once per window (the threshold-crossing failure)
                        # so a Bearer brute-force surfaces in the admin audit feed without
                        # amplifying into writer-lock contention on every attempt. Never let
                        # an audit write failure mask the auth response.
                        try:
                            audit.record_tx(db, actor=actor, via="mcp", action="mcp_auth_failed",
                                            outcome="blocked", detail=f"ip={_client_ip(ctx)}")
                        except Exception:
                            log.exception("failed to audit mcp auth block")
                    outcome = e.code
                    return e.to_dict()
                auth_limiter.reset(ip_key)
                res = fn(principal)
                if isinstance(res, dict) and res.get("ok") is False:
                    outcome = res.get("error", {}).get("code", "error")
                    return shape(res) if shape else res
                return res
            except WikiError as e:
                outcome = e.code
                d = e.to_dict()
                return shape(d) if shape else d
            except Exception:
                # An unexpected error must still reach the agent as the structured
                # envelope (not a raw protocol error), and the metric must reflect it.
                # Log the traceback server-side; never leak internals to the client.
                outcome = "internal"
                log.exception("tool=%s actor=%s crashed", tool, actor)
                return {"ok": False,
                        "error": {"code": "internal", "message": "Internal server error.",
                                  "request_id": rid}}
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
                          "that section, or feed a hit's 'chunk_ordinal' to read_chunk to pull that "
                          "exact passage (plus neighbours) token-cheaply. Each hit also carries "
                          "'updated_at' and 'backlinks_count'/'outlinks_count' (how many docs link "
                          "to/from it — a popularity/connectedness signal to rank or triage by). "
                          "'since'/'until' (ISO-8601) bound hits to an updated_at window (recency "
                          "filter). Rejects an empty query with code 'validation' (so 0 results "
                          "means 'no matches', never 'bad query').")
    async def search_documents(
        ctx: Context,
        query: str,
        mode: Annotated[Literal["hybrid", "bm25", "vector"],
                        Field(description="Ranking mode.")] = "hybrid",
        top_k: Annotated[int, Field(ge=1, le=50, description="Max hits (1..50).")] = 10,
        folder: Annotated[str | None, Field(description="Restrict to this folder subtree.")] = None,
        tags: Annotated[list[str] | None, Field(description="Require ALL of these tags.")] = None,
        since: Annotated[str | None,
                         Field(description="ISO-8601 lower bound on updated_at (recency filter).")] = None,
        until: Annotated[str | None,
                         Field(description="ISO-8601 upper bound on updated_at.")] = None,
    ) -> dict:
        def fn(p: Principal) -> dict:
            if not query or not query.strip():
                raise ValidationError("query must not be empty.")
            _throttle_read(p, "search_documents")
            results, truncated = docs.search_page(query, mode=mode, top_k=top_k,
                                                  folder=folder, tags=tags, since=since, until=until)
            capped = clamp_int(top_k, 1, 50)
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
        def fn(p: Principal) -> dict:
            _throttle_read(p, "assemble_context")
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

    @mcp.tool(description="Cheap metadata-only check for a document: returns version, title, tags, "
                          "folder, updated_at, updated_by, and last_via (web=human, mcp=agent, cli) "
                          "WITHOUT the body. Poll this to see if a doc changed since the 'version' you "
                          "hold (and who/what touched it last) before re-reading the full body or "
                          "retrying an edit — far cheaper than read_document on a large note.")
    async def get_document_info(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.info(path)}, "get_document_info")

    @mcp.tool(description="The heading outline of a document: a flat list of {level, text, line} "
                          "for navigation and for picking exact 'heading' values to pass to "
                          "read_document(section=)/replace_section/append_section.")
    async def get_outline(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.outline(path)}, "get_outline")

    @mcp.tool(description="Read one indexed chunk of a document by 'ordinal' — the 'chunk_ordinal' a "
                          "search hit carries — optionally with neighbouring chunks via 'before'/"
                          "'after'. Chunks are the exact passages the hybrid retriever matches, so "
                          "this pulls just the relevant section (plus surrounding context) instead "
                          "of the whole body: the token-cheap follow-up to a search hit. Returns the "
                          "joined 'text', the per-chunk breakdown (each with heading/heading_path/"
                          "char range), 'chunk_count', and 'has_before'/'has_after' so you can page "
                          "outward. Fails 'not_found' if the ordinal is out of range or the document "
                          "has no indexed chunks yet.")
    async def read_chunk(
        ctx: Context, path: str,
        ordinal: Annotated[int, Field(ge=0,
                           description="0-based chunk index (a search hit's chunk_ordinal).")],
        before: Annotated[int, Field(ge=0, le=20,
                          description="Also include N preceding chunks for context.")] = 0,
        after: Annotated[int, Field(ge=0, le=20,
                         description="Also include N following chunks for context.")] = 0,
    ) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.read_chunk(
            path, ordinal, before=before, after=after)}, "read_chunk")

    @mcp.tool(description="List documents, optionally filtered by folder and tags. Returns 'count' "
                          "(this page), 'total' (all matches), 'has_more' for paging, and echoes "
                          "'sort'/'offset'. Each item carries its folder and tags, so a tag/folder "
                          "sweep needs no follow-up read. 'tags' requires ALL listed tags (AND).")
    async def list_documents(
        ctx: Context,
        folder: str | None = None,
        tag: Annotated[str | None, Field(description="Single-tag filter (kept for convenience).")] = None,
        tags: Annotated[list[str] | None,
                        Field(description="Require ALL of these tags (AND); combine with 'tag'.")] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Page size (1..1000).")] = 100,
        offset: Annotated[int, Field(ge=0, description="Paging offset.")] = 0,
        sort: Annotated[Literal["updated_at", "title", "path"],
                        Field(description="Sort order.")] = "updated_at",
    ) -> dict:
        def fn(_p: Principal) -> dict:
            items = docs.list_docs(folder=folder, tag=tag, tags=tags, limit=limit,
                                   offset=offset, sort=sort)
            total = docs.count(folder=folder, tag=tag, tags=tags)
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
                          "and 'action' (must be one of the document actions). Pass 'actor' (a "
                          "username) to see only — or, paired with your own username, to exclude — "
                          "one editor's changes, e.g. ask 'what did OTHER actors change since T' to "
                          "reconcile concurrent edits. Security/account events are not exposed here.")
    async def list_activity(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=500, description="Max events (1..500).")] = 100,
        since: Annotated[str | None, Field(description="ISO-8601 lower bound on the event time.")] = None,
        until: Annotated[str | None, Field(description="ISO-8601 upper bound on the event time.")] = None,
        via: Annotated[Literal["web", "mcp", "cli"] | None,
                       Field(description="Restrict to one surface.")] = None,
        action: Annotated[str | None,
                          Field(description="One document action, e.g. 'doc_update'.")] = None,
        actor: Annotated[str | None,
                         Field(description="Restrict to one actor (username).")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            if action is not None and action not in audit.DOC_ACTIONS:
                raise ValidationError(
                    f"action must be one of {audit.DOC_ACTIONS} (security events are not exposed).")
            events = audit.recent(db, limit=limit, since=since, until=until, via=via,
                                  actor=actor, action=action, actions=audit.DOC_ACTIONS)
            return {"ok": True, "count": len(events), "limit": limit, "since": since,
                    "until": until, "via": via, "actor": actor,
                    "actions": list(audit.DOC_ACTIONS), "events": events}
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

    @mcp.tool(description="Every folder path in the vault (folders that hold documents, their "
                          "ancestors, and explicitly-created empty folders), sorted. Use to learn "
                          "the layout before creating a document, passing a 'folder' filter to "
                          "search/list_documents, or calling create_folder/delete_folder.")
    async def list_folders(ctx: Context) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, "folders": docs.list_folders()},
                           "list_folders")

    @mcp.tool(description="Outgoing links of a document (resolved + broken).")
    async def get_links(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.links(path)}, "get_links")

    @mcp.tool(description="Documents that link TO this document (backlinks): each carries "
                          "src_path, src_title, alias, anchor, link_type. Pass with_context=true "
                          "to also include a 'context' snippet — the sentence around each inbound "
                          "link — so you learn WHY each document links here in ONE call instead of "
                          "N read_document round-trips (omitted for links with no recorded offset).")
    async def get_backlinks(
        ctx: Context, path: str,
        with_context: Annotated[bool, Field(description="Include a 'context' snippet (the "
                                "surrounding sentence) for each inbound link.")] = False,
    ) -> dict:
        return await _call(
            ctx, lambda _p: {"ok": True, **docs.backlinks(path, with_context=with_context)},
            "get_backlinks")

    @mcp.tool(description="Resolve wikilink/markdown targets to existing document paths BEFORE you "
                          "write them — a dry run using the same resolver as the live graph (bare "
                          "names resolve by basename, preferring the same folder as 'from_path'). "
                          "Returns 'resolved' mapping each target to a document path or null "
                          "(null = would be a broken link). Use to avoid creating dangling "
                          "references instead of cleaning them up later.")
    async def resolve_links(
        ctx: Context,
        targets: Annotated[list[str], Field(description="Link targets to resolve.")],
        from_path: Annotated[str | None,
                             Field(description="Source document (for same-folder preference).")] = None,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            resolved = {t: docs.resolve_link(t, from_path) for t in (targets or [])}
            return {"ok": True, "resolved": resolved,
                    "unresolved": [t for t, v in resolved.items() if v is None]}
        return await _call(ctx, fn, "resolve_links")

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

    @mcp.tool(description="Unified line diff between two revisions of a document. The bodies are "
                          "diffed server-side and never travel to you, so comparing large revisions "
                          "is cheap. Returns 'diff' (classified lines: hunk/add/del/ctx) and "
                          "'summary' {lines_added, lines_deleted} — use it to audit what changed "
                          "between versions instead of fetching both full bodies.")
    async def compare_revisions(ctx: Context, path: str, from_version: int, to_version: int) -> dict:
        return await _call(ctx, lambda _p: {"ok": True, **docs.compare_revisions(
            path, from_version, to_version)}, "compare_revisions")

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
                          "already exists, 'forbidden' for viewer role. By default the response "
                          "omits the document body (you already have it) — pass "
                          "return_content='full' to echo it back.")
    async def create_document(
        ctx: Context, path: str, content: str,
        title: str | None = None, tags: list[str] | None = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(
            docs.create(p, path, content, title, tags), return_content)}, "create_document",
            shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Replace a document's full body (editor/admin only; viewer gets "
                          "'forbidden'). base_version is REQUIRED; if it does not match the "
                          "current version the update is rejected with code 'conflict' + current "
                          "content, so you can re-read, reapply, and retry. For small edits prefer "
                          "patch_document / replace_section (cheaper). Response omits the body by "
                          "default; pass return_content='full' to echo it.")
    async def update_document(
        ctx: Context, path: str, base_version: int, content: str,
        title: str | None = None, tags: list[str] | None = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.update(
            p, path, base_version, content, title, tags), return_content)}, "update_document",
            shape=lambda d: _shape_conflict(d, return_content))

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
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.patch(
            p, path, find, replace, base_version=base_version, count=count,
            mode=mode, occurrence=occurrence), return_content)}, "patch_document",
            shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Append a text block to a document (editor/admin only) — the natural "
                          "journaling primitive for logs/daily notes. With 'ensure_heading' the "
                          "block goes at the end of that heading's section, creating the heading "
                          "if missing; without it, at the very end. No base_version needed: it "
                          "reads the current version server-side, so a plain append is one call "
                          "and won't spuriously conflict. Pass a fresh 'idempotency_key' to make a "
                          "retry safe — replaying a key returns the prior result instead of "
                          "appending the block twice (returns 'deduplicated': true).")
    async def append_to_document(
        ctx: Context, path: str, text: str,
        ensure_heading: Annotated[str | None,
                                  Field(description="Append under this heading (created if absent).")] = None,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        idempotency_key: Annotated[str | None,
                                   Field(description="Unique id for this append; replaying it returns "
                                         "the original result without appending again (retry-safe).")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.append_to_document(
            p, path, text, ensure_heading=ensure_heading, base_version=base_version,
            idempotency_key=idempotency_key), return_content)}, "append_to_document",
            shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Restore a past revision's content as a new edit (editor/admin only) — a "
                          "one-call server-side undo. The old body is loaded server-side (it never "
                          "travels through you), so reverting a large document is cheap. Pass "
                          "base_version to reject with 'conflict' if the document changed since you "
                          "looked; omit to revert on top of the current version.")
    async def restore_revision(
        ctx: Context, path: str, version: int,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.restore_revision(
            p, path, version, base_version=base_version), return_content)}, "restore_revision",
            shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Replace the body under a heading (the heading line is kept; "
                          "editor/admin only). Token-cheap; reads latest server-side. When several "
                          "headings share the text, set 'occurrence' (1-based; from get_outline "
                          "order) to target the Nth — out-of-range fails 'validation' rather than "
                          "editing the wrong one. Pass base_version to reject with 'conflict' if "
                          "the document changed since you read it; omit to apply on top of current.")
    async def replace_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        occurrence: Annotated[int, Field(ge=1, description="Target the Nth same-named heading.")] = 1,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.replace_section(
            p, path, heading, text, base_version=base_version, occurrence=occurrence),
            return_content)}, "replace_section",
            shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Append text to the end of a heading's section (before the next "
                          "same/higher heading; editor/admin only). Token-cheap. When several "
                          "headings share the text, set 'occurrence' (1-based) to target the Nth — "
                          "out-of-range fails 'validation'. Pass base_version to reject with "
                          "'conflict' if the document changed since you read it.")
    async def append_section(
        ctx: Context, path: str, heading: str, text: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        occurrence: Annotated[int, Field(ge=1, description="Target the Nth same-named heading.")] = 1,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.append_section(
            p, path, heading, text, base_version=base_version, occurrence=occurrence),
            return_content)}, "append_section",
            shape=lambda d: _shape_conflict(d, return_content))

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

    @mcp.tool(description="Set or replace a single frontmatter property without rewriting the body "
                          "(editor/admin only) — e.g. status, aliases, due, author. 'value' is a "
                          "string or a list of strings (a list is written as an inline YAML list); "
                          "an empty value removes the property. 'title' and 'tags' are managed "
                          "elsewhere (use update_document's title / patch_tags) and are rejected "
                          "with 'validation'. base_version optional (defaults to current); runs "
                          "through the same optimistic-locking update.")
    async def set_document_property(
        ctx: Context, path: str, key: str,
        value: Annotated[str | list[str],
                         Field(description="Scalar or list value; empty removes the key.")] = "",
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.set_property(
            p, path, key, value, base_version=base_version), return_content)},
            "set_document_property", shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Remove a single frontmatter property from a document (editor/admin only); "
                          "no-op if absent. 'title'/'tags' are managed elsewhere and rejected. "
                          "base_version optional; runs through the same optimistic-locking update.")
    async def remove_document_property(
        ctx: Context, path: str, key: str,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.remove_property(
            p, path, key, base_version=base_version), return_content)},
            "remove_document_property", shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Replace the WHOLE editable frontmatter property set in one revision "
                          "(editor/admin only) — keys you OMIT are REMOVED, keys you include are "
                          "set. This is the bulk/declarative counterpart to set_document_property "
                          "(which sets ONE key and leaves the rest): use it to reconcile a "
                          "document's properties to an exact desired state. 'properties' maps each "
                          "key to a string or list of strings (an empty value drops that key). "
                          "'title'/'tags' are managed elsewhere (update_document title / patch_tags) "
                          "and rejected with 'validation'. The body is preserved. base_version "
                          "optional (defaults to current); runs through optimistic locking.")
    async def set_document_properties(
        ctx: Context, path: str,
        properties: Annotated[dict[str, str | list[str]],
                              Field(description="key -> scalar or list value; the COMPLETE editable "
                                    "set (omitted keys are removed).")],
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        def fn(p: Principal) -> dict:
            props = [(k, v if isinstance(v, list) else [v]) for k, v in (properties or {}).items()]
            return {"ok": True, **_shape_write(
                docs.replace_properties(p, path, props, base_version=base_version), return_content)}
        return await _call(ctx, fn, "set_document_properties",
                           shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Flip one markdown task checkbox (- [ ] <-> - [x]) and save through the "
                          "optimistic-locking update (editor/admin only) — tick off daily-note / "
                          "checklist items without rewriting the body. Target the checkbox by "
                          "0-based 'index' (the Nth checkbox in document order) OR 1-based 'line'; "
                          "exactly one is required. Fails 'validation' if the target isn't a task "
                          "line or is out of range. base_version optional (defaults to current).")
    async def toggle_task(
        ctx: Context, path: str,
        index: Annotated[int | None, Field(ge=0,
                         description="0-based checkbox index in document order.")] = None,
        line: Annotated[int | None, Field(ge=1,
                        description="1-based line number of the checkbox.")] = None,
        base_version: Annotated[int | None, Field(description="Guard against concurrent edits.")] = None,
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body (and current_content on a "
                                        "conflict); 'metadata' (default) omits them, giving char counts.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(docs.toggle_task(
            p, path, line=line, index=index, base_version=base_version), return_content)},
            "toggle_task", shape=lambda d: _shape_conflict(d, return_content))

    @mcp.tool(description="Open today's (or a given date's) daily note, creating it if absent — the "
                          "journaling entry point. Reading an existing note works for any role; "
                          "CREATING one needs editor/admin. 'date' is YYYY-MM-DD (default: today, "
                          "UTC); 'folder' is where daily notes live (default 'daily', so the path is "
                          "e.g. daily/2026-06-18.md). Returns the note (path/version/content) plus "
                          "'created' (true if just made). Pair with append_to_document(path=…) to "
                          "log timestamped entries into it.")
    async def get_or_create_daily_note(
        ctx: Context,
        date: Annotated[str | None, Field(description="YYYY-MM-DD; default today (UTC).")] = None,
        folder: Annotated[str, Field(description="Folder daily notes live in.")] = "daily",
        return_content: Annotated[Literal["full", "metadata"],
                                  Field(description="'full' echoes the body; 'metadata' (default) "
                                        "omits it, giving a char count.")] = "metadata",
    ) -> dict:
        return await _call(ctx, lambda p: {"ok": True, **_shape_write(
            docs.daily_note(p, date, folder=folder), return_content)},
            "get_or_create_daily_note")

    @mcp.tool(description="Create an empty folder so it persists as an organizational unit before "
                          "it holds any documents (editor/admin only) — 'structure first, content "
                          "later'. Fails 'conflict' if a folder already exists there (including one "
                          "implied by existing documents), 'validation' on an empty path.")
    async def create_folder(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda p: docs.create_folder(p, path), "create_folder")

    @mcp.tool(description="Delete an empty folder and its empty subfolders (editor/admin only). "
                          "Refuses with 'validation' if any document still lives under it (move or "
                          "delete those first); 'not_found' if there is no such folder.")
    async def delete_folder(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda p: docs.delete_folder(p, path), "delete_folder")

    @mcp.tool(description="List the documents YOU (this API key's user) have pinned as "
                          "favourites, title-sorted. Favourites are per-user. Pair with "
                          "set_favorite to manage them.")
    async def list_favorites(ctx: Context) -> dict:
        def fn(p: Principal) -> dict:
            items = docs.list_favorites(p.user_id)
            return {"ok": True, "count": len(items), "documents": items}
        return await _call(ctx, fn, "list_favorites")

    @mcp.tool(description="Pin or unpin a document as one of YOUR favourites (per-user; any "
                          "role — favouriting is read-side, not a document edit). Idempotent: "
                          "'favorite' is the resulting state, not a flip. Fails 'not_found' if "
                          "there's no such document.")
    async def set_favorite(
        ctx: Context, path: str,
        favorite: Annotated[bool, Field(description="True to pin, False to unpin.")] = True,
    ) -> dict:
        return await _call(ctx, lambda p: docs.set_favorite(p, path, favorite), "set_favorite")

    @mcp.tool(description="Upload a binary attachment (image/PDF) and get a markdown embed "
                          "snippet back (editor/admin only). 'content_base64' is the file bytes "
                          "base64-encoded; allowed types: png/jpg/jpeg/gif/svg/webp/bmp/pdf, max "
                          "10 MiB. Content-addressed (identical bytes dedupe). Returns {path, url, "
                          "markdown} — paste 'markdown' into a document to embed it. Fails "
                          "'validation' on bad base64, an unsupported type, or an oversize file.")
    async def upload_attachment(
        ctx: Context,
        filename: Annotated[str, Field(description="Original filename (its extension picks the type).")],
        content_base64: Annotated[str, Field(description="File bytes, base64-encoded.")],
    ) -> dict:
        def fn(p: Principal) -> dict:
            try:
                data = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValidationError("content_base64 is not valid base64.") from e
            res = docs.save_attachment(p, filename, data)
            audit.record_tx(db, actor=p.username, via="mcp", action="attachment_upload",
                            target=res["path"])
            return {"ok": True, **res}
        return await _call(ctx, fn, "upload_attachment")

    @mcp.tool(description="Rename one frontmatter tag across the WHOLE vault (editor/admin only): "
                          "every document tagged 'old' is retagged 'new'. Each document is its own "
                          "CAS revision (not one transaction). NOTE: only the frontmatter 'tags' "
                          "list is rewritten — tags written inline as #hashtags in the body are "
                          "left as-is. Returns {dest, sources, docs_affected, docs_changed}.")
    async def rename_tag(ctx: Context, old: str, new: str) -> dict:
        return await _call(ctx, lambda p: docs.rename_tag(p, old, new), "rename_tag")

    @mcp.tool(description="Merge several frontmatter tags into one across the whole vault "
                          "(editor/admin only): every document tagged with any of 'sources' is "
                          "retagged 'dest'. Same per-document CAS + inline-hashtag caveat as "
                          "rename_tag. Returns {dest, sources, docs_affected, docs_changed}.")
    async def merge_tags(
        ctx: Context,
        sources: Annotated[list[str], Field(description="Tags to fold into 'dest'.")],
        dest: Annotated[str, Field(description="The surviving tag.")],
    ) -> dict:
        return await _call(ctx, lambda p: docs.merge_tags(p, sources, dest), "merge_tags")

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
        dry_run: Annotated[bool, Field(
            description="Preview only: report whether the destination is free and which "
                        "inbound links fix_references would rewrite, WITHOUT moving.")] = False,
    ) -> dict:
        if dry_run:
            return await _call(ctx, lambda _p: {"ok": True, "dry_run": True,
                               **docs.move_preview(path, new_path)}, "move_document")
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

    @mcp.tool(description="List soft-deleted documents (the trash), most-recently-deleted first. "
                          "Each carries path/title/version/folder and when/by whom it was deleted. "
                          "Feed a path to restore_document (undo) or purge_document (permanent).")
    async def list_trash(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max entries (1..1000).")] = 100,
        offset: Annotated[int, Field(ge=0, description="Paging offset.")] = 0,
    ) -> dict:
        def fn(_p: Principal) -> dict:
            items = docs.list_deleted(limit=limit, offset=offset)
            return {"ok": True, "count": len(items), "offset": offset, "documents": items}
        return await _call(ctx, fn, "list_trash")

    @mcp.tool(description="Restore a soft-deleted document from the trash (editor/admin only): "
                          "un-tombstone it and rebuild its search/graph indexes from the "
                          "pre-delete body. Fails 'validation' if it isn't deleted, 'not_found' "
                          "if there's no such path.")
    async def restore_document(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda p: docs.restore(p, path), "restore_document")

    @mcp.tool(description="Permanently delete a TRASHED document and ALL its history (admin only) — "
                          "there is no undo. Refuses a live document with 'validation' (soft-delete "
                          "it first); 'not_found' if there's no such path.")
    async def purge_document(ctx: Context, path: str) -> dict:
        return await _call(ctx, lambda p: docs.purge(p, path), "purge_document")

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
                                        base_version=raw.get("base_version"),
                                        occurrence=raw.get("occurrence", 1))
        if op == "append_section":
            return docs.append_section(principal, path, raw.get("heading", ""), raw.get("text", ""),
                                       base_version=raw.get("base_version"),
                                       occurrence=raw.get("occurrence", 1))
        if op == "append":
            return docs.append_to_document(principal, path, raw.get("text", ""),
                                           ensure_heading=raw.get("ensure_heading"),
                                           base_version=raw.get("base_version"))
        if op == "patch_tags":
            return docs.patch_tags(principal, path, add=raw.get("add"), remove=raw.get("remove"))
        if op == "toggle_task":
            return docs.toggle_task(principal, path, line=raw.get("line"),
                                    index=raw.get("index"), base_version=raw.get("base_version"))
        if op == "set_properties":
            props_in = raw.get("properties") or {}
            props = [(k, v if isinstance(v, list) else [v]) for k, v in props_in.items()]
            return docs.replace_properties(principal, path, props,
                                           base_version=raw.get("base_version"))
        if op == "create_folder":
            return docs.create_folder(principal, path)
        if op == "delete_folder":
            return docs.delete_folder(principal, path)
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

    _LIVE_DOC_OPS = {"update", "patch", "replace_section", "append_section", "append",
                     "patch_tags", "toggle_task", "set_properties", "delete", "move"}

    def _preview_op(principal: Principal, raw: dict) -> dict:
        """Read-only feasibility check for one batch op (dry run): predict whether it would
        apply or fail — forbidden/validation/conflict/not_found — WITHOUT mutating anything.
        Best-effort: a green preview doesn't guarantee success against a concurrent edit."""
        op, path = raw.get("op"), raw.get("path")
        rep = {"op": op, "path": path, "ok": True}
        try:
            if not principal.can_write:
                raise ForbiddenError(f"Role '{principal.role}' cannot modify documents.")
            if op == "rename_references":
                old, new = raw.get("old_path") or raw.get("path"), raw.get("new_path")
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ValidationError("'rename_references' requires 'old_path'/'path' and 'new_path'.")
                return {**rep, "path": old}
            if not isinstance(path, str) or not path:
                raise ValidationError("each operation needs a 'path' string.")
            if op == "create":
                if docs.exists(path):
                    raise ConflictError("A document already exists at this path.", path=path)
                return rep
            if op in ("create_folder", "delete_folder"):
                return rep  # cheap to attempt; folder conflicts surface on apply
            if op in _LIVE_DOC_OPS:
                info = docs.info(path)  # raises not_found if missing/deleted
                bv = raw.get("base_version")
                if bv is not None and int(bv) != info["version"]:
                    return {**rep, "ok": False, "error": {
                        "code": "conflict", "message": f"base_version {bv} != current {info['version']}",
                        "current_version": info["version"]}}
                if op == "move":
                    new = raw.get("new_path")
                    if not new:
                        raise ValidationError("'move' requires 'new_path'.")
                    if docs.exists(new):
                        raise ConflictError("Destination already exists.", path=new)
                return rep
            if op == "restore":
                return rep  # tombstone state isn't cheaply checkable here; verified on apply
            raise ValidationError(f"unknown op {op!r}.")
        except WikiError as e:
            return {**rep, "ok": False, "error": e.to_dict()["error"]}
        except Exception as e:  # bad op shape (unsafe path, bad field, …)
            return {**rep, "ok": False, "error": {"code": "validation", "message": str(e)[:200]}}

    @mcp.tool(description="Apply many single-document edits in ONE call (editor/admin only). "
                          "'operations' is a list of {op, path, ...args}; op is one of create, "
                          "update, patch, replace_section, append_section, append, patch_tags, "
                          "set_properties, toggle_task, create_folder, delete_folder, move, delete, "
                          "restore, rename_references — each takes the same args as its standalone "
                          "tool. Returns a per-op report [{op, path, ok, version?, "
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
        dry_run: Annotated[bool, Field(
            description="Preview only: per-op feasibility report (would-apply / would-fail "
                        "with the predicted error) WITHOUT mutating anything. Use to pre-flight "
                        "a sweep. Best-effort — a green preview can still lose a CAS race.")] = False,
        return_content: Annotated[Literal["full", "metadata"], Field(
            description="On a per-op conflict, 'metadata' (default) omits the competing "
                        "document's current_content (current_chars given) to keep sweep "
                        "responses small; 'full' includes the body for each conflict.")] = "metadata",
    ) -> dict:
        def fn(principal: Principal) -> dict:
            if not principal.can_write:
                raise ForbiddenError(f"Role '{principal.role}' cannot modify documents.")
            if not operations:
                raise ValidationError("operations must be a non-empty list.")
            if len(operations) > 100:
                raise ValidationError("too many operations (max 100 per call).")
            if dry_run:
                preview = [_preview_op(principal, raw if isinstance(raw, dict) else {})
                           for raw in operations]
                ok_n = sum(1 for r in preview if r["ok"])
                return {"ok": True, "dry_run": True, "would_apply": ok_n,
                        "would_fail": len(preview) - ok_n, "results": preview}
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
                    shaped = _shape_conflict(e.to_dict(), return_content)
                    results.append({"op": op, "path": path, "ok": False, "error": shaped["error"]})
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
