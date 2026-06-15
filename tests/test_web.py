"""Web-route tests via Starlette's TestClient: auth redirects, RBAC, CSRF, the
409 conflict path, revision restore, raw download, tags, and security headers.
Authorization lives inline in each handler, so these route-level tests are the
only thing that catches a missing guard."""
import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.web import create_web_app


@pytest.fixture
def client(ctx, principals):
    # principals creates admin/alice(editor)/bob(viewer), all with password "secret12".
    return TestClient(create_web_app(ctx))


def _token(client: TestClient, path: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def login(client: TestClient, username: str, password: str = "secret12"):
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _token(client, "/login")},
    )


def create_doc(client: TestClient, path: str, content: str, title: str = ""):
    return client.post(
        "/new",
        data={"path": path, "content": content, "title": title, "csrf_token": _token(client, "/new")},
    )


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_success_and_logout(client):
    r = login(client, "admin")
    assert r.status_code == 200 and "llm-wiki" in r.text
    assert client.get("/", follow_redirects=False).status_code == 200


def test_bad_password_is_401(client):
    r = login(client, "admin", "wrongpassword")
    assert r.status_code == 401 and "Invalid" in r.text


def test_viewer_cannot_create_admin_can(client):
    login(client, "bob")  # viewer
    r = create_doc(client, "viewer-try.md", "nope")
    assert r.status_code == 403  # ForbiddenError -> edit.html with 403

    client.cookies.clear()
    login(client, "admin")
    r = create_doc(client, "admin-doc.md", "# Hello\n\nbody")
    assert r.status_code == 200 and "Hello" in r.text


def test_non_admin_cannot_reach_admin_page(client):
    login(client, "alice")  # editor
    assert client.get("/admin/users").status_code == 403
    client.cookies.clear()
    login(client, "admin")
    assert client.get("/admin/users").status_code == 200


def test_csrf_token_required(client):
    login(client, "admin")
    # No token at all -> rejected.
    assert client.post("/new", data={"path": "x.md", "content": "y"}).status_code == 403
    # Wrong token -> rejected.
    assert client.post(
        "/new", data={"path": "x.md", "content": "y", "csrf_token": "bogus"}
    ).status_code == 403


def test_cross_origin_post_rejected(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post(
        "/new",
        data={"path": "x.md", "content": "y", "csrf_token": tok},
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403


def test_optimistic_conflict_page(client):
    login(client, "admin")
    create_doc(client, "conf.md", "original")
    # Edit with a stale base_version -> 409 conflict page.
    r = client.post(
        "/doc/conf.md/edit",
        data={"content": "mine", "base_version": "0", "csrf_token": _token(client, "/doc/conf.md/edit")},
    )
    assert r.status_code == 409 and "충돌" in r.text


def test_restore_revision_route(client):
    login(client, "admin")
    create_doc(client, "rollback.md", "first version body")
    client.post(
        "/doc/rollback.md/edit",
        data={"content": "second version body", "base_version": "1",
              "csrf_token": _token(client, "/doc/rollback.md/edit")},
    )
    # Restore v1 via the history page's button.
    tok = _token(client, "/doc/rollback.md/history")
    r = client.post("/doc/rollback.md/rev/1/restore", data={"csrf_token": tok})
    assert r.status_code == 200
    assert "first version body" in client.get("/doc/rollback.md/raw").text


def test_raw_download(client):
    login(client, "admin")
    create_doc(client, "raw.md", "# Raw\n\nplain markdown")
    r = client.get("/doc/raw.md/raw")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "plain markdown" in r.text


def test_tags_page(client):
    login(client, "admin")
    create_doc(client, "tagged.md", "---\ntags: [alpha, beta]\n---\n# T\n\nbody")
    r = client.get("/tags")
    assert r.status_code == 200 and "alpha" in r.text


def test_security_headers_present(client):
    login(client, "admin")
    h = client.get("/").headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in h


def test_diff_route(client):
    login(client, "admin")
    create_doc(client, "diffy.md", "line one\nline two")
    client.post(
        "/doc/diffy.md/edit",
        data={"content": "line one\nline two changed", "base_version": "1",
              "csrf_token": _token(client, "/doc/diffy.md/edit")},
    )
    r = client.get("/doc/diffy.md/diff?from=1&to=2")
    assert r.status_code == 200 and "changed" in r.text and "d-add" in r.text


def test_preview_api(client):
    login(client, "admin")
    r = client.post("/api/preview", data={"content": "# Hi\n\n**bold**", "path": "x.md",
                                          "csrf_token": _token(client, "/new")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and "<strong>bold</strong>" in body["html"]


def test_complete_api(client):
    login(client, "admin")
    create_doc(client, "notes/meeting.md", "# Meeting\n\nbody")
    r = client.get("/api/complete?q=meet")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["path"] == "notes/meeting.md" for it in items)


def test_readiness_probe(client):
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert client.get("/healthz").status_code == 200


def test_api_key_minted_in_response_not_session(client):
    login(client, "admin")
    r = client.post("/settings/keys", data={"name": "agent", "csrf_token": _token(client, "/settings")})
    assert r.status_code == 200
    full = re.search(r"lw_[A-Za-z0-9_\-]+", r.text).group(0)
    assert len(full) > 12  # a real token, not just the displayed 12-char prefix
    # The full raw key must not survive into a later GET (it was rendered directly,
    # never round-tripped through the session cookie). The prefix may still show.
    assert full not in client.get("/settings").text


# -- batch A: metrics ------------------------------------------------------
def test_metrics_endpoint_exposes_prometheus(client):
    login(client, "admin")
    client.get("/")  # generate a request to count
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "llmwiki_http_requests_total" in r.text


# -- batch D: attachments / upload -----------------------------------------
_PNG = b"\x89PNG\r\n\x1a\n" + bytes(40)


def test_upload_and_serve_attachment(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post("/api/upload", files={"file": ("pic.png", _PNG, "image/png")},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["url"].startswith("/attachments/")
    got = client.get(body["url"])
    assert got.status_code == 200 and got.content == _PNG


def test_svg_attachment_served_without_script_execution(client):
    login(client, "admin")
    tok = _token(client, "/new")
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    r = client.post("/api/upload", files={"file": ("d.svg", svg, "image/svg+xml")},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    got = client.get(r.json()["url"])
    assert got.status_code == 200
    csp = got.headers.get("content-security-policy", "")
    # A directly-opened SVG must not run scripts (overrides the site's inline-allowing CSP).
    assert "script-src 'none'" in csp


def test_upload_rejects_unsupported_type(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post("/api/upload", files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation"


def test_upload_forbidden_for_viewer(client):
    login(client, "bob")  # viewer
    tok = _token(client, "/settings")
    r = client.post("/api/upload", files={"file": ("p.png", _PNG, "image/png")},
                    headers={"X-CSRF-Token": tok})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


# -- batch D: search filters + bad-path handling + responsive nav ----------
def test_search_folder_filter(client):
    login(client, "admin")
    create_doc(client, "notes/a.md", "alpha keyword here")
    create_doc(client, "other/b.md", "alpha keyword here too")
    r = client.get("/search?q=alpha&folder=notes&top_k=10")
    assert r.status_code == 200
    assert "notes/a.md" in r.text and "other/b.md" not in r.text


def test_traversal_path_is_400_not_500(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post("/new", data={"path": "../escape.md", "content": "x", "csrf_token": tok})
    assert r.status_code == 400


def test_nav_renders_responsive_menu(client):
    login(client, "admin")
    html = client.get("/").text
    assert 'class="navmenu"' in html and "navlinks" in html
