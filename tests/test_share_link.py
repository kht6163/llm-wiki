"""Signed public read-only share links for a single document."""
from __future__ import annotations

import re
import sqlite3
import threading

import pytest
from starlette.testclient import TestClient

from llm_wiki.db import Database
from llm_wiki.services import share as share_svc
from llm_wiki.services.errors import ForbiddenError, NotFoundError, ValidationError
from llm_wiki.web import create_web_app


def _login(client: TestClient, username: str) -> None:
    login_page = client.get("/login").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page)
    assert token
    response = client.post(
        "/login",
        data={"username": username, "password": "secret12", "csrf_token": token.group(1)},
    )
    assert response.status_code == 200


def test_mint_and_verify_share_token(ctx, principals):
    d = ctx.docs.create(principals["editor"], "shared.md", "# Shared\n\nhello public")
    assert d["path"] == "shared.md"
    token = share_svc.mint_share_token(ctx.settings.session_secret or "test-secret", "shared.md")
    path = share_svc.verify_share_token(ctx.settings.session_secret or "test-secret", token)
    assert path == "shared.md"


def test_share_token_rejects_tamper(ctx):
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "a.md")
    bad = token[:-2] + ("xx" if not token.endswith("xx") else "yy")
    try:
        share_svc.verify_share_token(secret, bad)
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


def test_revocable_share_ledger_lists_uses_and_revokes(ctx, principals):
    editor = principals["editor"]
    ctx.docs.create(editor, "revocable.md", "# Revocable", embed=False)
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(
        secret, "revocable.md", db=ctx.db, principal=editor
    )

    links = share_svc.list_share_links(ctx.db, editor, "revocable.md")
    assert len(links) == 1 and links[0]["revoked_at"] is None
    assert share_svc.verify_share_token(secret, token, db=ctx.db) == "revocable.md"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT last_used_at FROM share_links WHERE id=?", (links[0]["id"],)
        ).fetchone()[0]

    revoked = share_svc.revoke_share_link(ctx.db, editor, links[0]["id"])
    assert revoked["path"] == "revocable.md" and revoked["revoked_at"]
    with pytest.raises(ValidationError, match="revoked"):
        share_svc.verify_share_token(secret, token, db=ctx.db)


def test_share_verification_throttles_and_best_effort_usage_write(
    ctx, principals, monkeypatch
):
    editor = principals["editor"]
    ctx.docs.create(editor, "sparse.md", "# Sparse", embed=False)
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "sparse.md", db=ctx.db, principal=editor)

    assert share_svc.verify_share_token(secret, token, db=ctx.db) == "sparse.md"
    with ctx.db.reader() as conn:
        first_used = conn.execute(
            "SELECT last_used_at FROM share_links WHERE token_hash=?",
            (share_svc._token_hash(token),),
        ).fetchone()[0]
    real_try_write = ctx.db.try_write

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("fresh usage metadata must not acquire the writer lock")

    monkeypatch.setattr(ctx.db, "try_write", unexpected_write)
    assert share_svc.verify_share_token(secret, token, db=ctx.db) == "sparse.md"

    monkeypatch.setattr(ctx.db, "try_write", real_try_write)
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE share_links SET last_used_at='2000-01-01T00:00:00Z' "
            "WHERE token_hash=?",
            (share_svc._token_hash(token),),
        )

    def busy_write(*_args, **_kwargs):
        raise sqlite3.OperationalError("busy")

    monkeypatch.setattr(ctx.db, "try_write", busy_write)
    assert share_svc.verify_share_token(secret, token, db=ctx.db) == "sparse.md"
    assert first_used is not None


def test_best_effort_usage_write_skips_busy_and_connection_failure(ctx, monkeypatch):
    other = Database(ctx.settings.db_path)
    started = threading.Event()
    release = threading.Event()

    def hold_external_writer():
        with other.writer():
            started.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=hold_external_writer)
    thread.start()
    assert started.wait(timeout=5)
    try:
        assert ctx.db.try_write("UPDATE share_links SET last_used_at=NULL") is False
    finally:
        release.set()
        thread.join(timeout=5)
        other.close()
    assert not thread.is_alive()

    def unavailable_connection(*_args, **_kwargs):
        raise sqlite3.OperationalError("unavailable")

    monkeypatch.setattr(sqlite3, "connect", unavailable_connection)
    assert ctx.db.try_write("UPDATE share_links SET last_used_at=NULL") is False


def test_public_share_route_renders_without_login(ctx, principals):
    ctx.docs.create(principals["editor"], "pub.md", "# Pub\n\nbody text")
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "pub.md")
    client = TestClient(create_web_app(ctx))
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "body text" in r.text or "Pub" in r.text


def test_public_share_does_not_transclude_other_document_bodies(ctx, principals):
    ctx.docs.create(principals["editor"], "secret.md", "# Secret\n\nTOP-SECRET-BODY")
    ctx.docs.create(principals["editor"], "pub.md", "# Pub\n\n![[secret]]")
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "pub.md")

    response = TestClient(create_web_app(ctx)).get(f"/share/{token}")

    assert response.status_code == 200
    assert "TOP-SECRET-BODY" not in response.text
    assert "embed-body" not in response.text


