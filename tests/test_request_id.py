"""Request correlation id: the logging filter/contextvar, the web X-Request-ID
response header (minted + inbound-honoured), and the 500 envelope surfacing the id."""
import logging
import re
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from llm_wiki.logconf import (
    RequestIdFilter,
    bind_request_id,
    get_request_id,
    new_request_id,
    reset_request_id,
)
from llm_wiki.metrics import _route_label
from llm_wiki.web import create_web_app


# -- contextvar + logging filter -------------------------------------------
def _record() -> logging.LogRecord:
    return logging.LogRecord("llm_wiki.test", logging.INFO, __file__, 1, "msg", None, None)


def test_filter_defaults_to_dash_then_reflects_binding():
    f = RequestIdFilter()
    rec = _record()
    f.filter(rec)
    assert rec.request_id == "-"  # nothing bound -> sentinel
    token = bind_request_id("abc123def456")
    try:
        rec2 = _record()
        f.filter(rec2)
        assert rec2.request_id == "abc123def456"
        assert get_request_id() == "abc123def456"
    finally:
        reset_request_id(token)
    assert get_request_id() == "-"  # reset restores the sentinel


def test_new_request_id_is_unique_and_short():
    a, b = new_request_id(), new_request_id()
    assert a != b
    assert len(a) == 12 and a.isalnum()


def _request_for_metric(path: str, route_path: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
    }
    if route_path is not None:
        scope["route"] = SimpleNamespace(path=route_path)
    return Request(scope)


def test_route_label_bounds_unmatched_paths():
    request = _request_for_metric("/arbitrary/not-found/path")
    assert _route_label(request) == "__unmatched__"


def test_route_label_uses_matched_route_template():
    request = _request_for_metric("/doc/notes/example", "/doc/{path}")
    assert _route_label(request) == "/doc/{path}"


# -- web X-Request-ID header + 500 envelope --------------------------------
@pytest.fixture
def client(ctx, principals):
    # raise_server_exceptions=False so the 500 handler's response is returned to the
    # test (instead of re-raising the triggering exception).
    return TestClient(create_web_app(ctx), raise_server_exceptions=False)


def _csrf(client: TestClient, path: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def _login(client: TestClient, username: str = "alice", password: str = "secret12"):
    return client.post("/login", data={"username": username, "password": password,
                                        "csrf_token": _csrf(client, "/login")})


def test_response_carries_minted_request_id_header(client):
    r = client.get("/login")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) == 12  # a freshly minted id


def test_inbound_request_id_is_echoed(client):
    r = client.get("/login", headers={"X-Request-ID": "trace-xyz-123"})
    assert r.headers.get("X-Request-ID") == "trace-xyz-123"


def test_unhandled_error_returns_500_with_request_id(client, monkeypatch):
    # An unhandled error in a route surfaces as the structured 500 envelope carrying the
    # same correlation id that was echoed in the header — so a client can quote it.
    _login(client)

    def boom(*a, **k):
        raise RuntimeError("kaboom-internal")

    # /api/preview is CSRF-exempt and only needs a session; make its render raise.
    monkeypatch.setattr("llm_wiki.web.app.render_markdown", boom)
    r = client.post("/api/preview", data={"content": "# hi", "path": "p.md"})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "internal"
    rid = body["error"]["request_id"]
    assert rid and r.headers.get("X-Request-ID") == rid
    assert "kaboom" not in r.text  # internal detail never leaks to the client
