"""Graph API/service filters: folder, tag, and include_unresolved."""
import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.web import create_web_app


@pytest.fixture
def client(ctx, principals):
    return TestClient(create_web_app(ctx))


def _token(client: TestClient, path: str = "/login") -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert m, f"no csrf token on {path}"
    return m.group(1)


def _login(client: TestClient, username: str = "alice") -> None:
    client.post(
        "/login",
        data={
            "username": username,
            "password": "secret12",
            "csrf_token": _token(client, "/login"),
        },
    )


def _create_doc(client: TestClient, path: str, content: str) -> None:
    client.post(
        "/new",
        data={
            "path": path,
            "content": content,
            "title": "",
            "csrf_token": _token(client, "/new"),
        },
    )


def test_folder_filter_restricts_nodes(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "notes/a.md", "[[b]] in notes")
    docs.create(p, "notes/b.md", "leaf")
    docs.create(p, "other/c.md", "outside folder")

    g = docs.graph(folder="notes")
    ids = {n["id"] for n in g["nodes"] if n.get("exists")}
    assert "notes/a.md" in ids and "notes/b.md" in ids
    assert "other/c.md" not in ids


def test_tag_filter_restricts_nodes(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "tagged.md", "---\ntags: [release]\n---\n\nbody")
    docs.create(p, "plain.md", "no tags here")

    g = docs.graph(tag="release")
    ids = {n["id"] for n in g["nodes"] if n.get("exists")}
    assert "tagged.md" in ids
    assert "plain.md" not in ids


def test_include_unresolved_false_drops_ghosts(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "A.md", "[[B]] and [[ghosttarget]]")
    docs.create(p, "B.md", "x")

    g_on = docs.graph(root="A.md", depth=1, include_unresolved=True)
    assert any(n["id"].startswith("unresolved:") for n in g_on["nodes"])

    g_off = docs.graph(root="A.md", depth=1, include_unresolved=False)
    assert not any(n["id"].startswith("unresolved:") for n in g_off["nodes"])
    assert {n["id"] for n in g_off["nodes"] if n.get("exists")} == {"A.md", "B.md"}


def test_folder_and_tag_filters_combine(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "work/match.md", "---\ntags: [todo]\n---\n\nx")
    docs.create(p, "work/other.md", "---\ntags: [release]\n---\n\ny")
    docs.create(p, "home/match.md", "---\ntags: [todo]\n---\n\nz")

    g = docs.graph(folder="work", tags=["todo"])
    ids = {n["id"] for n in g["nodes"] if n.get("exists")}
    assert ids == {"work/match.md"}


def test_api_graph_honours_folder_and_include_unresolved(client):
    _login(client)
    _create_doc(client, "notes/a.md", "[[ghost]] and body")
    _create_doc(client, "other/b.md", "outside")

    data = client.get("/api/graph", params={"folder": "notes"}).json()
    ids = {n["id"] for n in data["nodes"] if n.get("exists")}
    assert "notes/a.md" in ids
    assert "other/b.md" not in ids

    with_ghosts = client.get(
        "/api/graph", params={"folder": "notes", "include_unresolved": "true"}
    ).json()
    assert any(n["id"].startswith("unresolved:") for n in with_ghosts["nodes"])

    no_ghosts = client.get(
        "/api/graph", params={"folder": "notes", "include_unresolved": "false"}
    ).json()
    assert not any(n["id"].startswith("unresolved:") for n in no_ghosts["nodes"])


def test_api_graph_honours_tag_filter(client):
    _login(client)
    _create_doc(client, "tagged.md", "---\ntags: [release]\n---\n\nbody")
    _create_doc(client, "plain.md", "no tags")

    data = client.get("/api/graph", params={"tag": "release"}).json()
    ids = {n["id"] for n in data["nodes"] if n.get("exists")}
    assert "tagged.md" in ids
    assert "plain.md" not in ids