def test_document_view_shows_share_disclosure_only_to_writers(ctx, principals):
    ctx.docs.create(principals["editor"], "shared-ui.md", "# Shared UI")
    app = create_web_app(ctx)
    editor = TestClient(app)
    _login(editor, "alice")
    editor_view = editor.get("/doc/shared-ui.md").text
    assert 'id="share-toggle"' in editor_view
    assert 'id="share-panel"' in editor_view
    assert "30일 후 자동 만료" in editor_view
    assert "언제든 이 화면에서 취소할 수 있습니다" in editor_view
    assert 'id="share-links"' in editor_view
    assert "share.js" in editor_view

    viewer = TestClient(app)
    _login(viewer, "bob")
    viewer_view = viewer.get("/doc/shared-ui.md").text
    assert 'id="share-toggle"' not in viewer_view
    assert 'id="share-panel"' not in viewer_view


def test_share_missing_doc_is_404(ctx):
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "nope.md")
    client = TestClient(create_web_app(ctx))
    r = client.get(f"/share/{token}")
    assert r.status_code == 404


def test_share_service_rejects_invalid_authorization_payloads_and_targets(ctx, principals):
    secret = ctx.settings.session_secret or "test-secret"
    editor = principals["editor"]
    viewer = principals["viewer"]
    ctx.docs.create(editor, "guarded.md", "# Guarded", embed=False)

    with pytest.raises(ValidationError, match="principal"):
        share_svc.mint_share_token(secret, "guarded.md", db=ctx.db)
    with pytest.raises(ForbiddenError, match="active editor"):
        share_svc.mint_share_token(
            secret, "guarded.md", db=ctx.db, principal=viewer
        )
    with pytest.raises(NotFoundError):
        share_svc.mint_share_token(secret, "missing.md", db=ctx.db, principal=editor)
    with pytest.raises(ForbiddenError, match="active editor"):
        share_svc.list_share_links(ctx.db, viewer, "guarded.md")

    invalid_legacy = share_svc._serializer(secret, legacy=True).dumps({"wrong": "shape"})
    with pytest.raises(ValidationError, match="invalid"):
        share_svc.verify_share_token(secret, invalid_legacy)
    invalid_v2 = share_svc._serializer(secret).dumps({"wrong": "shape"})
    with pytest.raises(ValidationError, match="invalid"):
        share_svc.verify_share_token(secret, invalid_v2, db=ctx.db)
    valid_v2 = share_svc.mint_share_token(
        secret, "guarded.md", db=ctx.db, principal=editor
    )
    with pytest.raises(ValidationError, match="invalid"):
        share_svc.verify_share_token(secret, valid_v2)

    legacy = share_svc.mint_share_token(secret, "guarded.md")
    with pytest.raises(ValidationError, match="expired"):
        share_svc.verify_share_token(secret, legacy, max_age_s=-1)
    with pytest.raises(ValidationError, match="expired"):
        share_svc.verify_share_token(secret, valid_v2, db=ctx.db, max_age_s=-1)


def test_share_revoke_ownership_admin_and_idempotent_paths(ctx, principals):
    secret = ctx.settings.session_secret or "test-secret"
    editor = principals["editor"]
    admin = principals["admin"]
    ctx.docs.create(editor, "owned.md", "# Owned", embed=False)
    token = share_svc.mint_share_token(
        secret, "owned.md", db=ctx.db, principal=editor
    )
    link = share_svc.list_share_links(ctx.db, editor, "owned.md")[0]

    with pytest.raises(NotFoundError):
        share_svc.revoke_share_link(ctx.db, admin, link["id"] + 999)
    revoked = share_svc.revoke_share_link(ctx.db, admin, link["id"])
    assert revoked["revoked_at"]
    assert share_svc.revoke_share_link(ctx.db, editor, link["id"]) == revoked
    with pytest.raises(ValidationError, match="revoked"):
        share_svc.verify_share_token(secret, token, db=ctx.db)


def test_share_ledger_web_api_lists_and_revokes_link(ctx, principals):
    ctx.docs.create(principals["editor"], "api-share.md", "# API Share", embed=False)
    client = TestClient(create_web_app(ctx))
    _login(client, "alice")
    page = client.get("/doc/api-share.md").text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert csrf

    minted = client.post(
        "/api/doc/api-share.md/share",
        data={"csrf_token": csrf.group(1)},
    )
    assert minted.status_code == 200
    payload = minted.json()
    assert payload["link"]["id"]
    listed = client.get("/api/doc/api-share.md/shares")
    assert [item["id"] for item in listed.json()["links"]] == [payload["link"]["id"]]

    revoked = client.post(
        f"/api/shares/{payload['link']['id']}/revoke",
        data={"csrf_token": csrf.group(1)},
    )
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert client.get(payload["url"]).status_code == 400
