"""Web-route tests via Starlette's TestClient: auth redirects, RBAC, CSRF, the
409 conflict path, revision restore, raw download, tags, and security headers.
Authorization lives inline in each handler, so these route-level tests are the
only thing that catches a missing guard."""
import re
from urllib.parse import quote

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


def test_raw_download_unicode_content_disposition(client):
    # A non-ASCII (Korean) filename must use RFC 5987 filename* and never emit raw
    # CR/LF into the header value (header-injection / broken-download hardening).
    login(client, "alice")
    create_doc(client, "노트.md", "# 노트\n\n본문")
    r = client.get("/doc/" + quote("노트.md") + "/raw")
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd
    assert "\r" not in cd and "\n" not in cd


def test_home_empty_state_for_fresh_vault(client):
    # A fresh vault (principals make users, no docs) renders the empty state with a
    # create CTA for a writer — not a bare "문서가 없습니다" line.
    login(client, "admin")
    body = client.get("/").text
    assert "empty-state" in body
    assert "아직 문서가 없습니다" in body
    assert "새 문서 만들기" in body


def test_search_no_results_shows_empty_state(client):
    login(client, "admin")
    body = client.get("/search?q=절대없는검색어zzqx").text
    assert "empty-state" in body
    assert "검색 결과가 없습니다" in body


def test_view_renders_frontmatter_as_properties_panel(client):
    # Frontmatter must not leak as a setext heading; extra keys (not title/tags)
    # surface as the monospace Properties panel instead.
    login(client, "alice")
    create_doc(client, "props.md",
               "---\ntitle: 속성 문서\ntags: [x]\nstatus: draft\naliases: [별명]\n---\n\n# 본문\n\n내용\n")
    body = client.get("/doc/props.md").text
    assert "status: draft" not in body          # raw frontmatter line is gone
    assert 'class="doc-props"' in body           # surfaced as a panel
    assert "draft" in body and "별명" in body     # the non-title/tags values show


def test_settings_empty_state_has_no_table_chrome(client):
    # With no keys, the calm standalone empty-state shows — not a column-header row
    # boxed in a bordered table cell.
    login(client, "alice")
    body = client.get("/settings").text
    assert "발급된 API 키가 없습니다" in body
    assert "empty-state" in body
    assert "revtable" not in body


def test_login_screen_shows_wordmark(client):
    body = client.get("/login").text
    assert 'class="login-brand"' in body
    assert "llm-wiki" in body


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


def test_admin_page_anonymous_redirects_to_login(client):
    # require_admin -> require_user raises _AuthRequired -> page redirect (not 403).
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_api_get_unauthenticated_is_401_json(client):
    # The centralized _AuthRequired handler returns JSON 401 for /api/* (not a redirect).
    r = client.get("/api/tree")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_api_wiki_error_returns_structured_envelope(client):
    # A service error on an /api route reaches the global WikiError handler as the
    # structured envelope at the right HTTP status (route no longer catches it inline).
    login(client, "alice")
    r = client.get("/api/doc/does-not-exist.md/preview")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_page_wiki_error_renders_error_page(client):
    # The same error on a page route renders the HTML error template at that status.
    login(client, "alice")
    r = client.get("/doc/does-not-exist.md/history")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"]
    client.cookies.clear()
    login(client, "admin")
    assert client.get("/admin/users").status_code == 200


def test_view_lazy_loads_related_panel(client):
    login(client, "admin")
    create_doc(client, "ml.md", "# ML\n\nneural networks and deep learning trained on data")
    create_doc(client, "ai.md", "# AI\n\ndeep learning and neural networks power modern AI")
    # The view ships only the lazy-load placeholder + script — the "관련 문서" list is no
    # longer server-rendered (it ran several KNN scans on the synchronous view path).
    r = client.get("/doc/ml.md")
    assert r.status_code == 200
    assert 'id="rp-related"' in r.text and "related.js" in r.text
    assert "관련 문서" not in r.text  # heading is injected client-side now
    # Related docs come from the dedicated endpoint instead.
    rel = client.get("/api/doc/ml.md/related")
    assert rel.status_code == 200
    body = rel.json()
    assert body["ok"] is True
    assert "ai.md" in [x["path"] for x in body["related"]]  # the similar note surfaces


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
    # C3: the conflict page offers a one-click "load current version" recovery affordance.
    assert 'id="load-current"' in r.text and 'id="server-current"' in r.text


