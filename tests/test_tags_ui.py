"""Web UI for vault-wide tag rename/merge: auth, RBAC, CSRF, and happy paths."""
import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.web import create_web_app


@pytest.fixture
def client(ctx, principals):
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


def logout(client: TestClient):
    return client.post("/logout", data={"csrf_token": _token(client, "/")})


def create_doc(client: TestClient, path: str, content: str, title: str = ""):
    return client.post(
        "/new",
        data={"path": path, "content": content, "title": title, "csrf_token": _token(client, "/new")},
    )


def test_tags_page_requires_auth(client):
    r = client.get("/tags", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_tags_page_lists_tags_for_viewer_without_write_forms(client):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [alpha, beta]\n---\n# A\n\nbody")
    logout(client)
    login(client, "bob")  # viewer
    r = client.get("/tags")
    assert r.status_code == 200
    assert "alpha" in r.text and "beta" in r.text
    assert 'action="/tags/rename"' not in r.text
    assert 'action="/tags/merge"' not in r.text
    assert 'name="new"' not in r.text


def test_tags_page_shows_rename_and_merge_forms_for_editor(client):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [alpha, beta]\n---\n# A\n\nbody")
    r = client.get("/tags")
    assert r.status_code == 200
    assert 'action="/tags/rename"' in r.text
    assert 'action="/tags/merge"' in r.text
    assert 'name="csrf_token"' in r.text
    assert "alpha" in r.text and "beta" in r.text


def test_rename_tag_works_for_editor(client, ctx):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [oldtag]\n---\n# A\n\nbody")
    tok = _token(client, "/tags")
    r = client.post(
        "/tags/rename",
        data={"old": "oldtag", "new": "newtag", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/tags"
    page = client.get("/tags")
    assert "newtag" in page.text
    tags = {t["tag"] for t in ctx.docs.tags()}
    assert "newtag" in tags and "oldtag" not in tags


def test_merge_tags_works_for_editor(client, ctx):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [draft, wip]\n---\n# A\n\nbody")
    create_doc(client, "b.md", "---\ntags: [draft]\n---\n# B\n\nbody")
    tok = _token(client, "/tags")
    r = client.post(
        "/tags/merge",
        data={
            "sources": ["draft", "wip"],
            "dest": "in-progress",
            "csrf_token": tok,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/tags"
    tags = {t["tag"] for t in ctx.docs.tags()}
    assert "in-progress" in tags
    assert "draft" not in tags and "wip" not in tags


def test_tag_routes_disclose_partial_and_projection_pending_results(
    client, ctx, monkeypatch
):
    login(client, "alice")
    partial = {
        "sources": ["old"],
        "dest": "new",
        "docs_changed": 1,
        "docs_skipped": 2,
        "projection_pending": [{"path": "note.md", "reason": "busy"}],
    }
    monkeypatch.setattr(ctx.docs, "rename_tag", lambda *args, **kwargs: partial)
    renamed = client.post(
        "/tags/rename",
        data={"old": "old", "new": "new", "csrf_token": _token(client, "/tags")},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    rename_flash = client.get("/tags").text
    assert "건너뜀 2개" in rename_flash and "파일 반영 지연 1개" in rename_flash

    monkeypatch.setattr(ctx.docs, "merge_tags", lambda *args, **kwargs: partial)
    merged = client.post(
        "/tags/merge",
        data={"sources": ["old"], "dest": "new", "csrf_token": _token(client, "/tags")},
        follow_redirects=False,
    )
    assert merged.status_code == 303
    merge_flash = client.get("/tags").text
    assert "건너뜀 2개" in merge_flash and "파일 반영 지연 1개" in merge_flash


def _csrf_from_page(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'csrf:\s*"([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


def test_viewer_cannot_rename_tag(client, ctx):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [keep]\n---\n# A\n\nbody")
    logout(client)
    login(client, "bob")
    tok = _csrf_from_page(client.get("/tags").text)
    r = client.post(
        "/tags/rename",
        data={"old": "keep", "new": "changed", "csrf_token": tok},
        follow_redirects=False,
    )
    tags = {t["tag"] for t in ctx.docs.tags()}
    assert "keep" in tags and "changed" not in tags
    if r.status_code == 303:
        assert r.headers["location"] == "/tags"
        assert "실패" in client.get("/tags").text
    else:
        assert r.status_code == 403


def test_viewer_cannot_merge_tags(client, ctx):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [one, two]\n---\n# A\n\nbody")
    logout(client)
    login(client, "bob")
    tok = _csrf_from_page(client.get("/tags").text)
    r = client.post(
        "/tags/merge",
        data={"sources": ["one"], "dest": "merged", "csrf_token": tok},
        follow_redirects=False,
    )
    tags = {t["tag"] for t in ctx.docs.tags()}
    assert "one" in tags and "merged" not in tags
    if r.status_code == 303:
        assert r.headers["location"] == "/tags"
    else:
        assert r.status_code == 403


def test_rename_tag_requires_csrf(client):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [csrf-tag]\n---\n# A\n\nbody")
    r = client.post(
        "/tags/rename",
        data={"old": "csrf-tag", "new": "other", "csrf_token": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_rename_tag_empty_new_flashes_error(client, ctx):
    login(client, "alice")
    create_doc(client, "a.md", "---\ntags: [x]\n---\n# A\n\nbody")
    tok = _token(client, "/tags")
    r = client.post(
        "/tags/rename",
        data={"old": "x", "new": "", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/tags"
    tags = {t["tag"] for t in ctx.docs.tags()}
    assert "x" in tags
    page = client.get("/tags")
    assert "flash" in page.text or "실패" in page.text or "empty" in page.text.lower()
