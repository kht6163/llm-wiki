"""One-click document creation from the broken-links page."""
import re
from urllib.parse import unquote

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


def test_broken_links_page_requires_auth(client):
    r = client.get("/broken-links", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_broken_links_create_button_for_editor_not_viewer(client):
    login(client, "alice")
    create_doc(client, "src.md", "see [[ghosttarget]] for details")
    editor_page = client.get("/broken-links")
    assert editor_page.status_code == 200
    assert "ghosttarget" in editor_page.text
    assert "문서 만들기" in editor_page.text
    assert 'action="/broken-links/create"' in editor_page.text

    logout(client)
    login(client, "bob")
    viewer_page = client.get("/broken-links")
    assert viewer_page.status_code == 200
    assert "ghosttarget" in viewer_page.text
    assert "문서 만들기" not in viewer_page.text
    assert 'action="/broken-links/create"' not in viewer_page.text


def _doc_path_from_location(loc: str) -> str:
    """Strip /doc/ prefix and optional /edit suffix."""
    assert loc.startswith("/doc/"), loc
    rest = loc.removeprefix("/doc/")
    if rest.endswith("/edit"):
        rest = rest[: -len("/edit")]
    return unquote(rest)


def test_create_from_broken_link_bare_name(client, ctx):
    login(client, "alice")
    create_doc(client, "src.md", "see [[ghosttarget]] for details")
    tok = _token(client, "/broken-links")
    r = client.post(
        "/broken-links/create",
        data={"target": "ghosttarget", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/doc/")
    path = _doc_path_from_location(loc)
    assert path.lower().endswith(".md")
    assert "ghosttarget" in path.lower()
    doc = ctx.docs.get(path)
    assert doc["path"] == path


def test_create_from_broken_link_path_target(client, ctx):
    login(client, "alice")
    create_doc(client, "src.md", "see [[folder/missing-note]] here")
    tok = _token(client, "/broken-links")
    r = client.post(
        "/broken-links/create",
        data={"target": "folder/missing-note", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    path = _doc_path_from_location(r.headers["location"])
    assert path == "folder/missing-note.md" or path.endswith("missing-note.md")
    ctx.docs.get(path)


def test_create_from_broken_link_already_exists_redirects(client, ctx):
    login(client, "alice")
    create_doc(client, "exists.md", "# Exists\n\nbody")
    create_doc(client, "src.md", "see [[exists]] for details")
    # Link resolves so /broken-links may have no forms — take CSRF from WIKI shell or /new.
    tok = _csrf_from_page(client.get("/broken-links").text)
    r = client.post(
        "/broken-links/create",
        data={"target": "exists", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/doc/")
    path = _doc_path_from_location(loc)
    assert "exists" in path.lower()


def _csrf_from_page(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'csrf:\s*"([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


def test_viewer_cannot_create_from_broken_link(client, ctx):
    login(client, "alice")
    create_doc(client, "src.md", "see [[ghost2]] for details")
    logout(client)
    login(client, "bob")
    tok = _csrf_from_page(client.get("/broken-links").text)
    r = client.post(
        "/broken-links/create",
        data={"target": "ghost2", "csrf_token": tok},
        follow_redirects=False,
    )
    from llm_wiki.services.errors import NotFoundError

    try:
        ctx.docs.get("ghost2.md")
        created = True
    except NotFoundError:
        created = False
    assert not created
    if r.status_code == 303:
        assert r.headers["location"] == "/broken-links"
    else:
        assert r.status_code == 403


def test_create_from_broken_link_requires_csrf(client):
    login(client, "alice")
    create_doc(client, "src.md", "see [[ghostcsrf]] for details")
    r = client.post(
        "/broken-links/create",
        data={"target": "ghostcsrf", "csrf_token": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_create_from_broken_link_unauthenticated_redirects(client):
    r = client.post(
        "/broken-links/create",
        data={"target": "x", "csrf_token": "nope"},
        follow_redirects=False,
    )
    # CSRF may fire first (403) or auth (303 to login) depending on middleware order.
    assert r.status_code in (303, 403)
    if r.status_code == 303:
        assert r.headers["location"] == "/login"