def test_new_post_invalid_path_stays_on_form(client):
    # C2: an invalid path is a field error -> re-render the editor with content preserved,
    # not a bounce to the generic error page (which would lose the draft).
    login(client, "admin")
    r = client.post(
        "/new",
        data={"path": "../escape.md", "content": "my draft content", "title": "",
              "csrf_token": _token(client, "/new")},
    )
    assert r.status_code == 400
    assert "잘못된 경로" in r.text                 # inline field error
    assert 'id="md-editor-mount"' in r.text       # still the editor form, not error.html
    assert "my draft content" in r.text           # the draft is preserved


def test_editor_has_unsaved_changes_guard(client):
    # C1: editor.js guards Cancel / navigation when there are unsaved edits.
    js = client.get("/static/editor.js").text
    assert "beforeunload" in js
    assert "cancel-edit" in js
    assert "load-current" in js                   # C3 recovery wiring present too


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


def test_diff_page_offers_revert_to_older_version(client):
    login(client, "admin")
    create_doc(client, "d.md", "alpha")
    client.post(
        "/doc/d.md/edit",
        data={"content": "beta", "base_version": "1", "csrf_token": _token(client, "/doc/d.md/edit")},
    )
    html = client.get("/doc/d.md/diff?from=1&to=2").text
    # The diff offers a one-click revert to the older (from) version.
    assert 'action="/doc/d.md/rev/1/restore"' in html
    assert "되돌리기" in html


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


def test_broken_links_page_lists_dangling_links(client):
    login(client, "admin")
    create_doc(client, "src.md", "see [[ghosttarget]] for details")
    r = client.get("/broken-links")
    assert r.status_code == 200
    assert "ghosttarget" in r.text and "src.md" in r.text


