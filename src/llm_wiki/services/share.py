"""Revocable, time-bound public read links for exactly one document."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..db import Database
from ..util import normalize_rel_path, now_iso, path_norm
from . import audit
from .auth import ROLE_RANK, Principal
from .errors import ForbiddenError, NotFoundError, ValidationError

_SALT = "llm-wiki-share-v2"
_LEGACY_SALT = "llm-wiki-share-v1"
DEFAULT_MAX_AGE_S = 30 * 24 * 3600
_LAST_USED_WRITE_INTERVAL_S = 60


def _serializer(secret: str, *, legacy: bool = False) -> URLSafeTimedSerializer:
    if not secret:
        raise ValidationError("share links require a session secret")
    return URLSafeTimedSerializer(secret, salt=_LEGACY_SALT if legacy else _SALT)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authorize_writer(conn, principal: Principal) -> str:
    row = conn.execute(
        "SELECT role,is_active,credential_version FROM users WHERE id=?",
        (principal.user_id,),
    ).fetchone()
    if (
        row is None
        or not row["is_active"]
        or int(row["credential_version"]) != int(principal.credential_version)
        or ROLE_RANK.get(str(row["role"]), 0) < ROLE_RANK["editor"]
    ):
        raise ForbiddenError("Only an active editor can manage share links.")
    return str(row["role"])


def mint_share_token(
    secret: str,
    path: str,
    *,
    db: Database | None = None,
    principal: Principal | None = None,
    max_age_s: int = DEFAULT_MAX_AGE_S,
) -> str:
    """Mint a token; with ``db`` it is recorded and individually revocable.

    The no-DB form remains for parsing/unit compatibility with v1 stateless tokens;
    all application routes use the revocable ledger form.
    """
    rel = normalize_rel_path(path)
    if db is None:
        return _serializer(secret, legacy=True).dumps({"path": rel})
    if principal is None:
        raise ValidationError("a principal is required to create a share link")
    lifetime = max(60, min(int(max_age_s), DEFAULT_MAX_AGE_S))
    jti = secrets.token_urlsafe(32)
    token = _serializer(secret).dumps({"jti": jti})
    created = datetime.now(UTC)
    expires = created + timedelta(seconds=lifetime)
    created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.writer() as conn:
        _authorize_writer(conn, principal)
        doc = conn.execute(
            "SELECT id,path FROM documents WHERE path_norm=? AND is_deleted=0",
            (path_norm(rel),),
        ).fetchone()
        if doc is None:
            raise NotFoundError("No document at this path.", path=rel)
        conn.execute(
            "INSERT INTO share_links(token_hash,doc_id,created_by,created_at,expires_at) "
            "VALUES(?,?,?,?,?)",
            (_token_hash(token), doc["id"], principal.user_id, created_at, expires_at),
        )
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="share_mint",
            target=str(doc["path"]),
            detail=f"expires={expires_at}",
        )
    return token


def verify_share_token(
    secret: str,
    token: str,
    *,
    db: Database | None = None,
    max_age_s: int = DEFAULT_MAX_AGE_S,
) -> str:
    try:
        data = _serializer(secret).loads(token, max_age=max_age_s)
    except SignatureExpired as exc:
        raise ValidationError("share link has expired") from exc
    except BadSignature:
        # Keep already-issued v1 links valid for their original 30-day maximum.
        try:
            legacy = _serializer(secret, legacy=True).loads(token, max_age=max_age_s)
        except SignatureExpired as exc:
            raise ValidationError("share link has expired") from exc
        except BadSignature as exc:
            raise ValidationError("share link is invalid") from exc
        path = legacy.get("path") if isinstance(legacy, dict) else None
        if not isinstance(path, str) or not path:
            raise ValidationError("share link is invalid") from None
        return normalize_rel_path(path)

    jti = data.get("jti") if isinstance(data, dict) else None
    if not isinstance(jti, str) or not jti or db is None:
        raise ValidationError("share link is invalid")
    now_dt = datetime.now(UTC)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    token_hash = _token_hash(token)
    with db.reader() as conn:
        row = conn.execute(
            "SELECT s.id,s.last_used_at,d.path FROM share_links s "
            "JOIN documents d ON d.id=s.doc_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? "
            "AND d.is_deleted=0",
            (token_hash, now),
        ).fetchone()
    if row is None:
        raise ValidationError("share link is expired, revoked, or invalid")

    # Anonymous reads must not take the global writer lock on every request.  At most
    # once per minute per link, opportunistically refresh usage metadata; failure to
    # acquire/commit that telemetry write never invalidates the already-verified read.
    cutoff = (now_dt - timedelta(seconds=_LAST_USED_WRITE_INTERVAL_S)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    last_used = row["last_used_at"]
    if last_used is None or str(last_used) <= cutoff:
        try:
            db.try_write(
                "UPDATE share_links SET last_used_at=? WHERE id=? AND token_hash=? "
                "AND revoked_at IS NULL AND expires_at>? "
                "AND (last_used_at IS NULL OR last_used_at<=?)",
                (now, row["id"], token_hash, now, cutoff),
            )
        except sqlite3.Error:
            pass
    return normalize_rel_path(str(row["path"]))


def list_share_links(db: Database, principal: Principal, path: str) -> list[dict]:
    rel = normalize_rel_path(path)
    with db.reader() as conn:
        current = conn.execute(
            "SELECT role,is_active,credential_version FROM users WHERE id=?",
            (principal.user_id,),
        ).fetchone()
        if (
            current is None
            or not current["is_active"]
            or int(current["credential_version"]) != int(principal.credential_version)
            or ROLE_RANK.get(str(current["role"]), 0) < ROLE_RANK["editor"]
        ):
            raise ForbiddenError("Only an active editor can list share links.")
        rows = conn.execute(
            "SELECT s.id,s.created_at,s.expires_at,s.revoked_at,s.last_used_at,s.created_by,"
            "u.username AS created_by_name FROM share_links s "
            "JOIN documents d ON d.id=s.doc_id LEFT JOIN users u ON u.id=s.created_by "
            "WHERE d.path_norm=? AND (s.created_by=? OR ?='admin') ORDER BY s.id DESC",
            (path_norm(rel), principal.user_id, str(current["role"])),
        ).fetchall()
    return [dict(row) for row in rows]


def revoke_share_link(db: Database, principal: Principal, link_id: int) -> dict:
    with db.writer() as conn:
        role = _authorize_writer(conn, principal)
        row = conn.execute(
            "SELECT s.id,s.created_by,s.revoked_at,d.path FROM share_links s "
            "JOIN documents d ON d.id=s.doc_id WHERE s.id=?",
            (int(link_id),),
        ).fetchone()
        if row is None or (row["created_by"] != principal.user_id and role != "admin"):
            raise NotFoundError("active share link not found")
        if row["revoked_at"] is None:
            revoked_at = now_iso()
            conn.execute("UPDATE share_links SET revoked_at=? WHERE id=?", (revoked_at, row["id"]))
            audit.record(
                conn,
                actor=principal.username,
                via=principal.via,
                action="share_revoke",
                target=str(row["path"]),
                detail=f"link_id={row['id']}",
            )
        else:
            revoked_at = str(row["revoked_at"])
        return {"id": int(row["id"]), "path": str(row["path"]), "revoked_at": revoked_at}
