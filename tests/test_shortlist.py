"""Shortlist features: favourites + trash + daily-note services, and the web surface
(security headers, key-mint rate limit, favourite toggle, trash, daily, admin form)."""
import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.errors import ForbiddenError, NotFoundError, ValidationError
from llm_wiki.web import create_web_app

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


# -- service: favourites ---------------------------------------------------
def test_favorites_toggle_list_and_per_user(ctx, principals):
    docs, ed = ctx.docs, principals["editor"]
    docs.create(ed, "fav.md", "# F\n\nx")
    assert docs.is_favorite(ed.user_id, "fav.md") is False
    assert docs.toggle_favorite(ed, "fav.md")["favorite"] is True
    assert docs.is_favorite(ed.user_id, "fav.md") is True
    assert [f["path"] for f in docs.list_favorites(ed.user_id)] == ["fav.md"]
    assert docs.list_favorites(principals["viewer"].user_id) == []  # per-user
    assert docs.toggle_favorite(ed, "fav.md")["favorite"] is False
    assert docs.list_favorites(ed.user_id) == []


def test_favorite_missing_doc_raises(ctx, principals):
    with pytest.raises(NotFoundError):
        ctx.docs.toggle_favorite(principals["editor"], "nope.md")


# -- service: trash --------------------------------------------------------
def test_trash_restore_roundtrip(ctx, principals):
    docs, ed = ctx.docs, principals["editor"]
    docs.create(ed, "t.md", "# T\n\nbody")
    docs.delete(ed, "t.md")
    assert [d["path"] for d in docs.list_deleted()] == ["t.md"]
    assert docs.exists("t.md") is False
    docs.restore(ed, "t.md")
    assert docs.exists("t.md") is True and docs.list_deleted() == []
    assert "body" in docs.get("t.md")["content"]  # body + indexes rebuilt


def test_restore_rejects_live(ctx, principals):
    ctx.docs.create(principals["editor"], "l.md", "x")
    with pytest.raises(ValidationError):
        ctx.docs.restore(principals["editor"], "l.md")


def test_purge_is_admin_only_and_removes_history(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "p.md", "x")
    docs.delete(principals["editor"], "p.md")
    with pytest.raises(ForbiddenError):
        docs.purge(principals["editor"], "p.md")
    docs.purge(principals["admin"], "p.md")
    assert docs.list_deleted() == []
    assert docs.create(principals["editor"], "p.md", "y")["version"] == 1  # row truly gone


# -- service: daily note ---------------------------------------------------
def test_daily_note_create_then_idempotent(ctx, principals):
    docs, ed = ctx.docs, principals["editor"]
    d = docs.daily_note(ed, "2026-03-09")
    assert d["created"] is True and d["path"] == "daily/2026-03-09.md"
    assert docs.daily_note(ed, "2026-03-09")["created"] is False
    with pytest.raises(ValidationError):
        docs.daily_note(ed, "bad-date")


def test_daily_note_viewer_reads_but_cannot_create(ctx, principals):
    docs = ctx.docs
    with pytest.raises(ForbiddenError):
        docs.daily_note(principals["viewer"], "2026-03-10")
    docs.daily_note(principals["editor"], "2026-03-10")  # editor creates it
    assert docs.daily_note(principals["viewer"], "2026-03-10")["created"] is False  # viewer reads


# -- web surface -----------------------------------------------------------
@pytest.fixture
def client(ctx, principals):
    return TestClient(create_web_app(ctx))


def _csrf(client: TestClient, path: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def _login(client: TestClient, username: str = "alice") -> None:
    client.post("/login", data={"username": username, "password": "secret12",
                                "csrf_token": _csrf(client, "/login")})


def test_security_headers_no_hsts_on_plain_http(client):
    h = {k.lower() for k in client.get("/login").headers}
    assert "x-content-type-options" in h and "content-security-policy" in h
    assert "strict-transport-security" not in h  # not pinned without HTTPS


def test_hsts_present_when_cookie_secure(tmp_path):
    s = Settings(vault_path=tmp_path / "v", db_path=tmp_path / "d.db",
                 embedding_model=TEST_MODEL, gui_port=8090, mcp_port=8091,
                 session_secret="x", cookie_secure=True)
    c = TestClient(create_web_app(build_context(s, full=True)))
    h = {k.lower(): v for k, v in c.get("/login").headers.items()}
    assert "strict-transport-security" in h and "max-age=" in h["strict-transport-security"]


def test_key_mint_is_rate_limited(client):
    _login(client)
    token = _csrf(client, "/settings")
    statuses = [client.post("/settings/keys",
                            data={"name": f"k{i}", "csrf_token": token}).status_code
                for i in range(11)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429  # 11th over the window cap


def test_favorite_toggle_via_web(ctx, principals, client):
    ctx.docs.create(principals["editor"], "wfav.md", "# W\n\nx")
    _login(client)
    client.post("/doc/wfav.md/favorite", data={"csrf_token": _csrf(client, "/doc/wfav.md")})
    html = client.get("/doc/wfav.md").text
    assert "fav-toggle is-fav" in html and 'aria-pressed="true"' in html
    # the favourite now shows in the sidebar
    assert "sb-favorites" in html


def test_trash_page_and_restore_via_web(ctx, principals, client):
    docs = ctx.docs
    docs.create(principals["editor"], "wt.md", "# WT\n\nbody")
    docs.delete(principals["editor"], "wt.md")
    _login(client)
    assert "wt.md" in client.get("/trash").text
    client.post("/trash/wt.md/restore", data={"csrf_token": _csrf(client, "/trash")})
    assert docs.exists("wt.md") is True


def test_daily_route_redirects_to_today(client):
    _login(client)
    assert client.get("/daily", follow_redirects=False).status_code == 405
    assert client.post(
        "/daily", data={"csrf_token": "invalid"}, follow_redirects=False
    ).status_code == 403
    r = client.post(
        "/daily", data={"csrf_token": _csrf(client, "/")}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/doc/daily/")


def test_admin_role_form_has_no_oninput_autosubmit(client):
    _login(client, username="admin")
    html = client.get("/admin/users").text
    assert 'onchange="this.form.submit()"' not in html  # WCAG 3.2.2: explicit apply
    assert "적용" in html


def test_attachment_served_with_explicit_media_type(ctx, principals, client):
    # Uploaded attachments are served with an explicit Content-Type (no sniffing).
    res = ctx.docs.save_attachment(principals["editor"], "pic.png", b"\x89PNG\r\n\x1a\nfake")
    _login(client)
    r = client.get(res["url"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers.get("x-content-type-options") == "nosniff"
