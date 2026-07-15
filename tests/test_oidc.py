"""OIDC client helpers: PKCE, discovery/JWKS (HTTP mocked), ID-token verify."""
from __future__ import annotations

import base64
import hashlib
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from llm_wiki.services import oidc
from llm_wiki.services.errors import ValidationError


@pytest.fixture
def rsa_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key


@pytest.fixture(autouse=True)
def _clear_oidc_caches():
    oidc.clear_oidc_caches()
    yield
    oidc.clear_oidc_caches()


def test_pkce_s256_challenge_matches_verifier():
    verifier, challenge = oidc.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    assert oidc.generate_state()
    assert oidc.generate_nonce()


def test_build_authorization_url_includes_pkce_and_nonce():
    url = oidc.build_authorization_url(
        authorization_endpoint="https://idp.example/authorize",
        client_id="cid",
        redirect_uri="http://127.0.0.1:8080/auth/oidc/callback",
        scope="openid profile email",
        state="st",
        nonce="nn",
        code_challenge="cc",
    )
    assert url.startswith("https://idp.example/authorize?")
    assert "response_type=code" in url
    assert "code_challenge=cc" in url
    assert "code_challenge_method=S256" in url
    assert "nonce=nn" in url
    assert "state=st" in url


def test_fetch_discovery_caches_and_validates_issuer(monkeypatch):
    calls = {"n": 0}
    doc = {
        "issuer": "https://idp.example",
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
    }

    def fake_get(url, *, timeout=15.0):
        calls["n"] += 1
        assert url.endswith("/.well-known/openid-configuration")
        return dict(doc)

    monkeypatch.setattr(oidc, "_http_get_json", fake_get)
    a = oidc.fetch_discovery("https://idp.example/")
    b = oidc.fetch_discovery("https://idp.example")
    assert a["token_endpoint"] == doc["token_endpoint"]
    assert b is a or b == a
    assert calls["n"] == 1

    bad = dict(doc, issuer="https://other.example")
    monkeypatch.setattr(oidc, "_http_get_json", lambda *a, **k: bad)
    oidc.clear_oidc_caches()
    with pytest.raises(ValidationError, match="issuer mismatch"):
        oidc.fetch_discovery("https://idp.example")


def test_verify_id_token_rs256_with_ephemeral_key(rsa_pair, monkeypatch):
    private_pem, private_key = rsa_pair
    issuer = "https://idp.example"
    client_id = "llm-wiki"
    nonce = "nonce-abc"
    now = int(time.time())
    claims = {
        "iss": issuer,
        "sub": "user-42",
        "aud": client_id,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email": "alice@Example.COM",
        "email_verified": True,
        "preferred_username": "alice",
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "k1"})

    # Feed JWKS via a monkeypatched PyJWKClient.get_signing_key_from_jwt.
    from jwt import PyJWK

    jwk = PyJWK.from_json(
        json.dumps(
            {
                "kty": "RSA",
                "kid": "k1",
                "use": "sig",
                "alg": "RS256",
                "n": _b64u_int(private_key.public_key().public_numbers().n),
                "e": _b64u_int(private_key.public_key().public_numbers().e),
            }
        )
    )

    class FakeClient:
        def get_signing_key_from_jwt(self, _token):
            return jwk

    monkeypatch.setattr(oidc, "_jwks_client", lambda _uri: FakeClient())

    verified = oidc.verify_id_token(
        token,
        issuer=issuer,
        client_id=client_id,
        jwks_uri="https://idp.example/jwks",
        nonce=nonce,
    )
    assert verified.subject == "user-42"
    assert verified.email == "alice@Example.COM"
    assert verified.email_verified is True
    assert verified.preferred_username == "alice"
    assert oidc.claim_username(verified, "preferred_username") == "alice"

    with pytest.raises(ValidationError, match="nonce"):
        oidc.verify_id_token(
            token,
            issuer=issuer,
            client_id=client_id,
            jwks_uri="https://idp.example/jwks",
            nonce="wrong",
        )


def test_verify_id_token_rejects_wrong_audience(rsa_pair, monkeypatch):
    private_pem, private_key = rsa_pair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://idp.example",
            "sub": "u",
            "aud": "other-client",
            "exp": now + 60,
            "iat": now,
            "nonce": "n",
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "k1"},
    )
    from jwt import PyJWK

    jwk = PyJWK.from_json(
        json.dumps(
            {
                "kty": "RSA",
                "kid": "k1",
                "n": _b64u_int(private_key.public_key().public_numbers().n),
                "e": _b64u_int(private_key.public_key().public_numbers().e),
            }
        )
    )

    class FakeClient:
        def get_signing_key_from_jwt(self, _token):
            return jwk

    monkeypatch.setattr(oidc, "_jwks_client", lambda _uri: FakeClient())
    with pytest.raises(ValidationError, match="invalid id_token"):
        oidc.verify_id_token(
            token,
            issuer="https://idp.example",
            client_id="llm-wiki",
            jwks_uri="https://idp.example/jwks",
            nonce="n",
        )


def test_exchange_code_for_tokens_posts_pkce(monkeypatch):
    captured: dict = {}

    def fake_post(url, fields, *, timeout=15.0):
        captured["url"] = url
        captured["fields"] = fields
        return {"id_token": "t", "access_token": "a", "token_type": "Bearer"}

    monkeypatch.setattr(oidc, "_http_post_form", fake_post)
    out = oidc.exchange_code_for_tokens(
        token_endpoint="https://idp.example/token",
        code="authcode",
        redirect_uri="http://127.0.0.1/cb",
        client_id="cid",
        client_secret="sec",
        code_verifier="ver",
    )
    assert out["id_token"] == "t"
    assert captured["fields"]["code_verifier"] == "ver"
    assert captured["fields"]["client_secret"] == "sec"
    assert captured["fields"]["grant_type"] == "authorization_code"


def _b64u_int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
