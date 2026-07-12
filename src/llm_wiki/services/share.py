"""Signed, time-bound public read links for a single document.

Tokens are itsdangerous URLSafeTimedSerializer payloads. They grant *read* of one
path only — never write, never list, never search. No DB row is required (stateless);
revoke by rotating SESSION_SECRET or waiting for max_age.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..util import normalize_rel_path
from .errors import ValidationError

_SALT = "llm-wiki-share-v1"
# Default share lifetime: 30 days. Callers may pass a shorter max_age on verify.
DEFAULT_MAX_AGE_S = 30 * 24 * 3600


def _serializer(secret: str) -> URLSafeTimedSerializer:
    if not secret:
        raise ValidationError("share links require a session secret")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def mint_share_token(secret: str, path: str) -> str:
    rel = normalize_rel_path(path)
    return _serializer(secret).dumps({"path": rel})


def verify_share_token(
    secret: str,
    token: str,
    *,
    max_age_s: int = DEFAULT_MAX_AGE_S,
) -> str:
    try:
        data = _serializer(secret).loads(token, max_age=max_age_s)
    except SignatureExpired as exc:
        raise ValidationError("share link has expired") from exc
    except BadSignature as exc:
        raise ValidationError("share link is invalid") from exc
    path = data.get("path") if isinstance(data, dict) else None
    if not path or not isinstance(path, str):
        raise ValidationError("share link is invalid")
    return normalize_rel_path(path)
