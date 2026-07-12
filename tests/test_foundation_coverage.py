"""Focused behavioral coverage for small shared foundation modules."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from llm_wiki import events, metrics, ratelimit, runtime
from llm_wiki.config import ConfigError, Settings
from llm_wiki.events import EventHub
from llm_wiki.logconf import configure_logging, get_logger, get_request_id
from llm_wiki.ratelimit import RateLimiter
from llm_wiki.services.errors import NotFoundError, WikiError
from llm_wiki.util import (
    PathError,
    basename_stem,
    clamp_int,
    content_disposition_attachment,
    folder_of,
    normalize_client_ip,
    normalize_folder_path,
    normalize_rel_path,
    now_iso,
    path_norm,
    safe_join,
    sha256_hex,
    word_count,
)
from llm_wiki.web.security import (
    RequestBodyLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    _same_origin,
    build_csp,
    enforce_csrf,
    get_csrf_token,
)


def _request(
    method: str = "GET",
    path: str = "/",
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    session: dict | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or [],
            "http_version": "1.1",
            "scheme": "http",
            "server": ("example.test", 80),
            "client": ("127.0.0.1", 1234),
            "session": session if session is not None else {},
        }
    )


def test_config_search_bounds_and_directory_creation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="rrf_k must be between"):
        Settings(rrf_k=0)
    with pytest.raises(ValueError, match="search_proximity_weight must be between"):
        Settings(search_proximity_weight=10.1)

    settings = Settings(vault_path=tmp_path / "vault", db_path=tmp_path / "db" / "wiki.db")
    settings.ensure_dirs()
    assert settings.vault_path.is_dir()
    assert settings.db_path.parent.is_dir()

    def deny_mkdir(_self, *args, **kwargs):
        raise PermissionError("read-only storage")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    with pytest.raises(ConfigError, match="Cannot create data directories: read-only storage"):
        settings.ensure_dirs()


def test_build_context_closes_database_when_schema_setup_fails(tmp_path, monkeypatch):
    closed = []

    class BrokenDatabase:
        def __init__(self, path):
            pass

        def ensure_schema(self):
            raise RuntimeError("schema setup failed")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(runtime, "Database", BrokenDatabase)
    monkeypatch.setattr(runtime, "get_embedder", lambda model: SimpleNamespace())
    settings = Settings(
        db_path=tmp_path / "data" / "wiki.db", vault_path=tmp_path / "vault"
    )

    with pytest.raises(RuntimeError, match="schema setup failed"):
        runtime.build_context(settings, full=False)

    assert closed == [True]


def test_time_hash_count_and_clamping_helpers():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", now_iso())
    assert sha256_hex("wiki") == hashlib.sha256(b"wiki").hexdigest()
    assert (clamp_int("-3", 0, 10), clamp_int(12, 0, 10), clamp_int(4, 0, 10)) == (
        0,
        10,
        4,
    )
    assert word_count(None) == {"words": 0, "chars": 0}
    assert word_count("hello 세계!") == {"words": 4, "chars": 9}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "?"),
        ("proxy.local", "proxy.local"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("2001:0db8::1", "2001:db8::1"),
    ],
)
def test_normalize_client_ip(raw, expected):
    assert normalize_client_ip(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (None, "path is required"),
        ("  ", "path is empty"),
        ("/./", "path is empty"),
        ("notes/../secret", "may not contain '..'"),
        ("notes/bad\nname", "control characters"),
    ],
)
def test_normalize_rel_path_rejects_malformed_paths(raw, message):
    with pytest.raises(PathError, match=re.escape(message)):
        normalize_rel_path(raw)


def test_path_and_filename_helpers(tmp_path):
    assert normalize_rel_path(r" ./notes\guide ") == "notes/guide.md"
    assert normalize_rel_path("/Already.MD") == "Already.MD"
    assert normalize_folder_path(None) == ""
    assert normalize_folder_path(r" / docs / ./ api\ ") == "docs/api"
    with pytest.raises(PathError, match="may not contain"):
        normalize_folder_path("docs/../private")
    assert path_norm("Docs/Guide.MD") == "docs/guide.md"
    assert folder_of("docs/guide.md") == "docs"
    assert folder_of("guide.md") == ""
    assert basename_stem("docs/Guide.MD") == "Guide"
    assert basename_stem("asset.png") == "asset.png"

    disposition = content_disposition_attachment('한글"\\\n.md')
    assert 'filename=".md"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert "%ED%95%9C%EA%B8%80" in disposition
    assert 'filename="document.md"' in content_disposition_attachment("한글")

    vault = tmp_path / "vault"
    vault.mkdir()
    assert safe_join(vault, "") == vault.resolve()
    assert safe_join(vault, "docs/guide.md") == (vault / "docs/guide.md").resolve()
    with pytest.raises(PathError, match="escapes the vault"):
        safe_join(vault, "../outside.md")


class _Loop:
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def call_soon_threadsafe(self, callback, *args):
        if self.fail:
            raise RuntimeError("closed")
        self.calls.append((callback, args))
        callback(*args)


def test_event_hub_publish_paths_and_subscriber_count():
    hub = EventHub(max_queue=2)
    assert hub.subscriber_count() == 0
    hub.publish({"ignored": "no loop or subscribers"})

    q = hub.subscribe()
    hub.publish({"ignored": "no loop"})
    loop = _Loop()
    hub.bind_loop(loop)
    hub.publish({"seq": 1})
    assert hub.subscriber_count() == 1
    assert q.get_nowait() == {"seq": 1}
    assert len(loop.calls) == 1

    hub.bind_loop(_Loop(fail=True))
    hub.publish({"ignored": "closed loop"})
    assert q.empty()
    hub.unsubscribe(q)
    hub.publish({"ignored": "no subscribers"})


def test_event_drop_warning_is_throttled(monkeypatch, caplog):
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait("existing")
    monkeypatch.setattr(events, "_last_drop_log", 0.0)
    times = iter([10.0, 12.0])
    monkeypatch.setattr(events.time, "monotonic", lambda: next(times))

    with caplog.at_level(logging.WARNING, logger="llm_wiki.events"):
        EventHub._offer(q, "drop-one")
        EventHub._offer(q, "drop-two")

    assert [record.message for record in caplog.records] == [
        "realtime event dropped: a subscriber queue is full (slow client)"
    ]
    assert q.get_nowait() == "existing"


def test_logging_defaults_and_named_logger(monkeypatch):
    configured = []
    monkeypatch.setattr("llm_wiki.logconf.dictConfig", configured.append)

    configure_logging("", "")

    assert configured[0]["handlers"] == {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "std",
            "filters": ["request_id"],
        }
    }
    assert configured[0]["loggers"]["llm_wiki"] == {
        "handlers": ["console"],
        "level": "INFO",
        "propagate": False,
    }
    logger = get_logger("foundation")
    assert logger.name == "llm_wiki.foundation"


def test_rate_limiter_threshold_expiry_and_reset(monkeypatch):
    limiter = RateLimiter(max_attempts=2, window_s=5.0)
    times = iter([0.0, 1.0, 2.0, 3.0, 10.0, 11.0])
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: next(times))

    assert limiter.allowed("client") is True
    assert limiter.record_failure("client") is False
    assert limiter.record_failure("client") is True
    assert limiter.allowed("client") is False
    assert limiter.allowed("client") is True
    assert list(limiter._hits["client"]) == []
    limiter.record_failure("client")
    limiter.reset("client")
    assert "client" not in limiter._hits


_GAUGE_QUERIES = [
    "SELECT COUNT(*) FROM documents WHERE is_deleted=0",
    "SELECT COUNT(*) FROM documents WHERE vector_dirty=1 AND is_deleted=0",
    "SELECT COUNT(*) FROM documents WHERE file_state='pending' AND is_deleted=0",
    (
        "SELECT COUNT(*) FROM links l JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.is_resolved=0 AND d.is_deleted=0"
    ),
]


class _Rows:
    def __init__(self, results):
        self.results = dict(zip(_GAUGE_QUERIES, results, strict=True))
        self.queries = []
        self.current = None

    def execute(self, query):
        normalized = " ".join(query.split())
        assert normalized in self.results, f"unexpected gauge SQL: {normalized}"
        self.queries.append(normalized)
        self.current = self.results[normalized]
        return self

    def fetchone(self):
        assert self.current is not None
        return [self.current]


class _DB:
    def __init__(self, values):
        self.conn = _Rows(values)

    @contextmanager
    def reader(self):
        yield self.conn


@pytest.mark.parametrize(("schema_version", "expected"), [("7", 7), (None, None)])
def test_collect_index_gauges_updates_returned_state_and_metrics(
    schema_version, expected, monkeypatch
):
    monkeypatch.setattr(metrics, "get_meta", lambda _conn, _key: schema_version)
    db = _DB([4, 3, 2, 1])
    stats = metrics.collect_index_gauges(db)
    assert stats == {
        "documents": 4,
        "vector_dirty": 3,
        "pending_files": 2,
        "broken_links": 1,
        "schema_version": expected,
    }
    assert metrics.DOCUMENTS._value.get() == 4
    assert metrics.VECTOR_DIRTY._value.get() == 3
    assert metrics.PENDING_FILES._value.get() == 2
    assert metrics.BROKEN_LINKS._value.get() == 1
    if expected is not None:
        assert metrics.SCHEMA_VERSION._value.get() == expected
    assert db.conn.queries == _GAUGE_QUERIES


def test_render_latest_returns_prometheus_payload():
    body, content_type = metrics.render_latest()
    assert b"llmwiki_documents" in body
    assert content_type.startswith("text/plain")


@pytest.mark.asyncio
async def test_prometheus_middleware_records_success_and_failure():
    middleware = metrics.PrometheusMiddleware(None)
    request = _request()
    request.scope["route"] = SimpleNamespace(path="/foundation")
    success_before = metrics.HTTP_REQUESTS.labels("GET", "/foundation", "204")._value.get()
    latency = metrics.HTTP_LATENCY.labels("GET", "/foundation")
    latency_before = next(
        sample.value
        for family in latency.collect()
        for sample in family.samples
        if sample.name == "llmwiki_http_request_duration_seconds_count"
    )

    async def succeed(_request):
        return PlainTextResponse("", 204)

    response = await middleware.dispatch(request, succeed)
    assert response.status_code == 204
    assert (
        metrics.HTTP_REQUESTS.labels("GET", "/foundation", "204")._value.get()
        == success_before + 1
    )
    latency_after_success = next(
        sample.value
        for family in latency.collect()
        for sample in family.samples
        if sample.name == "llmwiki_http_request_duration_seconds_count"
    )
    assert latency_after_success == latency_before + 1

    failure_before = metrics.HTTP_REQUESTS.labels("GET", "/foundation", "500")._value.get()

    async def fail(_request):
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError, match="handler failed"):
        await middleware.dispatch(request, fail)
    assert (
        metrics.HTTP_REQUESTS.labels("GET", "/foundation", "500")._value.get()
        == failure_before + 1
    )
    latency_after_failure = next(
        sample.value
        for family in latency.collect()
        for sample in family.samples
        if sample.name == "llmwiki_http_request_duration_seconds_count"
    )
    assert latency_after_failure == latency_after_success + 1


def test_wiki_error_serialization_and_recovery_hint_override():
    base = WikiError("broken", context="base")
    assert str(base) == "broken"
    assert base.to_dict() == {
        "ok": False,
        "error": {"code": "error", "message": "broken", "context": "base"},
    }

    missing = NotFoundError("gone", path="missing.md", suggested_action="search")
    assert missing.http_status == 404
    assert missing.to_dict()["error"] == {
        "code": "not_found",
        "message": "gone",
        "suggested_action": "search",
        "path": "missing.md",
    }


def test_csp_csrf_token_and_same_origin_helpers(monkeypatch):
    strict = build_csp()
    nonce = build_csp("abc123")
    assert "nonce-" not in strict
    assert "script-src 'self' 'nonce-abc123'" in nonce

    session = {}
    request = _request(session=session)
    monkeypatch.setattr("llm_wiki.web.security.secrets.token_urlsafe", lambda _n: "token")
    assert get_csrf_token(request) == "token"
    assert session == {"_csrf": "token"}
    assert get_csrf_token(request) == "token"
    assert _same_origin(request) is True
    assert _same_origin(_request(headers=[(b"origin", b"http://example.test")])) is True
    assert _same_origin(_request(headers=[(b"referer", b"http://evil.test/form")])) is False


@pytest.mark.asyncio
async def test_enforce_csrf_accepts_safe_exempt_header_and_form_tokens():
    await enforce_csrf(_request("GET"))
    await enforce_csrf(_request("POST", "/api/preview"))
    await enforce_csrf(
        _request("POST", headers=[(b"x-csrf-token", b"token")], session={"_csrf": "token"})
    )
    form_request = _request("POST", session={"_csrf": "form-token"})
    form_request._form = FormData({"csrf_token": "form-token"})
    await enforce_csrf(form_request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "headers", "form", "detail"),
    [
        ({"_csrf": "token"}, [(b"origin", b"http://evil.test")], None, "Cross-origin"),
        ({}, [], FormData({"csrf_token": "token"}), "Missing or invalid"),
        ({"_csrf": "token"}, [], FormData({}), "Missing or invalid"),
        ({"_csrf": "token"}, [], FormData({"csrf_token": 1}), "Missing or invalid"),
        ({"_csrf": "token"}, [(b"x-csrf-token", b"wrong")], None, "Missing or invalid"),
    ],
)
async def test_enforce_csrf_rejects_invalid_requests(session, headers, form, detail):
    request = _request("POST", headers=headers, session=session)
    if form is not None:
        request._form = form
    with pytest.raises(HTTPException, match=detail) as exc_info:
        await enforce_csrf(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_body_limit_invalid_length_non_request_and_exception_paths():
    sent = []

    async def send(message):
        sent.append(message)

    async def receive_non_request():
        return {"type": "http.disconnect"}

    async def pass_non_request(_scope, receive, send):
        assert await receive() == {"type": "http.disconnect"}
        await send({"type": "http.response.start", "status": 204, "headers": []})

    scope = {"type": "http", "headers": [(b"content-length", b"invalid")]}
    await RequestBodyLimitMiddleware(pass_non_request, 1)(scope, receive_non_request, send)
    assert sent[0]["status"] == 204

    async def fail(_scope, _receive, _send):
        raise RuntimeError("app failed")

    with pytest.raises(RuntimeError, match="app failed"):
        await RequestBodyLimitMiddleware(fail, 1)({"type": "http", "headers": []}, receive_non_request, send)

    messages = [{"type": "http.request", "body": b"too large"}]

    async def overflow_receive():
        return messages.pop(0)

    async def overflow_then_fail(_scope, receive, _send):
        assert (await receive())["type"] == "http.disconnect"
        assert (await receive())["type"] == "http.disconnect"
        raise RuntimeError("ignored after rejection")

    await RequestBodyLimitMiddleware(overflow_then_fail, 1)(
        {"type": "http", "headers": []}, overflow_receive, send
    )
    assert any(message.get("status") == 413 for message in sent)


@pytest.mark.asyncio
async def test_request_id_middleware_passes_non_http_and_restores_context(caplog):
    seen = []

    async def send(message):
        seen.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    async def echo(scope, _receive, send):
        assert get_request_id() == scope["state"]["request_id"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"other", b"x"), (b"x-request-id", b" trace-id " )],
    }
    await RequestIdMiddleware(echo)(scope, receive, send)
    assert dict(seen[0]["headers"])[b"x-request-id"] == b"trace-id"
    assert scope["state"]["request_id"] == "trace-id"
    assert get_request_id() == "-"

    websocket_seen = []

    async def websocket_app(inner_scope, _receive, _send):
        websocket_seen.append(inner_scope["type"])

    await RequestIdMiddleware(websocket_app)({"type": "websocket"}, receive, send)
    assert websocket_seen == ["websocket"]

    async def fail(_scope, _receive, _send):
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="llm_wiki.web"):
        with pytest.raises(RuntimeError, match="boom"):
            await RequestIdMiddleware(fail)(
                {"type": "http", "method": "POST", "path": "/fail", "headers": []},
                receive,
                send,
            )
    assert "unhandled error: method=POST path=/fail" in caplog.text
    assert get_request_id() == "-"


@pytest.mark.asyncio
async def test_request_id_blank_header_mints_bounded_id(monkeypatch):
    monkeypatch.setattr("llm_wiki.web.security.new_request_id", lambda: "minted")

    async def app(scope, _receive, send):
        assert scope["state"]["request_id"] == "minted"
        await send({"type": "http.response.start", "status": 200, "headers": []})

    sent = []

    async def send(message):
        sent.append(message)

    await RequestIdMiddleware(app)(
        {"type": "http", "headers": [(b"x-request-id", b"   ")]},
        lambda: None,
        send,
    )
    assert dict(sent[0]["headers"])[b"x-request-id"] == b"minted"


@pytest.mark.asyncio
async def test_security_headers_preserve_existing_values_and_optionally_add_hsts():
    request = _request()

    async def existing(_request):
        return PlainTextResponse(
            "ok",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "custom",
                "Strict-Transport-Security": "custom-hsts",
            },
        )

    response = await SecurityHeadersMiddleware(None, hsts=True).dispatch(request, existing)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["content-security-policy"] == "custom"
    assert response.headers["strict-transport-security"] == "custom-hsts"

    async def plain(_request):
        return PlainTextResponse("ok")

    response = await SecurityHeadersMiddleware(None, hsts=False).dispatch(request, plain)
    assert "strict-transport-security" not in response.headers
    assert response.headers["content-security-policy"] == build_csp()
