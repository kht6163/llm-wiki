"""Signed public read-only share links for a single document."""
from __future__ import annotations

from starlette.testclient import TestClient

from llm_wiki.services import share as share_svc
from llm_wiki.services.errors import ValidationError
from llm_wiki.web import create_web_app


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


def test_public_share_route_renders_without_login(ctx, principals):
    ctx.docs.create(principals["editor"], "pub.md", "# Pub\n\nbody text")
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "pub.md")
    client = TestClient(create_web_app(ctx))
    r = client.get(f"/share/{token}")
    assert r.status_code == 200
    assert "body text" in r.text or "Pub" in r.text


def test_share_missing_doc_is_404(ctx):
    secret = ctx.settings.session_secret or "test-secret"
    token = share_svc.mint_share_token(secret, "nope.md")
    client = TestClient(create_web_app(ctx))
    r = client.get(f"/share/{token}")
    assert r.status_code == 404
