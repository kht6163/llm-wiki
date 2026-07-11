"""Admin user management. History (revisions/documents) is preserved on delete
via ON DELETE SET NULL, so authors simply become anonymous rather than blocking."""
from __future__ import annotations

from ..db import Database
from ..util import now_iso
from . import audit
from .auth import (
    MAX_PASSWORD_LEN,
    MIN_PASSWORD_LEN,
    ROLES,
    hash_password,
    invalidate_credentials,
)
from .errors import NotFoundError, ValidationError


def list_users(db: Database) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, username, role, is_active, created_at FROM users ORDER BY username"
        ).fetchall()
    return [dict(r) for r in rows]


def get_user(db: Database, user_id: int) -> dict:
    with db.reader() as conn:
        r = conn.execute(
            "SELECT id, username, role, is_active, created_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not r:
        raise NotFoundError("user not found")
    return dict(r)


def count_admins(db: Database) -> int:
    with db.reader() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1"
        ).fetchone()[0]


def _is_last_admin(conn, user_id: int) -> bool:
    row = conn.execute("SELECT role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or row["role"] != "admin" or not row["is_active"]:
        return False
    n = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]
    return n <= 1


def set_role(
    db: Database,
    user_id: int,
    role: str,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> None:
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}")
    with db.writer() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise NotFoundError("user not found")
        if role != "admin" and _is_last_admin(conn, user_id):
            raise ValidationError("cannot demote the last active admin")
        conn.execute("UPDATE users SET role=?, updated_at=? WHERE id=?", (role, now_iso(), user_id))
        if audit_via is not None:
            audit.record(
                conn,
                actor=audit_actor,
                via=audit_via,
                action="role_change",
                target=str(user_id),
                detail=f"role={role}",
            )


def set_active(
    db: Database,
    user_id: int,
    active: bool,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> None:
    with db.writer() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise NotFoundError("user not found")
        if not active and _is_last_admin(conn, user_id):
            raise ValidationError("cannot deactivate the last active admin")
        conn.execute(
            "UPDATE users SET is_active=?, credential_version=credential_version+?, "
            "updated_at=? WHERE id=?",
            (1 if active else 0, 0 if active else 1, now_iso(), user_id),
        )
        # Deactivating revokes every access path (sessions + API keys); the user
        # cannot have either while disabled.
        if not active:
            invalidate_credentials(conn, user_id)
        if audit_via is not None:
            audit.record(
                conn,
                actor=audit_actor,
                via=audit_via,
                action="user_active",
                target=str(user_id),
                detail=f"active={bool(active)}",
            )


def set_password(
    db: Database,
    user_id: int,
    password: str,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> None:
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValidationError(f"password must be at most {MAX_PASSWORD_LEN} characters")
    password_hash = hash_password(password)
    with db.writer() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise NotFoundError("user not found")
        conn.execute(
            "UPDATE users SET password_hash=?, credential_version=credential_version+1, "
            "updated_at=? WHERE id=?",
            (password_hash, now_iso(), user_id),
        )
        # A password change invalidates anything minted under the old credential:
        # existing sessions are dropped and all API keys revoked (see CLAUDE.md).
        invalidate_credentials(conn, user_id)
        if audit_via is not None:
            audit.record(
                conn,
                actor=audit_actor,
                via=audit_via,
                action="password_change",
                target=str(user_id),
            )


def delete_user(
    db: Database,
    user_id: int,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> None:
    with db.writer() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise NotFoundError("user not found")
        if _is_last_admin(conn, user_id):
            raise ValidationError("cannot delete the last active admin")
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        if audit_via is not None:
            audit.record(
                conn,
                actor=audit_actor,
                via=audit_via,
                action="user_delete",
                target=str(user_id),
            )
