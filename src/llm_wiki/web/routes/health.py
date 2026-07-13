"""Liveness/readiness, Prometheus metrics, and agent-facing llms.txt exports."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ...metrics import INTERNAL_ERRORS, collect_index_gauges, render_latest
from ...ratelimit import RateLimiter
from ...util import clamp_int
from .deps import WebDeps


def register_health(web: FastAPI, deps: WebDeps) -> None:
    app = deps.app
    db = deps.db
    embedder = deps.embedder
    docs = deps.docs
    principal_web_or_bearer = deps.principal_web_or_bearer
    export_limiter = RateLimiter(max_attempts=6, window_s=60.0)
    log = logging.getLogger("llm_wiki.health")

    @web.get("/healthz")
    def healthz():
        # Liveness: cheap, always ok if the process is up.
        return JSONResponse({"ok": True})

    @web.get("/readyz")
    def readyz():
        # Readiness: DB reachable, the embedding model loaded, AND this process's
        # immutable embedding binding still current. Orchestrators
        # should route traffic only once this returns 200. Also surfaces index health
        # (embedding backlog / pending writes / broken links) for at-a-glance ops.
        details: dict = {}
        binding_current = False
        embedding_on = bool(getattr(app.settings, "embedding_enabled", True))
        try:
            details = collect_index_gauges(db)
            if embedding_on:
                binding_current = db.embedding_binding_is_current()
                ready = embedder.is_loaded and binding_current
            else:
                binding_current = True
                ready = True  # BM25-only mode: DB up is enough
        except Exception:
            INTERNAL_ERRORS.labels("readiness").inc()
            log.exception("readiness gauge collection failed")
            ready = False
        code = 200 if ready else 503
        body = {
            "ok": ready, "ready": ready, "model_loaded": embedder.is_loaded,
            "binding_current": binding_current,
            "embedding_enabled": embedding_on,
            "embedding_model": embedder.model_name, **details,
        }
        # Surface background-embedding-worker health (running / consecutive failures /
        # last error / backlog) so a silently stalled worker is visible at a glance.
        if app.embed_worker is not None:
            body["embed_worker"] = app.embed_worker.status()
        body["embedding_status"] = docs.embedding_status()
        return JSONResponse(body, status_code=code)

    @web.get("/metrics")
    def metrics():
        # Prometheus exposition over the shared process registry (web + MCP). Like
        # /healthz this is unauthenticated; restrict it at the network layer if the
        # port is exposed beyond the scrape target. Refresh point-in-time gauges from
        # the DB at scrape time.
        try:
            collect_index_gauges(db)
        except Exception:
            INTERNAL_ERRORS.labels("metrics").inc()
            log.exception("metrics gauge refresh failed")
        body, ctype = render_latest()
        return Response(content=body, media_type=ctype)

    # ---- llms.txt corpus export (agent-facing site map / full ingest) ---
    def _llms_unauthorized() -> PlainTextResponse:
        return PlainTextResponse(
            "Unauthorized. Log in via the web UI, or send "
            "'Authorization: Bearer <api_key>'.\n",
            status_code=401, media_type="text/plain; charset=utf-8",
            headers={"WWW-Authenticate": "Bearer"})

    @web.get("/llms.txt")
    def llms_txt(request: Request):
        # The emerging agent-facing site map (https://llmstxt.org/): an index of the
        # vault as markdown links to each doc's raw (.md), readable by ANY LLM client.
        if principal_web_or_bearer(request) is None:
            return _llms_unauthorized()
        text = docs.llms_index(site_title=app.settings.site_title,
                               base_url=str(request.base_url))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @web.get("/llms-full.txt")
    def llms_full_txt(request: Request, max_chars: int = 2_000_000):
        # The whole corpus concatenated into one markdown document, so an agent can
        # ingest the full context in a single request.
        principal = principal_web_or_bearer(request)
        if principal is None:
            return _llms_unauthorized()
        if not export_limiter.consume(f"full:{principal.user_id}"):
            return PlainTextResponse("Too many corpus exports; retry later.\n", status_code=429)
        res = docs.llms_full(site_title=app.settings.site_title,
                             max_chars=clamp_int(max_chars, 10_000, 2_000_000))
        return PlainTextResponse(res["text"], media_type="text/markdown; charset=utf-8")
