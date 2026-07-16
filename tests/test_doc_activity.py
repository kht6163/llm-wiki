"""Per-document activity timeline: audit path filter, API, and view wiring."""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.services import audit
from llm_wiki.services.auth import Principal, create_api_key, principal_from_api_key
from llm_wiki.web import create_web_app


def _p(ctx, principals, via: str = "web") -> Principal:
    e = principals["editor"]
    if via == "mcp":
        token = create_api_key(ctx.db, e, "doc-activity")
        principal = principal_from_api_key(ctx.db, token)
        assert principal is not None
        return principal
    return Principal(e.user_id, e.username, e.role, via=via)


def _token(client: TestClient, path: str = "/login") -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m
    return m.group(1)


def login(client: TestClient, username: str = "alice", password: str = "secret12"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _token(client)},
    )


@pytest.fixture
def client(ctx, principals):
    return TestClient(create_web_app(ctx))


# ---- audit.recent path filters ----------------------------------------------


def test_audit_recent_target_exact_match(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals), "keep.md", "a")
    docs.create(_p(ctx, principals), "other.md", "b")
    docs.update(_p(ctx, principals), "keep.md", 1, "a2")

    rows = audit.recent(ctx.db, target="keep.md", actions=audit.DOC_ACTIONS)
    assert rows
    assert all(r["target"] == "keep.md" for r in rows)
    assert {r["action"] for r in rows} >= {"doc_create", "doc_update"}


def test_audit_recent_target_path_matches_moves(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals), "old.md", "x")
    docs.create(_p(ctx, principals), "unrelated.md", "y")
    docs.move(_p(ctx, principals), "old.md", "renamed.md")

    # Viewing the new path surfaces create (no — create was on old.md) + move.
    # Path filter matches exact target OR either side of "old -> new".
    for path in ("old.md", "renamed.md"):
        rows = audit.recent(
            ctx.db, target_path=path, actions=audit.DOC_ACTIONS, limit=50
        )
        actions = {r["action"] for r in rows}
        assert "doc_move" in actions
        assert all(
            r["target"] == path
            or r["target"] == "old.md -> renamed.md"
            or (r["target"] or "").startswith(f"{path} -> ")
            or (r["target"] or "").endswith(f" -> {path}")
            for r in rows
        )
        assert not any(r["target"] == "unrelated.md" for r in rows)

    # Exact target= does not match move rows.
    exact = audit.recent(ctx.db, target="old.md", actions=audit.DOC_ACTIONS)
    assert all(r["target"] == "old.md" for r in exact)
    assert all(r["action"] != "doc_move" for r in exact)


def test_audit_recent_target_path_escapes_like_metacharacters(ctx):
    # A path containing SQL LIKE wildcards must not broaden the match.
    audit.record_tx(
        ctx.db, actor="a", via="web", action="doc_create", target="100%_done.md"
    )
    audit.record_tx(
        ctx.db, actor="a", via="web", action="doc_create", target="100XYdone.md"
    )
    audit.record_tx(
        ctx.db,
        actor="a",
        via="web",
        action="doc_move",
        target="100%_done.md -> safe.md",
    )
    rows = audit.recent(ctx.db, target_path="100%_done.md", actions=audit.DOC_ACTIONS)
    targets = {r["target"] for r in rows}
    assert "100%_done.md" in targets
    assert "100%_done.md -> safe.md" in targets
    assert "100XYdone.md" not in targets


# ---- API --------------------------------------------------------------------


def test_doc_activity_api_requires_session(client, ctx, principals):
    ctx.docs.create(_p(ctx, principals), "a.md", "x")
    r = client.get("/api/doc/a.md/activity")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_doc_activity_api_returns_path_events(client, ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "note.md", "v1")
    docs.update(_p(ctx, principals, "web"), "note.md", 1, "v2")
    docs.create(_p(ctx, principals), "other.md", "z")
    audit.record_tx(
        ctx.db, actor="alice", via="web", action="share_mint", target="note.md",
        detail="expires=2099-01-01T00:00:00Z",
    )
    audit.record_tx(
        ctx.db, actor="mallory", via="web", action="login_failed", outcome="error"
    )

    login(client, "alice")
    r = client.get("/api/doc/note.md/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    events = body["events"]
    assert events
    assert all(
        set(e) >= {"ts", "actor", "via", "action", "outcome", "detail", "target"}
        for e in events
    )
    actions = {e["action"] for e in events}
    assert "doc_create" in actions and "doc_update" in actions
    assert "share_mint" in actions
    assert "login_failed" not in actions
    assert all(
        e["target"] == "note.md"
        or (e["target"] or "").startswith("note.md -> ")
        or (e["target"] or "").endswith(" -> note.md")
        for e in events
    )
    # Newest first.
    assert events[0]["ts"] >= events[-1]["ts"]


def test_doc_activity_api_respects_limit(client, ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals), "lim.md", "1")
    for i in range(5):
        docs.update(_p(ctx, principals), "lim.md", i + 1, f"v{i + 2}")
    login(client, "alice")
    r = client.get("/api/doc/lim.md/activity?limit=3")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 3


# ---- View wiring ------------------------------------------------------------


def test_view_includes_activity_tab_and_script(client, ctx, principals):
    ctx.docs.create(_p(ctx, principals), "view-me.md", "# Hello\n\nbody")
    login(client, "alice")
    r = client.get("/doc/view-me.md")
    assert r.status_code == 200
    html = r.text
    assert 'data-rp="activity"' in html
    assert "활동" in html
    assert 'id="rp-activity"' in html or 'data-rp-panel="activity"' in html
    assert "activity.js" in html
    # Still ships the other two tabs.
    assert 'data-rp="outline"' in html and 'data-rp="links"' in html
