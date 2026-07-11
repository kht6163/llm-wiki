"""Regression tests for the Obsidian-style shell features: explicit (empty)
folders, the file tree, the folder/move/tree API routes, click-to-toggle task
service, and the status-bar word count."""
import re

import pytest
from starlette.testclient import TestClient

from llm_wiki.services.errors import ConflictError, ForbiddenError, ValidationError
from llm_wiki.util import PathError, normalize_folder_path, word_count
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


# -- util.word_count --------------------------------------------------------
def test_word_count_counts_cjk_per_character():
    # 한글/CJK 글자당, 라틴은 공백 단위. 공백 포함 chars.
    assert word_count("hello world") == {"words": 2, "chars": 11}
    assert word_count("안녕하세요 hello")["words"] == 6  # 5 한글 + 1 라틴
    assert word_count("")["words"] == 0


def test_normalize_folder_path_strips_and_rejects_traversal():
    assert normalize_folder_path("/a/b/") == "a/b"
    assert normalize_folder_path("  notes ") == "notes"
    assert normalize_folder_path("") == ""
    with pytest.raises(PathError):
        normalize_folder_path("../escape")


# -- DocumentService folders + tree -----------------------------------------
def test_create_folder_persists_empty_and_shows_in_tree(ctx, principals):
    docs = ctx.docs
    res = docs.create_folder(principals["editor"], "Projects")
    assert res["path"] == "Projects"
    # Empty folder appears in the tree even with no documents.
    tree = docs.tree()
    names = [f["name"] for f in tree["folders"]]
    assert "Projects" in names
    # ...and a real directory is projected into the vault.
    assert (ctx.docs.vault / "Projects").is_dir()
    # folder_counts includes it with a zero count.
    assert ("Projects", 0) in docs.folder_counts()


def test_create_folder_duplicate_conflicts(ctx, principals):
    ctx.docs.create_folder(principals["editor"], "dup")
    with pytest.raises(ConflictError):
        ctx.docs.create_folder(principals["editor"], "dup")


def test_create_folder_forbidden_for_viewer(ctx, principals):
    with pytest.raises(ForbiddenError):
        ctx.docs.create_folder(principals["viewer"], "nope")


def test_tree_nests_documents_under_folders(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "area/sub/deep.md", "# Deep")
    docs.create(principals["editor"], "area/top.md", "# Top")
    tree = docs.tree()
    area = next(f for f in tree["folders"] if f["name"] == "area")
    # 'area' holds doc top.md and subfolder 'sub'; 'sub' holds deep.md.
    assert any(d["path"] == "area/top.md" for d in area["docs"])
    sub = next(f for f in area["folders"] if f["name"] == "sub")
    assert any(d["path"] == "area/sub/deep.md" for d in sub["docs"])


def test_delete_folder_refuses_when_not_empty(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "keep/a.md", "x")
    with pytest.raises(ValidationError):
        docs.delete_folder(principals["editor"], "keep")


def test_delete_empty_folder_removes_it(ctx, principals):
    docs = ctx.docs
    docs.create_folder(principals["editor"], "temp")
    docs.delete_folder(principals["editor"], "temp")
    assert "temp" not in [f["name"] for f in docs.tree()["folders"]]


# -- toggle_task ------------------------------------------------------------
def test_toggle_task_flips_checkbox(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "todo.md", "- [ ] buy milk\n- [x] done\n")
    docs.toggle_task(principals["editor"], "todo.md", line=1)
    body = docs.get("todo.md")["content"]
    assert "- [x] buy milk" in body
    docs.toggle_task(principals["editor"], "todo.md", line=2)
    assert "- [ ] done" in docs.get("todo.md")["content"]


def test_toggle_task_rejects_non_task_line(ctx, principals):
    ctx.docs.create(principals["editor"], "plain.md", "just text\n")
    with pytest.raises(ValidationError):
        ctx.docs.toggle_task(principals["editor"], "plain.md", line=1)


def test_toggle_task_by_index_targets_nth_checkbox(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "t.md", "intro\n- [ ] a\ntext\n- [ ] b\n")
    # index 1 = the SECOND checkbox in document order ("b"), regardless of line gaps.
    docs.toggle_task(principals["editor"], "t.md", index=1)
    body = docs.get("t.md")["content"]
    assert "- [ ] a" in body and "- [x] b" in body


# -- API routes -------------------------------------------------------------
def test_api_create_folder_and_tree(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post("/api/folders", data={"path": "Inbox", "csrf_token": tok})
    assert r.status_code == 200 and r.json()["ok"] is True
    tree = client.get("/api/tree").json()
    assert tree["ok"] and any(f["name"] == "Inbox" for f in tree["tree"]["folders"])


def test_api_create_folder_forbidden_for_viewer(client):
    login(client, "bob")  # viewer
    tok = _token(client, "/new")
    r = client.post("/api/folders", data={"path": "x", "csrf_token": tok})
    assert r.status_code == 403


def test_api_folder_delete_route(client):
    login(client, "admin")
    tok = _token(client, "/new")
    client.post("/api/folders", data={"path": "scratch", "csrf_token": tok})
    r = client.post("/api/folders/scratch/delete", data={"csrf_token": tok})
    assert r.status_code == 200 and r.json()["deleted"] is True


def test_api_doc_move_route_updates_path(client):
    login(client, "admin")
    client.post("/new", data={"path": "old.md", "content": "body",
                              "csrf_token": _token(client, "/new")})
    tok = _token(client, "/doc/old.md")
    r = client.post("/api/doc/old.md/move", data={"new_path": "moved/new.md", "csrf_token": tok})
    assert r.status_code == 200 and r.json()["path"] == "moved/new.md"
    assert client.get("/doc/moved/new.md", follow_redirects=False).status_code == 200


def test_api_toggle_task_route(client):
    login(client, "admin")
    client.post("/new", data={"path": "tasks.md", "content": "- [ ] one\n- [ ] two\n",
                              "csrf_token": _token(client, "/new")})
    tok = _token(client, "/doc/tasks.md")
    r = client.post("/api/doc/tasks.md/toggle-task",
                    data={"index": 0, "base_version": 1, "csrf_token": tok})
    assert r.status_code == 200 and r.json()["ok"] is True
    # The rendered viewer now shows the first box checked.
    html = client.get("/doc/tasks.md").text
    assert 'data-ti="0" disabled checked' in html


# -- viewer surface ---------------------------------------------------------
def test_command_palette_is_hidden_by_default(client):
    # The palette overlay must start hidden. Its `display:flex` would otherwise
    # override the `hidden` attribute, so a CSS guard is required and asserted.
    login(client, "admin")
    html = client.get("/").text
    head = html.split('id="cmd-overlay"', 1)[1][:40]
    assert "hidden" in head
    css = client.get("/static/style.css").text
    assert "[hidden]" in css and "display: none" in css


def test_view_page_shows_outline_panel_and_statusbar(client):
    login(client, "admin")
    client.post("/new", data={"path": "doc.md", "content": "# Title\n\ntext here\n\n## Section\n",
                              "csrf_token": _token(client, "/new")})
    html = client.get("/doc/doc.md").text
    assert 'id="outline"' in html and "/static/outline.js" in html
    assert html.index('id="outline"') < html.index("/static/outline.js")
    js = client.get("/static/outline.js").text
    assert "location.hash" in js
    assert 'behavior: "auto"' in js
    assert "MutationObserver" in js
    assert "단어" in html  # status bar word count
    assert 'id="doc-rendered"' in html
