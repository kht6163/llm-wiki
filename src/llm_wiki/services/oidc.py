"""Minimal OIDC client helpers: PKCE S256, discovery + JWKS cache, ID-token verify.

Uses the stdlib (urllib) for HTTP so the runtime does not depend on httpx.
ID tokens are verified with PyJWT (RS256): iss, aud, exp, nonce.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from .errors import ValidationError

# Discovery / JWKS responses are cached process-wide for this TTL.
_DISCOVERY_TTL_S = 3600.0
_HTTP_TIMEOUT_S = 15.0

_cache_lock = threading.Lock()
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_clients: dict[str, PyJWKClient] = {}


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    # 43–128 chars of unreserved URL-safe characters (RFC 7636).
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def _http_get_json(url: str, *, timeout: float = _HTTP_TIMEOUT_S) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise ValidationError(f"OIDC HTTP error fetching {url}: {e.code}") from e
    except urllib.error.URLError as e:
        raise ValidationError(f"OIDC network error fetching {url}: {e.reason}") from e
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValidationError(f"OIDC response from {url} is not JSON") from e
    if not isinstance(data, dict):
        raise ValidationError(f"OIDC response from {url} is not a JSON object")
    return data


def _http_post_form(
    url: str, fields: dict[str, str], *, timeout: float = _HTTP_TIMEOUT_S
) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise ValidationError(
            f"OIDC token endpoint error {e.code}" + (f": {detail}" if detail else "")
        ) from e
    except urllib.error.URLError as e:
        raise ValidationError(f"OIDC network error posting to {url}: {e.reason}") from e
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValidationError("OIDC token response is not JSON") from e
    if not isinstance(parsed, dict):
        raise ValidationError("OIDC token response is not a JSON object")
    return parsed


def discovery_url(issuer: str) -> str:
    base = issuer.rstrip("/")
    return f"{base}/.well-known/openid-configuration"


def fetch_discovery(issuer: str, *, force: bool = False) -> dict[str, Any]:
    """Return the OpenID Provider Metadata document (cached)."""
    key = issuer.rstrip("/")
    now = time.monotonic()
    with _cache_lock:
        hit = _discovery_cache.get(key)
        if hit and not force and now - hit[0] < _DISCOVERY_TTL_S:
            return hit[1]
    doc = _http_get_json(discovery_url(key))
    # Soft check: issuer in metadata should match (trailing-slash tolerant).
    meta_iss = str(doc.get("issuer") or "").rstrip("/")
    if meta_iss and meta_iss != key:
        raise ValidationError(
            f"OIDC discovery issuer mismatch: expected {key!r}, got {meta_iss!r}"
        )
    with _cache_lock:
        _discovery_cache[key] = (now, doc)
    return doc


def clear_oidc_caches() -> None:
    """Test helper: drop discovery + JWKS client caches."""
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_clients.clear()


def _jwks_client(jwks_uri: str) -> PyJWKClient:
    with _cache_lock:
        client = _jwks_clients.get(jwks_uri)
        if client is None:
            # PyJWKClient fetches and caches keys; lifespan keeps them warm.
            client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=int(_DISCOVERY_TTL_S))
            _jwks_clients[jwks_uri] = client
        return client


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + sep + urllib.parse.urlencode(params)


def exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    code_verifier: str,
) -> dict[str, Any]:
    fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    return _http_post_form(token_endpoint, fields)


@dataclass(frozen=True)
class IdTokenClaims:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None
    preferred_username: str | None
    raw: dict[str, Any]


def verify_id_token(
    id_token: str,
    *,
    issuer: str,
    client_id: str,
    jwks_uri: str,
    nonce: str,
) -> IdTokenClaims:
    """Verify an RS256 ID token: signature via JWKS, iss/aud/exp/nonce claims."""
    if not id_token or not id_token.strip():
        raise ValidationError("missing id_token")
    try:
        signing_key = _jwks_client(jwks_uri).get_signing_key_from_jwt(id_token)
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer.rstrip("/"),
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise ValidationError(f"invalid id_token: {e}") from e

    token_nonce = payload.get("nonce")
    if not nonce or token_nonce != nonce:
        raise ValidationError("id_token nonce mismatch")

    email = payload.get("email")
    email_s = str(email).strip() if email is not None else None
    if email_s == "":
        email_s = None

    ev = payload.get("email_verified")
    email_verified: bool | None
    if ev is None:
        email_verified = None
    elif isinstance(ev, bool):
        email_verified = ev
    elif isinstance(ev, str):
        email_verified = ev.lower() in {"true", "1", "yes"}
    else:
        email_verified = bool(ev)

    pref = payload.get("preferred_username")
    preferred = str(pref).strip() if pref is not None and str(pref).strip() else None

    return IdTokenClaims(
        issuer=str(payload["iss"]).rstrip("/"),
        subject=str(payload["sub"]),
        email=email_s,
        email_verified=email_verified,
        preferred_username=preferred,
        raw=dict(payload),
    )


def claim_username(claims: IdTokenClaims, username_claim: str) -> str | None:
    """Extract a username from verified claims using the configured claim name."""
    if username_claim == "preferred_username":
        return claims.preferred_username
    if username_claim == "email":
        return claims.email
    raw = claims.raw.get(username_claim)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None
