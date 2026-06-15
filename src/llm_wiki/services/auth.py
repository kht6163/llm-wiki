"""Authentication & authorization: password hashing, web sessions, per-user MCP
API keys, and the Principal that both surfaces resolve identity into."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher

from ..db import Database, get_meta, set_meta
from ..util import now_iso
from .errors import ValidationError

ROLES = ("admin", "editor", "viewer")
ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
SESSION_TTL_DAYS = 14
API_KEY_PREFIX_LEN = 12
MIN_PASSWORD_LEN = 8

_ph = PasswordHasher()

# A precomputed hash to verify against when the username doesn't exist, so a failed
# login spends the same Argon2 work whether or not the account is real. Without this,
# an unknown user returns instantly (no hash) while a real user pays the hashing
# cost — a timing oracle for username enumeration.
_DUMMY_PASSWORD_HASH = _ph.hash("enumeration-guard-not-a-real-secret")


@dataclass
class Principal:
    user_id: int
    username: str
    role: str
    via: str = "?"  # surface that resolved this identity: web | mcp | cli

    @property
    def can_write(self) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK["editor"]

    @property
    def can_admin(self) -> bool:
        return self.role == "admin"


# -- passwords -------------------------------------------------------------
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _ph.verify(stored_hash, password)
    except Exception:
        return False


def _validate_new_user(username: str, password: str, role: str) -> None:
    if not username or not username.strip():
        raise ValidationError("username is required")
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}")
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LEN} characters")


def create_user(db: Database, username: str, password: str, role: str = "editor") -> int:
    username = username.strip()
    _validate_new_user(username, password, role)
    now = now_iso()
    with db.writer() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValidationError(f"user '{username}' already exists")
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, role, is_active, created_at, updated_at) "
            "VALUES(?,?,?,1,?,?)",
            (username, hash_password(password), role, now, now),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid


def authenticate(db: Database, username: str, password: str) -> Principal | None:
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id, username, role, password_hash FROM users WHERE username=? AND is_active=1",
            (username.strip(),),
        ).fetchone()
    if row is None:
        # Spend equivalent Argon2 work so response time doesn't reveal whether the
        # username exists (the result is discarded).
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    if not verify_password(row["password_hash"], password):
        return None
    return Principal(row["id"], row["username"], row["role"])


# -- web sessions ----------------------------------------------------------
def create_session(db: Database, user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.writer() as conn:
        # Opportunistic GC: expired rows are otherwise only filtered at read time and
        # would accumulate forever. Login is infrequent and the table is small, so a
        # sweep here keeps it bounded without a separate scheduled job.
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_str,))
        conn.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (sid, user_id, now_str, expires),
        )
    return sid


def principal_from_session(db: Database, sid: str | None) -> Principal | None:
    if not sid:
        return None
    now = now_iso()
    with db.reader() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.id=? AND s.expires_at > ? AND u.is_active=1",
            (sid, now),
        ).fetchone()
    return Principal(row["id"], row["username"], row["role"], via="web") if row else None


def delete_session(db: Database, sid: str | None) -> None:
    if not sid:
        return
    with db.writer() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))


# -- API keys (MCP) --------------------------------------------------------
# Throttle last_used_at writes: without this, every authenticated (read-only)
# MCP tool call would take the writer lock just to stamp a timestamp, contending
# with real writes. One write per key per window is plenty for an activity hint.
_LAST_USED_THROTTLE_S = 60.0
_last_used_marks: dict[int, float] = {}
_last_used_lock = threading.Lock()


def _should_stamp_last_used(key_id: int) -> bool:
    now = time.monotonic()
    with _last_used_lock:
        if now - _last_used_marks.get(key_id, 0.0) >= _LAST_USED_THROTTLE_S:
            _last_used_marks[key_id] = now
            return True
    return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_key(db: Database, user_id: int, name: str) -> str:
    """Mint a new API key; the raw token is returned ONCE (only its hash is stored)."""
    token = "lw_" + secrets.token_urlsafe(32)
    prefix = token[:API_KEY_PREFIX_LEN]
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, created_at) "
            "VALUES(?,?,?,?,?)",
            (user_id, (name or "key").strip(), prefix, _hash_token(token), now_iso()),
        )
    return token


def principal_from_api_key(db: Database, raw: str | None) -> Principal | None:
    if not raw or len(raw) < API_KEY_PREFIX_LEN:
        return None
    prefix = raw[:API_KEY_PREFIX_LEN]
    with db.reader() as conn:
        row = conn.execute(
            "SELECT k.id, k.key_hash, u.id AS uid, u.username, u.role "
            "FROM api_keys k JOIN users u ON u.id=k.user_id "
            "WHERE k.key_prefix=? AND k.revoked_at IS NULL AND u.is_active=1",
            (prefix,),
        ).fetchone()
    if not row or not hmac.compare_digest(row["key_hash"], _hash_token(raw)):
        return None
    if _should_stamp_last_used(row["id"]):
        with db.writer() as conn:
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
    return Principal(row["uid"], row["username"], row["role"], via="mcp")


def list_api_keys(db: Database, user_id: int) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_api_key(db: Database, user_id: int, key_id: int) -> None:
    with db.writer() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now_iso(), key_id, user_id),
        )


# -- credential-change invalidation (conn-scoped; run inside the caller's txn) ---
# Policy (see CLAUDE.md): API keys are NOT time-expired — sustaining long-lived
# agent keys is intentional. The ONLY automatic revocation triggers are a password
# change or account deactivation, and the same triggers also drop the user's web
# sessions. Both run inside the caller's writer transaction so the credential
# change and its invalidation commit atomically.
def revoke_user_sessions(conn, user_id: int) -> int:
    return conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount


def revoke_user_api_keys(conn, user_id: int, *, now: str | None = None) -> int:
    return conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now or now_iso(), user_id),
    ).rowcount


def invalidate_credentials(conn, user_id: int) -> dict[str, int]:
    """Revoke all active web sessions AND MCP API keys for a user. Returns the
    counts touched. Call on password change / deactivation only."""
    now = now_iso()
    return {
        "sessions": revoke_user_sessions(conn, user_id),
        "api_keys": revoke_user_api_keys(conn, user_id, now=now),
    }


# -- session signing secret ------------------------------------------------
def get_or_create_session_secret(db: Database, configured: str) -> str:
    if configured:
        return configured
    with db.writer() as conn:
        s = get_meta(conn, "session_secret")
        if not s:
            s = secrets.token_hex(32)
            set_meta(conn, "session_secret", s)
        return s
