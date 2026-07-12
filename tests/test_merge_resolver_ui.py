import json
import re

from starlette.testclient import TestClient

from llm_wiki.web import create_web_app


def _token(client: TestClient, path: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
    client.post(
        "/login",
        data={
            "username": "admin",
            "password": "secret12",
            "csrf_token": _token(client, "/login"),
        },
    )


def _conflict(
    client: TestClient, path: str, mine: str, base_version: int = 1, title: str = ""
):
    return client.post(
        f"/doc/{path}/edit",
        data={
            "content": mine,
            "title": title,
            "base_version": str(base_version),
            "csrf_token": _token(client, f"/doc/{path}/edit"),
        },
    )


def _payload(body: str) -> dict:
    match = re.search(
        r'<script id="merge-payload" type="application/json">(.*?)</script>', body, re.S
    )
    assert match
    return json.loads(match.group(1))


def test_conflict_template_renders_escaped_accessible_resolver_payload(ctx, principals):
    client = TestClient(create_web_app(ctx))
    _login(client)
    base = "same\nrepeat\nend\n"
    mine = 'same\n<script data-x="mine">&</script>\nend\n'
    current = "same\n<strong>current</strong>\nend\n"
    ctx.docs.create(principals["admin"], "resolver.md", base)
    ctx.docs.update(principals["admin"], "resolver.md", 1, current)

    response = _conflict(client, "resolver.md", mine)

    assert response.status_code == 409
    payload = _payload(response.text)
    assert payload["mine"] == mine
    assert payload["current"] == current
    assert payload["current_version"] == 2
    assert "<script data-x" not in response.text
    assert "\\u003cscript data-x=\\\"mine\\\"\\u003e\\u0026\\u003c/script\\u003e" in response.text
    assert '<section id="merge-resolver"' in response.text
    assert '<p id="merge-progress" role="status" aria-live="polite" aria-atomic="true">' in response.text
    assert '<p id="merge-error" role="alert" hidden>' in response.text
    assert '<fieldset class="merge-conflict" data-conflict-index="0"' in response.text
    assert 'data-resolution="mine"' in response.text
    assert 'data-resolution="current"' in response.text
    assert 'data-resolution="manual"' in response.text
    assert 'class="merge-base"' in response.text
    assert 'value="base"' not in response.text
    assert 'aria-describedby="merge-save-help merge-error"' in response.text
    assert response.text.index("editor.js") < response.text.index("merge.js")


def test_conflict_free_preview_requires_explicit_proposal_application(ctx, principals):
    client = TestClient(create_web_app(ctx))
    _login(client)
    ctx.docs.create(principals["admin"], "proposal-ui.md", "one\ntwo\nthree\n")
    ctx.docs.update(principals["admin"], "proposal-ui.md", 1, "one\ntwo\nTHREE\n")

    response = _conflict(client, "proposal-ui.md", "ONE\ntwo\nthree\n")

    assert response.status_code == 409
    assert _payload(response.text)["merged"] == "ONE\ntwo\nTHREE\n"
    assert 'data-merge-state="proposal"' in response.text
    assert '<button type="button" id="apply-merge-proposal"' in response.text
    assert "자동 병합 제안 적용" in response.text


def test_manual_only_preview_keeps_original_draft_without_fake_hunks(ctx, principals):
    client = TestClient(create_web_app(ctx))
    _login(client)
    ctx.docs.create(principals["admin"], "manual-ui.md", "base")
    ctx.docs.update(principals["admin"], "manual-ui.md", 1, "current")
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM revisions WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?) "
            "AND version=1",
            ("manual-ui.md",),
        )

    response = _conflict(client, "manual-ui.md", "original mine")

    assert response.status_code == 409
    assert _payload(response.text)["mine"] == "original mine"
    assert 'data-merge-state="manual-only"' in response.text
    assert 'data-conflict-index=' not in response.text
    assert 'id="load-current"' in response.text
    assert ">original mine</textarea>" in response.text


def test_ambiguous_title_conflict_requires_an_accessible_explicit_choice(ctx, principals):
    client = TestClient(create_web_app(ctx))
    _login(client)
    ctx.docs.create(principals["admin"], "title-ui.md", "base", title="Base <title> 😀")
    ctx.docs.update(principals["admin"], "title-ui.md", 1, "current", title="Current & title")

    response = _conflict(client, "title-ui.md", "mine", title="Mine <script> title")

    assert response.status_code == 409
    payload = _payload(response.text)
    assert payload["base_title"] == "Base <title> 😀"
    assert payload["mine_title"] == "Mine <script> title"
    assert payload["current_title"] == "Current & title"
    assert payload["merged_title"] is None
    assert payload["title_conflict"] is True
    assert '<fieldset id="merge-title-conflict"' in response.text
    assert 'data-title-resolution="mine"' in response.text
    assert 'data-title-resolution="current"' in response.text
    assert 'data-title-resolution="manual"' in response.text
    assert 'id="merge-title-manual"' in response.text
    assert "Mine &lt;script&gt; title" in response.text
    assert "Current &amp; title" in response.text


def test_concurrent_title_is_preserved_when_stale_editor_only_changes_body(ctx, principals):
    client = TestClient(create_web_app(ctx))
    _login(client)
    ctx.docs.create(principals["admin"], "body-only.md", "base", title="Base")
    ctx.docs.update(principals["admin"], "body-only.md", 1, "base", title="Current")

    response = _conflict(client, "body-only.md", "mine body", title="Base")

    assert response.status_code == 409
    assert _payload(response.text)["merged_title"] == "Current"
    assert 'value="Current"' in response.text
    assert 'id="merge-title-conflict"' not in response.text
