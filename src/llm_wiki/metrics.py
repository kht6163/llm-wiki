"""Prometheus instrumentation. A single process-global registry is shared by the
web and MCP surfaces (they run in one process), so a ``/metrics`` endpoint on
either port exposes everything.

Cardinality is kept bounded on purpose: HTTP metrics label by the *route template*
(``/doc/{path}``), never the concrete path, so an unbounded vault can't explode the
label set.
"""
from __future__ import annotations

import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

from .db import get_meta

HTTP_REQUESTS = Counter(
    "llmwiki_http_requests_total", "HTTP requests handled by the web UI.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "llmwiki_http_request_duration_seconds", "Web request latency in seconds.",
    ["method", "route"],
)
MCP_CALLS = Counter(
    "llmwiki_mcp_tool_calls_total", "MCP tool invocations by outcome.",
    ["tool", "outcome"],
)
MCP_LATENCY = Histogram(
    "llmwiki_mcp_tool_duration_seconds", "MCP tool execution latency in seconds.",
    ["tool"],
)
SEARCH_QUERIES = Counter(
    "llmwiki_search_queries_total", "Search queries by ranking mode.", ["mode"],
)
SEARCH_LATENCY = Histogram(
    "llmwiki_search_duration_seconds",
    "Hybrid search latency in seconds (BM25 + vector KNN + RRF) by mode.", ["mode"],
)
DOC_WRITES = Counter(
    "llmwiki_doc_writes_total", "Document write operations.", ["action"],
)
# Embedding is the slowest CPU step on the write path; these isolate its cost so a
# growing vector_dirty backlog can be attributed to throughput vs. queue depth.
EMBED_DURATION = Histogram(
    "llmwiki_embed_duration_seconds", "Embedding forward-pass latency in seconds (per batch).",
)
EMBED_CHUNKS = Counter(
    "llmwiki_embedded_chunks_total", "Chunks sent through the embedding encoder.",
)
# Background embedding worker health: with embedding off the request path, a silently
# stalled worker = silently rotting RAG. These distinguish "alive and succeeding",
# "alive and failing", and "last success was long ago" (worker dead / wedged).
EMBED_WORKER_RUNS = Counter(
    "llmwiki_embed_worker_runs_total", "Background embedding sweeps by outcome.", ["outcome"],
)
EMBED_WORKER_LAST_SUCCESS = Gauge(
    "llmwiki_embed_worker_last_success_timestamp_seconds",
    "Unix time of the last successful background embedding sweep.",
)
EMBED_WORKER_BUSY = Gauge(
    "llmwiki_embed_worker_busy", "1 while an embedding sweep is running, else 0.",
)
EMBED_WORKER_FAILURES = Gauge(
    "llmwiki_embed_worker_consecutive_failures",
    "Consecutive failed embedding sweeps (0 when healthy).",
)
# Realtime stream health: a slow/stuck WebSocket client whose queue fills has its
# events dropped (so it can't stall the loop). Silent drops mean a browser quietly
# out-of-sync with the live document; these make that visible.
WS_SUBSCRIBERS = Gauge(
    "llmwiki_ws_subscribers", "Currently-connected realtime WebSocket subscribers.",
)
WS_EVENTS_DROPPED = Counter(
    "llmwiki_ws_events_dropped_total",
    "Realtime events dropped because a subscriber's queue was full.",
)

# Index/health gauges — point-in-time state, refreshed from the DB at scrape time
# (and reused by /readyz). A growing vector_dirty backlog is a silent RAG-quality
# regression; pending_files signals an interrupted write; broken_links tracks vault
# link hygiene.
DOCUMENTS = Gauge("llmwiki_documents", "Non-deleted documents.")
VECTOR_DIRTY = Gauge("llmwiki_vector_dirty_documents", "Documents awaiting (re)embedding.")
PENDING_FILES = Gauge("llmwiki_pending_files", "Documents whose .md projection is pending.")
BROKEN_LINKS = Gauge("llmwiki_broken_links", "Unresolved (dangling) links from live documents.")
SCHEMA_VERSION = Gauge("llmwiki_schema_version", "Applied database schema version.")
BUILD_INFO = Info("llmwiki_build", "Static build/runtime info (model, dimension, version).")


def collect_index_gauges(db) -> dict:
    """Refresh the index/health gauges from the DB and return the same counts as a
    dict, so /metrics (scrape) and /readyz (status JSON) share one source of truth."""
    with db.reader() as conn:
        documents = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0]
        dirty = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE vector_dirty=1 AND is_deleted=0").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE file_state='pending' AND is_deleted=0").fetchone()[0]
        broken = conn.execute(
            "SELECT COUNT(*) FROM links l JOIN documents d ON d.id=l.src_doc_id "
            "WHERE l.is_resolved=0 AND d.is_deleted=0").fetchone()[0]
        sv = get_meta(conn, "schema_version")
    DOCUMENTS.set(documents)
    VECTOR_DIRTY.set(dirty)
    PENDING_FILES.set(pending)
    BROKEN_LINKS.set(broken)
    if sv:
        SCHEMA_VERSION.set(int(sv))
    return {"documents": documents, "vector_dirty": dirty, "pending_files": pending,
            "broken_links": broken, "schema_version": int(sv) if sv else None}


def render_latest() -> tuple[bytes, str]:
    """(body, content_type) for a ``/metrics`` response over the default registry."""
    return generate_latest(), CONTENT_TYPE_LATEST


def _route_label(request) -> str:
    """The matched route's path template, or a bounded unmatched sentinel."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "__unmatched__"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Time every web request and count it by method/route-template/status. ``route``
    is only present in scope after routing, so it is read after ``call_next``."""

    async def dispatch(self, request, call_next):
        t0 = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = _route_label(request)
            HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - t0)
            HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