def test_broken_links_requires_auth(client):
    r = client.get("/broken-links", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_history_shows_authoring_surface_badge(client):
    login(client, "admin")
    create_doc(client, "h.md", "# H\n\nbody")  # authored over the web == 사람
    html = client.get("/doc/h.md/history").text
    assert "via-badge" in html and "사람" in html


def test_activity_page_visible_to_editor_not_viewer(client):
    login(client, "alice")  # editor
    create_doc(client, "act.md", "# A\n\nbody")
    r = client.get("/activity")
    assert r.status_code == 200
    assert "활동" in r.text and "문서 생성" in r.text  # Korean action label, not raw action
    # A viewer has no edit footprint to audit and is refused.
    client.get("/logout")
    login(client, "bob")  # viewer
    assert client.get("/activity").status_code == 403


def test_activity_requires_auth(client):
    r = client.get("/activity", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_readyz_reports_index_health(client):
    login(client, "admin")
    create_doc(client, "rz.md", "links to [[nope]]")
    d = client.get("/readyz").json()
    assert d["ready"] is True and d["model_loaded"] is True
    assert d["embedding_model"]
    assert d["broken_links"] >= 1 and d["pending_files"] == 0
    assert d["schema_version"] >= 4


def test_metrics_exposes_index_gauges(client):
    body = client.get("/metrics").text
    assert "llmwiki_vector_dirty_documents" in body
    assert "llmwiki_broken_links" in body
    assert "llmwiki_schema_version" in body


def test_api_doc_preview_returns_title_and_excerpt(client):
    login(client, "admin")
    create_doc(client, "prev.md", "# Preview Title\n\nThe quick brown fox jumps over.", title="Preview Title")
    r = client.get("/api/doc/prev.md/preview")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["title"] == "Preview Title"
    assert "quick brown fox" in d["excerpt"]


def test_list_page_includes_preview_hook(client):
    login(client, "admin")
    assert "/static/preview.js" in client.get("/").text


def test_edit_page_uses_vendored_md_editor(client):
    # The editor is md-editor-rt, shipped as a vendored offline bundle. The page
    # must load both bundle assets, keep the #editor textarea (the form field) and
    # the #md-editor-mount target, and the assets must actually serve.
    login(client, "admin")
    create_doc(client, "e.md", "# T\n\nbody")
    html = client.get("/doc/e.md/edit").text
    assert "/static/vendor/md-editor.bundle.js" in html
    assert "/static/vendor/md-editor.bundle.css" in html
    assert 'id="editor"' in html and 'id="md-editor-mount"' in html and "/static/editor.js" in html
    assert client.get("/static/vendor/md-editor.bundle.js").status_code == 200
    assert client.get("/static/vendor/md-editor.bundle.css").status_code == 200


def test_nested_path_edit_form_action_is_not_doubled(client):
    # Regression: base.html highlights the active nav route. It must NOT bind that
    # to a template variable named `path`, because child templates (edit/history/
    # diff) receive a document `path` in their context and build URLs from it. A
    # name collision shadowed it with request.url.path, producing a doubled action
    # like `/doc//doc/foo/bar.md/edit/edit` -> 404 / "No document at this path."
    login(client, "admin")
    create_doc(client, "folder/sub/note.md", "# T\n\nbody")
    html = client.get("/doc/folder/sub/note.md/edit").text
    assert 'action="/doc/folder/sub/note.md/edit"' in html
    assert 'data-path="folder/sub/note.md"' in html
    assert "/doc//doc/" not in html  # no path doubling anywhere on the page

    # And the round-trip actually saves (the symptom the user hit).
    csrf = _token(client, "/doc/folder/sub/note.md/edit")
    r = client.post(
        "/doc/folder/sub/note.md/edit",
        data={"content": "# T\n\n**bold** edit\n", "base_version": "1", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/doc/folder/sub/note.md"
    # history links for a nested path must not double either
    hist = client.get("/doc/folder/sub/note.md/history").text
    assert "/doc//doc/" not in hist


def test_view_page_loads_code_highlighter(client):
    # The reading view colours code with the same vendored highlight.js as the
    # editor, so the bundle + scoped theme must be referenced and actually serve.
    login(client, "admin")
    create_doc(client, "hl.md", "# T\n\n```python\nx = 1\n```\n")
    html = client.get("/doc/hl.md").text
    assert "/static/vendor/hljs.bundle.js" in html
    assert "/static/vendor/hljs-theme.css" in html
    assert client.get("/static/vendor/hljs.bundle.js").status_code == 200
    assert client.get("/static/vendor/hljs-theme.css").status_code == 200


def test_timestamps_render_as_localizable_time_elements(client):
    # Stored timestamps are UTC ISO ("…Z"); the `dt` filter wraps them in a
    # <time> element (datetime=ISO, text=cleaned UTC fallback) that datetime.js
    # localizes client-side. Verify the markup + that the localizer is loaded.
    import re

    login(client, "admin")
    create_doc(client, "ts.md", "# T\n\nbody")
    html = client.get("/doc/ts.md").text
    m = re.search(
        r'<time class="dt" datetime="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)">'
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})</time>",
        html,
    )
    assert m, "view page should render updated_at as a localizable <time> element"
    # the fallback text is the same instant with T/Z stripped (no timezone shift server-side)
    assert m.group(2) == m.group(1)[:10] + " " + m.group(1)[11:19]
    assert "/static/datetime.js" in html
    assert client.get("/static/datetime.js").status_code == 200


def test_security_headers_present(client):
    login(client, "admin")
    h = client.get("/").headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    csp = h.get("content-security-policy", "")
    assert "content-security-policy" in h
    # Images allowed over HTTPS / data: URIs only — no plain http: (mixed content).
    assert "img-src 'self' data: https:" in csp
    assert "http:" not in csp


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


def test_preview_api_is_csrf_exempt(client):
    # The editor renders the preview by POSTing to /api/preview on every toggle.
    # It changes no state, so it's exempt from CSRF — must work with no token and
    # even when the Origin doesn't match (e.g. behind a proxy / different host),
    # otherwise the preview fails silently.
    login(client, "admin")
    r = client.post(
        "/api/preview",
        data={"content": "# Hi\n\n[[other]]", "path": "x.md"},  # no csrf_token
        headers={"origin": "http://proxy.example"},             # cross-origin
    )
    assert r.status_code == 200 and r.json()["ok"]
    # Still gated on auth: a logged-out client gets 401, not a render.
    client.get("/logout")
    assert client.post("/api/preview", data={"content": "x"}).status_code == 401


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


def test_upload_rejects_oversized_without_buffering(client, monkeypatch):
    # The size cap must be enforced during the streamed read, not after buffering the
    # whole body. Shrink the cap so a tiny body trips it.
    import llm_wiki.web.app as app_mod
    monkeypatch.setattr(app_mod, "ATTACH_MAX_BYTES", 16)
    login(client, "admin")
    tok = _token(client, "/new")
    big = b"\x89PNG\r\n\x1a\n" + bytes(64)  # > 16 bytes
    r = client.post("/api/upload", files={"file": ("big.png", big, "image/png")},
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
    # Scope to the main content: the persistent file tree (left sidebar) lists every
    # document, so assert against the search-results region only.
    results = r.text.split("<main>")[-1]
    assert "notes/a.md" in results and "other/b.md" not in results


def test_traversal_path_is_400_not_500(client):
    login(client, "admin")
    tok = _token(client, "/new")
    r = client.post("/new", data={"path": "../escape.md", "content": "x", "csrf_token": tok})
    assert r.status_code == 400


def test_home_pagination(client):
    login(client, "admin")
    # Create more than one page worth (per_page=50) to exercise paging.
    for i in range(55):
        create_doc(client, f"p{i:03}.md", f"doc number {i}")
    # Scope to the main list region: the left file tree lists every doc regardless
    # of which page is shown, so pagination must be asserted on <main> only.
    first = client.get("/?sort=path").text.split("<main>")[-1]
    assert "p000.md" in first and "/ 55" in first  # pager total shown
    # Page 2 holds the tail; page 1 doesn't.
    second = client.get("/?sort=path&page=2").text.split("<main>")[-1]
    assert "p054.md" in second and "p054.md" not in first
    assert "p000.md" not in second


def test_shell_renders_navigation(client):
    # The Obsidian-style app shell: ribbon + collapsible left sidebar with a file
    # tree, and a sidebar-toggle control (the responsive navigation surface).
    login(client, "admin")
    html = client.get("/").text
    assert 'class="ribbon"' in html
    assert 'class="sidebar-left"' in html and 'id="file-tree"' in html
    assert 'data-action="toggle-left"' in html
    assert "/static/shell.js" in html and "/static/palette.js" in html


# -- realtime: WebSocket live change reflection -----------------------------
def test_ws_reflects_document_change(client, ctx, principals):
    login(client, "admin")
    create_doc(client, "live.md", "# Live\n\noriginal")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "ready"  # subscribed; no event can be missed now
        # An out-of-band edit (another user / MCP) must be pushed to the viewer.
        ctx.docs.update(principals["editor"], "live.md", 1, "# Live\n\nchanged")
        ev = ws.receive_json()
    assert ev["type"] == "doc_changed" and ev["path"] == "live.md"
    assert ev["op"] == "update" and ev["version"] == 2


def test_ws_receives_create_event(client, ctx, principals):
    login(client, "admin")
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "ready"
        ctx.docs.create(principals["editor"], "fresh.md", "# Fresh\n\nbody")
        ev = ws.receive_json()
    assert ev["type"] == "doc_changed" and ev["op"] == "create" and ev["path"] == "fresh.md"


def test_list_page_has_realtime_hook(client):
    login(client, "admin")
    html = client.get("/").text
    assert 'data-mode="list"' in html and "/static/realtime.js" in html


def test_ws_rejects_unauthenticated(client):
    # No session cookie -> the handshake is refused (closed before accept).
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_api_doc_rendered_returns_html_and_version(client):
    login(client, "admin")
    create_doc(client, "rd.md", "# RD\n\n**bold** body")
    r = client.get("/api/doc/rd.md/rendered")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["version"] == 1
    assert "<strong>bold</strong>" in body["html"]


def test_login_lockout_is_ip_scoped_not_per_username(ctx, principals):
    # An attacker spamming bad passwords for 'admin' from their own IP must NOT lock
    # the real admin out from a different, clean IP — the 429 is keyed by client IP,
    # so a per-username lockout DoS is not possible.
    app = create_web_app(ctx)
    attacker = TestClient(app, client=("9.9.9.9", 1))
    victim = TestClient(app, client=("1.1.1.1", 2))
    for _ in range(12):  # well past the limiter threshold
        attacker.post("/login", data={"username": "admin", "password": "wrong",
                                       "csrf_token": _token(attacker, "/login")})
    r = login(victim, "admin")  # correct password from a clean IP
    assert r.status_code == 200 and "llm-wiki" in r.text


def test_login_lockout_collapses_ipv4_mapped_ipv6(ctx, principals):
    # The same caller arriving as plain IPv4 and as IPv4-mapped IPv6 must share one
    # limiter bucket, so it can't reset the counter by switching address family.
    app = create_web_app(ctx)
    mapped = TestClient(app, client=("::ffff:9.9.9.9", 1))
    plain = TestClient(app, client=("9.9.9.9", 2))
    for _ in range(9):  # exhaust the limiter via the IPv6-mapped form
        mapped.post("/login", data={"username": "admin", "password": "wrong",
                                    "csrf_token": _token(mapped, "/login")})
    # Arriving as plain IPv4 now lands in the SAME bucket -> blocked.
    r = plain.post(
        "/login",
        data={"username": "admin", "password": "wrong", "csrf_token": _token(plain, "/login")},
        follow_redirects=False,
    )
    assert r.status_code == 429


def test_login_throttle_block_is_audited_once(ctx, principals):
    # When a brute-force first crosses the throttle threshold, exactly one login_blocked
    # audit row is written (subsequent attempts short-circuit at the rate gate without
    # re-auditing), so the block surfaces in the admin feed without write-amplifying.
    from llm_wiki.services import audit
    app = create_web_app(ctx)
    attacker = TestClient(app, client=("7.7.7.7", 1))
    for _ in range(15):  # well past the limiter threshold
        attacker.post("/login", data={"username": "admin", "password": "wrong",
                                      "csrf_token": _token(attacker, "/login")})
    blocks = audit.recent(ctx.db, action="login_blocked")
    assert len(blocks) == 1
    assert blocks[0]["via"] == "web" and blocks[0]["outcome"] == "blocked"
    assert "7.7.7.7" in (blocks[0]["detail"] or "")


def test_realtime_banner_escapes_untrusted_values(client):
    # realtime.js builds change banners with innerHTML; the two attacker-influenced
    # values it interpolates — the move-target path (paths allow '<'/'>') and the
    # change author username (unrestricted) — must be HTML-escaped, or a crafted path
    # / username becomes stored DOM XSS in every viewer's browser.
    r = client.get("/static/realtime.js")
    assert r.status_code == 200
    js = r.text
    assert "function esc(" in js          # escape helper present
    assert "esc(to)" in js                # move banner escapes the document path
    assert "esc(whoVia(ev))" in js        # edit banner escapes the username


def test_tree_context_menu_is_keyboard_accessible(client):
    # C1: rename/delete must be reachable without a mouse (WCAG 2.1.1).
    js = client.get("/static/shell.js").text
    assert "ContextMenu" in js and "F10" in js     # keyboard trigger
    assert "openTreeMenuFor" in js                 # shared open path for mouse + keyboard


def test_sidebar_resizer_supports_touch(client):
    # C3: pointer events cover mouse + touch + pen (was mouse-only).
    js = client.get("/static/shell.js").text
    assert "pointerdown" in js and "setPointerCapture" in js
    assert "mousedown" not in js                    # fully migrated off mouse-only events


def test_command_palette_restores_focus_on_close(client):
    # C2: closing the palette returns focus to where it was (WCAG 2.4.3).
    js = client.get("/static/palette.js").text
    assert "previousFocus" in js


def test_user_mod_failure_is_audited(client, ctx):
    # D1: a rejected user-modification (demoting the only admin) must still leave an
    # audit row with outcome="error" — the security trail can't have silent gaps.
    from llm_wiki.services import audit
    login(client, "admin")
    with ctx.db.reader() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    r = client.post(f"/admin/users/{uid}/role",
                    data={"role": "editor", "csrf_token": _token(client, "/admin/users")},
                    follow_redirects=False)
    assert r.status_code == 303
    rows = audit.recent(ctx.db, action="role_change")
    assert any(x["outcome"] == "error" for x in rows)
