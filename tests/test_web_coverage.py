"""Coverage of web boundary failures and observable response/state envelopes."""

import asyncio
import importlib.metadata
import re
import subprocess
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from markupsafe import Markup
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import llm_wiki.web.app as web_mod
from llm_wiki import file_projection as fp
from llm_wiki.services import users as users_svc
from llm_wiki.services.auth import create_api_key
from llm_wiki.services.documents import ProjectionPendingError
from llm_wiki.services.errors import ConflictError, NotFoundError, ValidationError
from llm_wiki.util import PathError
from llm_wiki.web import create_web_app


def _token(client: TestClient, path: str = "/login") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def _login(client: TestClient, username: str = "alice") -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": "secret12", "csrf_token": _token(client)},
    )
    assert response.status_code == 200


def _logout(client: TestClient) -> None:
    response = client.post("/logout", data={"csrf_token": _token(client, "/")})
    assert response.status_code == 200


def _client(ctx, principals, *, raise_server_exceptions: bool = True) -> TestClient:
    assert principals  # ensure the named users exist
    return TestClient(create_web_app(ctx), raise_server_exceptions=raise_server_exceptions)


def test_web_package_lazily_exports_app_without_loading_numpy():
    script = """
import sys
import llm_wiki.web
assert 'llm_wiki.web.app' not in sys.modules
assert not any(name == 'numpy' or name.startswith('numpy.') for name in sys.modules)
from llm_wiki.web import create_web_app
assert callable(create_web_app)
assert 'llm_wiki.web.app' in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_web_package_rejects_unknown_public_attributes():
    web_package = sys.modules["llm_wiki.web"]
    missing_name = "missing_export"

    with pytest.raises(AttributeError) as exc_info:
        getattr(web_package, missing_name)

    assert exc_info.value.args == (
        f"module 'llm_wiki.web' has no attribute {missing_name!r}",
    )


@pytest.mark.asyncio
async def test_format_helpers_cover_upload_diff_dates_and_windows():
    small = UploadFile(BytesIO(b"abc"), filename="small.txt")
    large = UploadFile(BytesIO(b"abcdef"), filename="large.txt")
    assert await web_mod._read_capped(small, 3) == b"abc"
    assert await web_mod._read_capped(large, 3) is None

    diff = web_mod._diff_lines("same\nold\ntail", "same\nnew\ntail")
    assert {line["cls"] for line in diff} == {"hunk", "ctx", "del", "add"}
    assert web_mod._human_dt(None) is None
    assert web_mod._human_dt("not-a-date") == "not-a-date"
    rendered = web_mod._human_dt("2026-01-02T03:04:05Z")
    assert isinstance(rendered, Markup) and "2026-01-02 03:04:05" in rendered
    assert web_mod._window_since("all") is None
    for window in ("today", "24h", "7d", "30d", "invalid"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", web_mod._window_since(window))


def test_unexpected_errors_split_json_and_html(ctx, principals, monkeypatch):
    client = _client(ctx, principals, raise_server_exceptions=False)
    _login(client)
    original_nav_tree = ctx.docs.nav_tree
    monkeypatch.setattr(ctx.docs, "nav_tree", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    api = client.get("/api/tree", headers={"X-Request-ID": "coverage-api"})
    assert api.status_code == 500
    assert api.json() == {
        "ok": False,
        "error": {"code": "internal", "message": "Internal server error.", "request_id": "coverage-api"},
    }
    assert api.headers["x-request-id"] == "coverage-api"

    monkeypatch.setattr(ctx.docs, "nav_tree", original_nav_tree)
    monkeypatch.setattr(ctx.docs, "list_docs", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    page = client.get("/", headers={"X-Request-ID": "coverage-page"})
    assert page.status_code == 500
    assert "coverage-page" in page.text
    assert page.headers["x-request-id"] == "coverage-page"


def test_ready_and_metrics_keep_stable_envelopes_when_gauges_fail(
    ctx, principals, monkeypatch
):
    client = _client(ctx, principals)
    import llm_wiki.web.routes.health as health_mod

    monkeypatch.setattr(
        health_mod, "collect_index_gauges", lambda _db: (_ for _ in ()).throw(RuntimeError("db"))
    )

    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert ready.json()["binding_current"] is False
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "llmwiki_build_info" in metrics.text


def test_llms_exports_require_auth_and_clamp_full_limit(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    denied = client.get("/llms.txt")
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"

    _login(client)
    monkeypatch.setattr(ctx.docs, "llms_index", lambda **kw: f"index:{kw['site_title']}")
    seen = {}

    def full(**kw):
        seen.update(kw)
        return {"text": "full", "truncated": False}

    monkeypatch.setattr(ctx.docs, "llms_full", full)
    assert client.get("/llms.txt").text.startswith("index:")
    response = client.get("/llms-full.txt?max_chars=1")
    assert response.status_code == 200 and response.text == "full"
    assert seen["max_chars"] == 10_000


def test_page_permission_and_service_failures_are_visible(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client, "bob")
    assert client.get("/trash").status_code == 403
    assert client.get("/activity").status_code == 403
    daily = client.post(
        "/daily", data={"csrf_token": _token(client, "/")}, follow_redirects=False
    )
    assert daily.status_code == 303 and daily.headers["location"] == "/"

    _logout(client)
    _login(client)
    monkeypatch.setattr(ctx.docs, "broken_links", lambda **kw: {"count": 0, "links": []})
    assert client.get("/graph?root=x.md").status_code == 200
    assert client.get("/broken-links?limit=99999").status_code == 200


def test_api_write_routes_return_success_and_path_error_envelopes(ctx, principals):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")

    created_folder = client.post("/api/folders", data={"path": "notes", "csrf_token": csrf})
    assert created_folder.status_code == 200 and created_folder.json()["ok"]
    created = client.post(
        "/new", data={"path": "notes/a.md", "content": "- [ ] task\n", "csrf_token": csrf}
    )
    assert created.status_code == 200
    assert client.get("/api/graph").json()["ok"]
    assert client.get("/api/tree").json()["ok"]

    moved = client.post(
        "/api/doc/notes/a.md/move",
        data={"new_path": "notes/b.md", "csrf_token": csrf},
    )
    assert moved.status_code == 200 and moved.json()["path"] == "notes/b.md"
    toggled = client.post(
        "/api/doc/notes/b.md/toggle-task",
        data={"index": 0, "base_version": 2, "csrf_token": csrf},
    )
    assert toggled.status_code == 200 and toggled.json()["version"] == 3
    props = client.post(
        "/api/doc/notes/b.md/properties",
        json={"base_version": 3, "properties": [
            {"key": "owners", "values": "a, b"},
            {"key": "flags", "values": [1, 2]},
            {"key": "empty", "values": None},
        ]},
        headers={"X-CSRF-Token": csrf},
    )
    assert props.status_code == 200 and props.json()["version"] == 4

    bad = client.post("/api/folders", data={"path": "../bad", "csrf_token": csrf})
    assert bad.status_code == 400
    assert bad.json()["error"] == "bad_path"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT action, outcome, detail FROM audit_log WHERE actor='alice' "
            "AND action='write_rejected' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert dict(row) == {"action": "write_rejected", "outcome": "error", "detail": "code=bad_path"}


def test_route_specific_failures_preserve_redirect_flash_and_audit(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")
    client.post("/new", data={"path": "x.md", "content": "v1", "csrf_token": csrf})

    monkeypatch.setattr(ctx.docs, "delete", lambda *a, **k: (_ for _ in ()).throw(ConflictError("busy")))
    deleted = client.post("/doc/x.md/delete", data={"base_version": 1, "csrf_token": csrf}, follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/doc/x.md"

    monkeypatch.setattr(ctx.docs, "restore", lambda *a, **k: (_ for _ in ()).throw(NotFoundError("gone")))
    restored = client.post("/trash/x.md/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert restored.status_code == 303 and restored.headers["location"] == "/trash"
    monkeypatch.setattr(ctx.docs, "purge", lambda *a, **k: (_ for _ in ()).throw(ValidationError("live")))
    purged = client.post("/trash/x.md/purge", data={"csrf_token": csrf}, follow_redirects=False)
    assert purged.status_code == 303

    monkeypatch.setattr(ctx.docs, "toggle_favorite", lambda *a, **k: (_ for _ in ()).throw(NotFoundError("missing")))
    favorite = client.post("/doc/x.md/favorite", data={"csrf_token": csrf}, follow_redirects=False)
    assert favorite.status_code == 303
    assert "missing" in client.get("/doc/x.md").text

    with ctx.db.reader() as conn:
        actions = [row["action"] for row in conn.execute(
            "SELECT action FROM audit_log WHERE actor='alice' AND outcome != 'ok'"
        )]
    assert {"doc_delete", "doc_restore", "doc_purge"} <= set(actions)


def test_committed_projection_delays_are_successful_web_writes_without_rejection_audit(
    ctx, principals, monkeypatch
):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/")

    def pending(path: str) -> ProjectionPendingError:
        return ProjectionPendingError(
            fp.ProjectionResult(7, path, False, False, "target_changed", attempts=3)
        )

    monkeypatch.setattr(
        ctx.docs,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("queued.md")),
    )
    created = client.post(
        "/broken-links/create",
        data={"target": "queued", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/doc/queued.md/edit"
    assert "문서는 저장됐지만 파일 반영이 지연" in client.get("/").text

    created_from_form = client.post(
        "/new",
        data={"path": "queued.md", "content": "body", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert created_from_form.status_code == 303
    assert created_from_form.headers["location"] == "/doc/queued.md"

    monkeypatch.setattr(
        ctx.docs,
        "update",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("edited.md")),
    )
    edited = client.post(
        "/doc/edited.md/edit",
        data={"content": "new", "base_version": 1, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert edited.status_code == 303 and edited.headers["location"] == "/doc/edited.md"

    monkeypatch.setattr(
        ctx.docs,
        "delete",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("deleted.md")),
    )
    deleted = client.post(
        "/doc/deleted.md/delete",
        data={"base_version": 1, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert deleted.status_code == 303 and deleted.headers["location"] == "/"

    monkeypatch.setattr(
        ctx.docs,
        "restore_revision",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("revision.md")),
    )
    revision = client.post(
        "/doc/revision.md/rev/1/restore",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert revision.status_code == 303
    assert revision.headers["location"] == "/doc/revision.md"

    monkeypatch.setattr(
        ctx.docs,
        "restore",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("restored.md")),
    )
    restored = client.post(
        "/trash/restored.md/restore",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert restored.status_code == 303 and restored.headers["location"] == "/trash"
    assert "복원은 저장됐지만 파일 반영이 지연" in client.get("/").text

    monkeypatch.setattr(
        ctx.docs,
        "daily_note",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending("daily/2026-07-13.md")),
    )
    daily = client.post(
        "/daily", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert daily.status_code == 303
    assert daily.headers["location"] == "/doc/daily/2026-07-13.md"
    assert "오늘 노트는 저장됐지만 파일 반영이 지연" in client.get("/").text

    with ctx.db.reader() as conn:
        rejected = conn.execute(
            "SELECT action,target FROM audit_log WHERE actor='alice' AND outcome != 'ok' "
            "AND target IN ('queued.md','restored.md','daily/2026-07-13.md')"
        ).fetchall()
    assert rejected == []


def test_uncommitted_projection_precondition_is_audited_as_rejected_write(
    ctx, principals, monkeypatch
):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/")
    pending = ProjectionPendingError(
        fp.ProjectionResult(7, "blocked.md", False, False, "io_error", attempts=1),
        committed=False,
    )
    monkeypatch.setattr(
        ctx.docs,
        "create_folder",
        lambda *args, **kwargs: (_ for _ in ()).throw(pending),
    )

    response = client.post(
        "/api/folders", data={"path": "blocked", "csrf_token": csrf}
    )

    assert response.status_code == 409
    assert response.json()["error"]["committed"] is False
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT action,outcome,detail FROM audit_log WHERE actor='alice' "
            "AND target='/api/folders' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert dict(row) == {
        "action": "write_rejected",
        "outcome": "error",
        "detail": "code=projection_pending",
    }


def test_diff_restore_and_edit_failure_branches(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")
    client.post("/new", data={"path": "x.md", "content": "v1", "csrf_token": csrf})

    invalid_diff = client.get("/doc/x.md/diff?from=nope&to=1")
    assert invalid_diff.status_code == 400 and "Invalid revision numbers" in invalid_diff.text

    monkeypatch.setattr(ctx.docs, "restore_revision", lambda *a, **k: (_ for _ in ()).throw(ConflictError("race")))
    conflict = client.post("/doc/x.md/rev/1/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert conflict.status_code == 303 and conflict.headers["location"].endswith("/history")
    monkeypatch.setattr(ctx.docs, "restore_revision", lambda *a, **k: (_ for _ in ()).throw(NotFoundError("gone")))
    missing = client.post("/doc/x.md/rev/99/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert missing.status_code == 303

    monkeypatch.setattr(ctx.docs, "update", lambda *a, **k: (_ for _ in ()).throw(PathError("bad")))
    bad_edit = client.post(
        "/doc/x.md/edit",
        data={"content": "new", "base_version": 1, "csrf_token": csrf},
    )
    assert bad_edit.status_code == 400 and "잘못된 경로" in bad_edit.text
    monkeypatch.setattr(ctx.docs, "update", lambda *a, **k: (_ for _ in ()).throw(NotFoundError("gone")))
    missing_edit = client.post(
        "/doc/x.md/edit",
        data={"content": "new", "base_version": 1, "csrf_token": csrf},
    )
    assert missing_edit.status_code == 404 and "gone" in missing_edit.text


@pytest.mark.parametrize(
    ("method", "path", "service", "form", "action"),
    [
        ("post", "/admin/users", "create_user", {"username": "alice", "password": "secret12", "role": "editor"}, "user_create"),
        ("post", "/admin/users/999/role", "set_role", {"role": "editor"}, "role_change"),
        ("post", "/admin/users/999/active", "set_active", {"active": "1"}, "user_active"),
        ("post", "/admin/users/999/password", "set_password", {"password": "secret12"}, "password_change"),
        ("post", "/admin/users/999/delete", "delete_user", {}, "user_delete"),
    ],
)
def test_admin_failures_are_audited(ctx, principals, monkeypatch, method, path, service, form, action):
    client = _client(ctx, principals)
    _login(client, "admin")
    import llm_wiki.web.routes.settings_admin as settings_mod

    target = settings_mod if service == "create_user" else users_svc
    monkeypatch.setattr(target, service, lambda *a, **k: (_ for _ in ()).throw(ValidationError("rejected")))
    form["csrf_token"] = _token(client, "/admin/users")
    response = getattr(client, method)(path, data=form, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/users"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT outcome, detail FROM audit_log WHERE actor='admin' AND action=? ORDER BY id DESC LIMIT 1",
            (action,),
        ).fetchone()
    assert dict(row) == {"outcome": "error", "detail": "rejected"}


class _FakeWebSocket:
    def __init__(self, *, origin=None):
        self.headers = {"origin": origin} if origin else {}
        self.url = SimpleNamespace(netloc="testserver")
        self.session = {"sid": "sid"}
        self.closed = []
        self.accepted = False
        self.sent = []

    async def close(self, code):
        self.closed.append(code)

    async def accept(self):
        self.accepted = True

    async def send_json(self, value):
        self.sent.append(value)

    async def receive(self):
        raise RuntimeError("wire failed")


@pytest.mark.asyncio
async def test_websocket_origin_and_unexpected_receive_are_observable(ctx, principals, monkeypatch, caplog):
    app = create_web_app(ctx)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/ws")

    cross = _FakeWebSocket(origin="https://evil.example")
    await endpoint(cross)
    assert cross.closed == [1008] and not cross.accepted

    good = _FakeWebSocket()
    monkeypatch.setattr(web_mod, "principal_from_session", lambda _db, _sid: principals["editor"])
    await endpoint(good)
    assert good.accepted and good.sent == [{"type": "ready"}]
    assert "websocket receive failed" in caplog.text


@pytest.mark.asyncio
async def test_websocket_clean_disconnect_is_swallowed(ctx, principals, monkeypatch):
    app = create_web_app(ctx)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/ws")
    ws = _FakeWebSocket()

    async def disconnect():
        raise WebSocketDisconnect()

    ws.receive = disconnect
    monkeypatch.setattr(web_mod, "principal_from_session", lambda _db, _sid: principals["editor"])
    await endpoint(ws)
    assert ws.accepted and ws.closed == []


def test_build_info_and_missing_static_are_best_effort(ctx, principals, monkeypatch):
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda _name: (_ for _ in ()).throw(importlib.metadata.PackageNotFoundError()),
    )
    web_mod._set_build_info(SimpleNamespace(model_name="model", dim=3))

    class BrokenEmbedder:
        model_name = "model"

        @property
        def dim(self):
            raise RuntimeError("unavailable")

    web_mod._set_build_info(BrokenEmbedder())

    original_stat = web_mod.Path.stat

    def missing_static(path, *args, **kwargs):
        if "/static/" in str(path):
            raise OSError("missing")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(web_mod.Path, "stat", missing_static)
    client = _client(ctx, principals)
    assert client.get("/login").status_code == 200


def test_embed_resolution_missing_and_lookup_failure(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")
    client.post(
        "/new",
        data={"path": "embed.md", "content": "![[unresolved]]\n![[gone]]", "csrf_token": csrf},
    )
    original_resolve = ctx.docs.resolve_link
    original_get = ctx.docs.get

    def resolve(target, from_path):
        return "gone.md" if target == "gone" else original_resolve(target, from_path)

    def get(path):
        if path == "gone.md":
            raise NotFoundError("gone")
        return original_get(path)

    monkeypatch.setattr(ctx.docs, "resolve_link", resolve)
    monkeypatch.setattr(ctx.docs, "get", get)
    response = client.get("/doc/embed.md")
    assert response.status_code == 200 and "embed-missing" in response.text


def test_bearer_auth_raw_and_unauthorized_raw(ctx, principals):
    client = _client(ctx, principals)
    ctx.docs.create(principals["editor"], "raw.md", "body")
    assert client.get("/doc/raw.md/raw", follow_redirects=False).status_code == 303
    key = create_api_key(ctx.db, principals["viewer"], "reader")
    response = client.get("/doc/raw.md/raw", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200 and response.text == "body"


def test_write_actions_are_classified_through_public_http_routes(
    ctx, principals, monkeypatch, caplog
):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")
    ctx.docs.create(principals["editor"], "route.md", "v1")

    def latest() -> dict:
        with ctx.db.reader() as conn:
            row = conn.execute(
                "SELECT action, target, outcome, detail FROM audit_log "
                "WHERE actor='alice' AND outcome != 'ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row)

    def reject(exc):
        def raising(*args, **kwargs):
            raise exc

        return raising

    original = ctx.docs.create
    monkeypatch.setattr(ctx.docs, "create", reject(PathError("bad path")))
    response = client.post(
        "/new", data={"path": "bad.md", "content": "secret", "csrf_token": csrf}
    )
    assert response.status_code == 400 and "잘못된 경로" in response.text
    assert latest() == {
        "action": "doc_create", "target": "bad.md", "outcome": "error", "detail": "code=bad_path"
    }
    monkeypatch.setattr(ctx.docs, "create", original)

    original = ctx.docs.update
    monkeypatch.setattr(ctx.docs, "update", reject(ValidationError("rejected update")))
    response = client.post(
        "/doc/route.md/edit",
        data={"content": "secret", "base_version": 1, "csrf_token": csrf},
    )
    assert response.status_code == 400 and "rejected update" in response.text
    assert latest() == {
        "action": "doc_update", "target": "route.md", "outcome": "error", "detail": "code=validation"
    }
    monkeypatch.setattr(ctx.docs, "update", original)

    original = ctx.docs.delete
    monkeypatch.setattr(ctx.docs, "delete", reject(ConflictError("delete race")))
    response = client.post(
        "/doc/route.md/delete", data={"base_version": 1, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/doc/route.md"
    assert latest() == {
        "action": "doc_delete", "target": "route.md", "outcome": "conflict", "detail": "code=conflict"
    }
    monkeypatch.setattr(ctx.docs, "delete", original)

    original = ctx.docs.restore
    monkeypatch.setattr(ctx.docs, "restore", reject(NotFoundError("not trashed")))
    response = client.post(
        "/trash/route.md/restore", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/trash"
    assert latest() == {
        "action": "doc_restore", "target": "route.md", "outcome": "error", "detail": "code=not_found"
    }
    monkeypatch.setattr(ctx.docs, "restore", original)

    original = ctx.docs.purge
    monkeypatch.setattr(ctx.docs, "purge", reject(ValidationError("not deleted")))
    response = client.post(
        "/trash/route.md/purge", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/trash"
    assert latest() == {
        "action": "doc_purge", "target": "route.md", "outcome": "error", "detail": "code=validation"
    }
    monkeypatch.setattr(ctx.docs, "purge", original)

    original = ctx.docs.restore_revision
    monkeypatch.setattr(ctx.docs, "restore_revision", reject(ConflictError("restore race")))
    response = client.post(
        "/doc/route.md/rev/1/restore", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].endswith("/history")
    assert latest() == {
        "action": "doc_restore", "target": "route.md", "outcome": "conflict", "detail": "code=conflict"
    }
    monkeypatch.setattr(ctx.docs, "restore_revision", original)

    original = ctx.docs.move
    monkeypatch.setattr(ctx.docs, "move", reject(ValidationError("bad destination")))
    response = client.post(
        "/api/doc/route.md/move", data={"new_path": "new.md", "csrf_token": csrf}
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation"
    assert latest() == {
        "action": "doc_move", "target": "/api/doc/route.md/move",
        "outcome": "error", "detail": "code=validation",
    }
    monkeypatch.setattr(ctx.docs, "move", original)

    original = ctx.docs.replace_properties
    monkeypatch.setattr(ctx.docs, "replace_properties", reject(ConflictError("property race")))
    response = client.post(
        "/api/doc/route.md/properties",
        json={"base_version": 1, "properties": []}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"
    assert latest() == {
        "action": "doc_update", "target": "/api/doc/route.md/properties",
        "outcome": "conflict", "detail": "code=conflict",
    }
    monkeypatch.setattr(ctx.docs, "replace_properties", original)

    original = ctx.docs.save_attachment
    monkeypatch.setattr(ctx.docs, "save_attachment", reject(ValidationError("bad attachment")))
    response = client.post(
        "/api/upload", files={"file": ("pic.png", b"png", "image/png")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == "validation"
    assert latest() == {
        "action": "attachment_upload", "target": "/api/upload",
        "outcome": "error", "detail": "code=validation",
    }
    monkeypatch.setattr(ctx.docs, "save_attachment", original)

    import llm_wiki.web.routes.settings_admin as settings_mod

    original = settings_mod.create_api_key
    monkeypatch.setattr(settings_mod, "create_api_key", reject(ValidationError("key rejected")))
    response = client.post("/settings/keys", data={"name": "bad", "csrf_token": csrf})
    assert response.status_code == 400 and response.headers["x-error-code"] == "validation"
    assert latest() == {
        "action": "key_change", "target": "/settings/keys",
        "outcome": "error", "detail": "code=validation",
    }
    monkeypatch.setattr(settings_mod, "create_api_key", original)

    original = ctx.docs.create_folder
    monkeypatch.setattr(ctx.docs, "create_folder", reject(PathError("bad folder")))
    response = client.post("/api/folders", data={"path": "bad", "csrf_token": csrf})
    assert response.status_code == 400 and response.json()["error"] == "bad_path"
    assert latest() == {
        "action": "write_rejected", "target": "/api/folders",
        "outcome": "error", "detail": "code=bad_path",
    }
    monkeypatch.setattr(ctx.docs, "create_folder", original)

    monkeypatch.setattr(
        web_mod.audit,
        "record_tx",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    monkeypatch.setattr(ctx.docs, "create_folder", reject(PathError("bad folder")))
    response = client.post("/api/folders", data={"path": "bad", "csrf_token": csrf})
    assert response.status_code == 400 and response.json()["error"] == "bad_path"
    assert "failed to audit rejected web write" in caplog.text


def test_html_path_error_and_unsafe_anonymous_wiki_error(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client)
    monkeypatch.setattr(ctx.docs, "list_docs", lambda **kw: (_ for _ in ()).throw(PathError("bad folder")))
    response = client.get("/")
    assert response.status_code == 400 and "잘못된 경로" in response.text

    _logout(client)
    import llm_wiki.web.routes.auth_pages as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate", lambda *a: (_ for _ in ()).throw(PathError("bad login")))
    bad_path = client.post(
        "/login",
        data={"username": "alice", "password": "secret12", "csrf_token": _token(client)},
    )
    assert bad_path.status_code == 400 and "잘못된 경로" in bad_path.text
    monkeypatch.setattr(auth_mod, "authenticate", lambda *a: (_ for _ in ()).throw(ValidationError("login backend")))
    post = client.post(
        "/login",
        data={"username": "alice", "password": "secret12", "csrf_token": _token(client)},
    )
    assert post.status_code == 400 and post.headers["x-error-code"] == "validation"


def test_remaining_page_success_and_empty_branches(ctx, principals):
    ctx.embed_worker = SimpleNamespace(status=lambda: {"running": True})
    client = _client(ctx, principals)
    assert client.get("/llms-full.txt").status_code == 401
    _login(client)
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.get("/search").status_code == 200
    daily = client.post(
        "/daily", data={"csrf_token": _token(client, "/")}, follow_redirects=False
    )
    assert daily.status_code == 303 and daily.headers["location"].startswith("/doc/")
    ready = client.get("/readyz")
    assert ready.json()["embed_worker"] == {"running": True}

    csrf = _token(client, "/new")
    assert client.get("/go?target=absent&_=x", follow_redirects=False).headers["location"].startswith("/new")
    client.post("/new", data={"path": "target.md", "content": "target", "csrf_token": csrf})
    assert client.get("/go?target=target&_=x", follow_redirects=False).headers["location"] == "/doc/target.md"
    assert client.post("/api/folders", data={"path": "empty", "csrf_token": csrf}).status_code == 200
    deleted_folder = client.post("/api/folders/empty/delete", data={"csrf_token": csrf})
    assert deleted_folder.status_code == 200 and deleted_folder.json()["ok"]
    assert client.get("/trash").status_code == 200

    client.post("/new", data={"path": "gone.md", "content": "old", "csrf_token": csrf})
    assert client.get("/doc/gone.md/rev/1").status_code == 200
    assert client.post("/doc/gone.md/delete", data={"base_version": 1, "csrf_token": csrf}).status_code == 200
    assert client.get("/doc/gone.md").status_code == 200
    assert client.get("/api/doc/nope.md/related").json() == {"ok": True, "related": []}
    restored = client.post("/trash/gone.md/restore", data={"csrf_token": csrf}, follow_redirects=False)
    assert restored.status_code == 303
    client.post("/doc/gone.md/delete", data={"base_version": 3, "csrf_token": csrf})
    purged = client.post("/trash/gone.md/purge", data={"csrf_token": csrf}, follow_redirects=False)
    assert purged.status_code == 303

    _logout(client)
    _login(client, "admin")
    admin_csrf = _token(client, "/new")
    client.post("/new", data={"path": "purge.md", "content": "x", "csrf_token": admin_csrf})
    client.post("/doc/purge.md/delete", data={"base_version": 1, "csrf_token": admin_csrf})
    assert client.post("/trash/purge.md/purge", data={"csrf_token": admin_csrf}).status_code == 200


def test_new_and_edit_get_route_path_failures(ctx, principals, monkeypatch):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/new")
    monkeypatch.setattr(ctx.docs, "create", lambda *a, **k: (_ for _ in ()).throw(PathError("bad")))
    response = client.post(
        "/new", data={"path": "x.md", "content": "x", "csrf_token": csrf}
    )
    assert response.status_code == 400 and "잘못된 경로" in response.text
    assert client.get("/doc/missing.md/edit", follow_redirects=False).headers["location"].startswith("/new")


def test_key_mint_rate_limit_envelope(ctx, principals):
    client = _client(ctx, principals)
    _login(client)
    csrf = _token(client, "/settings")
    for i in range(10):
        assert client.post("/settings/keys", data={"name": f"k{i}", "csrf_token": csrf}).status_code == 200
    blocked = client.post("/settings/keys", data={"name": "blocked", "csrf_token": csrf})
    assert blocked.status_code == 429 and "너무 잦습니다" in blocked.text


@pytest.mark.asyncio
async def test_websocket_timeout_continue_and_outer_disconnect(ctx, principals, monkeypatch):
    app = create_web_app(ctx)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/ws")
    session_checks = []

    def active_session(_db, sid):
        session_checks.append(sid)
        return principals["editor"]

    monkeypatch.setattr(web_mod, "principal_from_session", active_session)
    original_wait = asyncio.wait
    calls = 0

    async def timeout_then_receive(tasks, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return set(), set(tasks)
        return await original_wait(tasks, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)

    monkeypatch.setattr(asyncio, "wait", timeout_then_receive)
    ws = _FakeWebSocket()

    async def inbound():
        return {"type": "websocket.disconnect"}

    ws.receive = inbound
    await endpoint(ws)
    assert calls == 2 and ws.accepted and ws.closed == []
    assert session_checks == ["sid", "sid"]
    assert ctx.events.subscriber_count() == 0

    revoked_checks = []

    def revoked_session(_db, sid):
        revoked_checks.append(sid)
        return principals["editor"] if len(revoked_checks) == 1 else None

    async def timeout_only(tasks, **kwargs):
        return set(), set(tasks)

    monkeypatch.setattr(web_mod, "principal_from_session", revoked_session)
    monkeypatch.setattr(asyncio, "wait", timeout_only)
    revoked = _FakeWebSocket()
    await endpoint(revoked)
    assert revoked_checks == ["sid", "sid"]
    assert revoked.closed == [1008]
    assert ctx.events.subscriber_count() == 0

    async def disconnecting_wait(*args, **kwargs):
        raise WebSocketDisconnect()

    monkeypatch.setattr(asyncio, "wait", disconnecting_wait)
    monkeypatch.setattr(web_mod, "principal_from_session", active_session)
    ws2 = _FakeWebSocket()
    ws2.receive = inbound
    await endpoint(ws2)
    assert ws2.accepted and ws2.closed == []
    assert ctx.events.subscriber_count() == 0
    monkeypatch.setattr(asyncio, "wait", original_wait)
