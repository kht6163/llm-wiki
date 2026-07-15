"""OIDC login/callback web routes with mocked discovery and token exchange."""
from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services import oidc as oidc_svc
from llm_wiki.services.auth import create_user
from llm_wiki.web import create_web_app

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest.fixture
def oidc_ctx(tmp_path):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_model=TEST_MODEL,
        gui_port=8088,
        mcp_port=8089,
        session_secret="test-secret",
        oidc_enabled=True,
        oidc_issuer="https://idp.example",
        oidc_client_id="llm-wiki",
        oidc_client_secret="secret",
        oidc_redirect_uri="http://127.0.0.1:8080/auth/oidc/callback",
        oidc_default_role="viewer",
        oidc_auto_provision=True,
    )
    ctx = build_context(settings, full=True)
    create_user(ctx.db, "admin", "secret12", "admin")
    return ctx


@pytest.fixture
def oidc_client(oidc_ctx):
    return TestClient(create_web_app(oidc_ctx))


@pytest.fixture
def rsa_material():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem


def _patch_discovery(monkeypatch):
    doc = {
        "issuer": "https://idp.example",
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
    }
    monkeypatch.setattr(oidc_svc, "fetch_discovery", lambda *_a, **_k: doc)
    return doc


def test_login_page_shows_sso_when_enabled(oidc_client):
    r = oidc_client.get("/login")
    assert r.status_code == 200
    assert "SSO로 로그인" in r.text
    assert 'href="/auth/oidc/login"' in r.text


def test_login_page_hides_sso_when_disabled(ctx, principals):
    assert ctx.settings.oidc_enabled is False
    client = TestClient(create_web_app(ctx))
    r = client.get("/login")
    assert r.status_code == 200
    assert "SSO로 로그인" not in r.text


def test_oidc_login_redirects_to_idp(oidc_client, monkeypatch):
    _patch_discovery(monkeypatch)
    r = oidc_client.get("/auth/oidc/login?next=/search", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://idp.example/authorize?")
    qs = parse_qs(urlparse(loc).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["llm-wiki"]
    assert qs["code_challenge_method"] == ["S256"]
    assert "state" in qs and "nonce" in qs and "code_challenge" in qs


def test_oidc_login_disabled_is_404(ctx, principals):
    client = TestClient(create_web_app(ctx))
    r = client.get("/auth/oidc/login")
    assert r.status_code == 404
    assert "not enabled" in r.text


def test_oidc_callback_provisions_and_sets_session(
    oidc_ctx, oidc_client, monkeypatch, rsa_material
):
    _patch_discovery(monkeypatch)
    # Start login to plant state/nonce/verifier in the session cookie.
    start = oidc_client.get("/auth/oidc/login?next=/tags", follow_redirects=False)
    qs = parse_qs(urlparse(start.headers["location"]).query)
    state = qs["state"][0]
    nonce = qs["nonce"][0]

    now = int(time.time())
    id_token = jwt.encode(
        {
            "iss": "https://idp.example",
            "sub": "oidc-sub-1",
            "aud": "llm-wiki",
            "exp": now + 300,
            "iat": now,
            "nonce": nonce,
            "email": "sso@example.com",
            "email_verified": True,
            "preferred_username": "sso_user",
        },
        rsa_material,
        algorithm="RS256",
    )

    def fake_exchange(**kwargs):
        assert kwargs["code"] == "auth-code"
        assert kwargs["code_verifier"]
        return {"id_token": id_token, "token_type": "Bearer"}

    monkeypatch.setattr(oidc_svc, "exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(
        oidc_svc,
        "verify_id_token",
        lambda token, **kw: oidc_svc.IdTokenClaims(
            issuer="https://idp.example",
            subject="oidc-sub-1",
            email="sso@example.com",
            email_verified=True,
            preferred_username="sso_user",
            raw={"sub": "oidc-sub-1"},
        ),
    )

    r = oidc_client.get(
        f"/auth/oidc/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/tags"
    # Session cookie should resolve to the provisioned user.
    # Re-hit a protected page.
    home = oidc_client.get("/", follow_redirects=False)
    assert home.status_code != 303 or home.headers.get("location") != "/login"
    with oidc_ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT username, password_hash, role FROM users WHERE oidc_sub=?",
            ("oidc-sub-1",),
        ).fetchone()
    assert row is not None
    assert row["username"] == "sso_user"
    assert row["password_hash"] is None
    assert row["role"] == "viewer"


def test_oidc_callback_rejects_bad_state(oidc_client, monkeypatch):
    _patch_discovery(monkeypatch)
    oidc_client.get("/auth/oidc/login", follow_redirects=False)
    r = oidc_client.get(
        "/auth/oidc/callback?code=x&state=wrong",
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid SSO state" in r.text


def test_safe_return_path_rejects_open_redirect():
    from llm_wiki.web.routes.auth_pages import _safe_return_path

    assert _safe_return_path("/search") == "/search"
    assert _safe_return_path("//evil.example") == "/"
    assert _safe_return_path("https://evil.example") == "/"
    assert _safe_return_path(None) == "/"
    assert _safe_return_path("/doc/a.md") == "/doc/a.md"
