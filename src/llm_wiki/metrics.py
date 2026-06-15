"""Prometheus instrumentation. A single process-global registry is shared by the
web and MCP surfaces (they run in one process), so a ``/metrics`` endpoint on
either port exposes everything.

Cardinality is kept bounded on purpose: HTTP metrics label by the *route template*
(``/doc/{path}``), never the concrete path, so an unbounded vault can't explode the
label set.
"""
from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

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
DOC_WRITES = Counter(
    "llmwiki_doc_writes_total", "Document write operations.", ["action"],
)


def render_latest() -> tuple[bytes, str]:
    """(body, content_type) for a ``/metrics`` response over the default registry."""
    return generate_latest(), CONTENT_TYPE_LATEST


def _route_label(request) -> str:
    """The matched route's path template (low cardinality), or the raw path if the
    request didn't match a route (404)."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


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
