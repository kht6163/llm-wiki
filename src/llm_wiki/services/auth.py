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
from . import audit
from .errors import NotFoundError, ValidationError

ROLES = ("admin", "editor", "viewer")
ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2}
API_KEY_SCOPES = ("read", "readwrite")
SESSION_TTL_DAYS = 14
API_KEY_PREFIX_LEN = 12
MIN_PASSWORD_LEN = 8
MAX_USERNAME_LEN = 128
MAX_PASSWORD_LEN = 1024
MAX_API_KEY_NAME_LEN = 128
# Keys with no last_used_at, or last_used older than this many days, surface as unused.
DEFAULT_UNUSED_AFTER_DAYS = 30

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
    credential_version: int = 1
    # MCP API key scope only (web sessions leave this as "readwrite").
    key_scope: str = "readwrite"
    # Resolved MCP key identity.  Writer transactions use this to fence an
    # already-authenticated request against a concurrent per-key revocation.
    api_key_id: int | None = None

    @property
    def can_write(self) -> bool:
        """Editor+ role, and for MCP keys the key scope must allow write."""
        if ROLE_RANK.get(self.role, 0) < ROLE_RANK["editor"]:
            return False
        # Read-only MCP keys keep the editor role for display but cannot write.
        if self.via == "mcp" and self.key_scope == "read":
            return False
        return True

    @property
    def can_write_via_key(self) -> bool:
        """Alias of can_write (includes key-scope gate for MCP principals)."""
        return self.can_write

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
    if len(username) > MAX_USERNAME_LEN:
        raise ValidationError(f"username must be at most {MAX_USERNAME_LEN} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise ValidationError("username cannot contain control characters")
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}")
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if len(password) > MAX_PASSWORD_LEN:
        raise ValidationError(f"password must be at most {MAX_PASSWORD_LEN} characters")


def _record_success(
    conn,
    *,
    audit_actor: str | None,
    audit_via: str | None,
    action: str,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    """Record a success only when the caller supplied an audit surface.

    The helper deliberately accepts the caller's existing connection so the security
    change and its audit row either both commit or both roll back.
    """
    if audit_via is not None:
        audit.record(
            conn,
            actor=audit_actor,
            via=audit_via,
            action=action,
            target=target,
            detail=detail,
        )


def create_user(
    db: Database,
    username: str,
    password: str,
    role: str = "editor",
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> int:
    username = username.strip()
    _validate_new_user(username, password, role)
    now = now_iso()
    password_hash = hash_password(password)
    with db.writer() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValidationError(f"user '{username}' already exists")
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, role, is_active, created_at, updated_at) "
            "VALUES(?,?,?,1,?,?)",
            (username, password_hash, role, now, now),
        )
        assert cur.lastrowid is not None
        _record_success(
            conn,
            audit_actor=audit_actor,
            audit_via=audit_via,
            action="user_create",
            target=username,
            detail=f"role={role}",
        )
        return cur.lastrowid


def authenticate(db: Database, username: str, password: str) -> Principal | None:
    username = username.strip()
    # Reject an oversized password before Argon2 work. Unknown oversized usernames
    # skip Argon2 after the indexed lookup below; the web/audit layers cap the value
    # before persistence. Existing legacy long usernames remain able to sign in.
    if len(password) > MAX_PASSWORD_LEN:
        return None
    with db.reader() as conn:
        row = conn.execute(
            "SELECT id, username, role, password_hash, credential_version "
            "FROM users WHERE username=? AND is_active=1",
            (username,),
        ).fetchone()
    if row is None:
        # New accounts are bounded, but an older database may contain a legacy
        # username above today's limit. Query it exactly for compatibility; only a
        # non-existent oversized name skips the dummy Argon2 work.
        if len(username) > MAX_USERNAME_LEN:
            return None
        # Spend equivalent Argon2 work so response time doesn't reveal whether the
        # username exists (the result is discarded).
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    # SSO-only accounts have no local password. Still burn Argon2 time so the
    # response does not reveal that the account is passwordless / linked to OIDC.
    if row["password_hash"] is None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    if not verify_password(row["password_hash"], password):
        return None
    return Principal(
        row["id"],
        row["username"],
        row["role"],
        credential_version=row["credential_version"],
    )


def _normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    text = email.strip().lower()
    return text or None


def _email_domain_allowed(email: str | None, allowed_domains: str) -> bool:
    """True when the allowlist is empty or the email's domain is listed."""
    domains = [d.strip().lower() for d in (allowed_domains or "").split(",") if d.strip()]
    if not domains:
        return True
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return domain in domains


def _sanitize_oidc_username(candidate: str) -> str:
    """Clamp an IdP-derived username to local rules (no control chars, length)."""
    name = (candidate or "").strip()
    if not name:
        raise ValidationError("OIDC username claim is empty")
    if len(name) > MAX_USERNAME_LEN:
        name = name[:MAX_USERNAME_LEN]
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValidationError("OIDC username contains control characters")
    return name


def _unique_username(conn, base: str) -> str:
    """Pick an unused username, appending a numeric suffix on collision."""
    candidate = base
    n = 0
    while conn.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
        n += 1
        suffix = f"_{n}"
        trimmed = base[: max(1, MAX_USERNAME_LEN - len(suffix))]
        candidate = f"{trimmed}{suffix}"
    return candidate


def resolve_or_provision_oidc_user(
    db: Database,
    *,
    issuer: str,
    sub: str,
    email: str | None,
    preferred_username: str | None,
    settings,
) -> Principal:
    """Link an OIDC subject to a local user (or create one).

    Match order: (issuer, sub) → case-normalized email → auto-provision (if enabled).
    New users receive ``settings.oidc_default_role`` (never forced admin).
    """
    issuer = (issuer or "").rstrip("/")
    sub = (sub or "").strip()
    if not issuer or not sub:
        raise ValidationError("OIDC issuer and subject are required")

    email_norm = _normalize_email(email)
    require_verified = bool(getattr(settings, "oidc_require_email_verified", True))
    # Domain allowlist always applies when set; verified-email gate applies when
    # we have an email claim and the setting demands it (callback enforces verified
    # before calling us when require_email_verified and email is used for policy).
    allowed_domains = getattr(settings, "oidc_allowed_email_domains", "") or ""
    if allowed_domains and not _email_domain_allowed(email_norm, allowed_domains):
        raise ValidationError("email domain is not allowed for SSO")

    default_role = getattr(settings, "oidc_default_role", "viewer") or "viewer"
    if default_role not in ROLES:
        raise ValidationError(f"oidc_default_role must be one of {ROLES}")
    auto_provision = bool(getattr(settings, "oidc_auto_provision", True))
    username_claim_fallback = preferred_username or (email_norm.split("@", 1)[0] if email_norm else None)

    now = now_iso()
    with db.writer() as conn:
        # 1) Exact OIDC subject link.
        row = conn.execute(
            "SELECT id, username, role, credential_version, is_active FROM users "
            "WHERE oidc_issuer=? AND oidc_sub=?",
            (issuer, sub),
        ).fetchone()
        if row is not None:
            if not row["is_active"]:
                raise ValidationError("account is disabled")
            if email_norm:
                # Refresh email if empty or changed (keep unique).
                conn.execute(
                    "UPDATE users SET email=COALESCE(email, ?), updated_at=? WHERE id=?",
                    (email_norm, now, row["id"]),
                )
            return Principal(
                row["id"],
                row["username"],
                row["role"],
                via="web",
                credential_version=row["credential_version"],
            )

        # 2) Email match (case-normalized). Link the OIDC subject onto that user.
        if email_norm:
            row = conn.execute(
                "SELECT id, username, role, credential_version, is_active, "
                "oidc_issuer, oidc_sub FROM users WHERE email=?",
                (email_norm,),
            ).fetchone()
            if row is not None:
                if not row["is_active"]:
                    raise ValidationError("account is disabled")
                if row["oidc_issuer"] and (
                    row["oidc_issuer"] != issuer or row["oidc_sub"] != sub
                ):
                    raise ValidationError(
                        "email is already linked to a different SSO identity"
                    )
                conn.execute(
                    "UPDATE users SET oidc_issuer=?, oidc_sub=?, email=?, updated_at=? "
                    "WHERE id=?",
                    (issuer, sub, email_norm, now, row["id"]),
                )
                return Principal(
                    row["id"],
                    row["username"],
                    row["role"],
                    via="web",
                    credential_version=row["credential_version"],
                )

        # 3) Auto-provision SSO-only user (password_hash NULL).
        if not auto_provision:
            raise ValidationError("no matching local user and auto-provision is disabled")

        if require_verified and allowed_domains and not email_norm:
            # Domain policy cannot be evaluated without an email.
            raise ValidationError("email is required for SSO on this deployment")

        base_name = _sanitize_oidc_username(
            username_claim_fallback or f"user_{sub[:12]}"
        )
        username = _unique_username(conn, base_name)
        cur = conn.execute(
            "INSERT INTO users(username, password_hash, role, is_active, "
            "email, oidc_issuer, oidc_sub, created_at, updated_at) "
            "VALUES(?,NULL,?,1,?,?,?,?,?)",
            (username, default_role, email_norm, issuer, sub, now, now),
        )
        assert cur.lastrowid is not None
        _record_success(
            conn,
            audit_actor=username,
            audit_via="web",
            action="user_create",
            target=username,
            detail=f"role={default_role} via=oidc",
        )
        return Principal(
            int(cur.lastrowid),
            username,
            default_role,
            via="web",
            credential_version=1,
        )


# -- web sessions ----------------------------------------------------------
def create_session(
    db: Database,
    principal: Principal,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
    audit_detail: str | None = None,
) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.writer() as conn:
        # Fence the INSERT with the exact credential generation that authenticate()
        # observed. A password change/deactivation between verification and this writer
        # transaction advances the generation, so a stale Principal cannot mint access.
        if not conn.execute(
            "SELECT 1 FROM users WHERE id=? AND is_active=1 AND credential_version=?",
            (principal.user_id, principal.credential_version),
        ).fetchone():
            raise ValidationError("the authenticated user is no longer eligible for a session")
        # Opportunistic GC: expired rows are otherwise only filtered at read time and
        # would accumulate forever. Login is infrequent and the table is small, so a
        # sweep here keeps it bounded without a separate scheduled job.
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_str,))
        conn.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (sid, principal.user_id, now_str, expires),
        )
        _record_success(
            conn,
            audit_actor=audit_actor or principal.username,
            audit_via=audit_via,
            action="login",
            detail=audit_detail,
        )
    return sid


def principal_from_session(db: Database, sid: str | None) -> Principal | None:
    if not sid:
        return None
    now = now_iso()
    with db.reader() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role, u.credential_version "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.id=? AND s.expires_at > ? AND u.is_active=1",
            (sid, now),
        ).fetchone()
    return (
        Principal(
            row["id"],
            row["username"],
            row["role"],
            via="web",
            credential_version=row["credential_version"],
        )
        if row
        else None
    )


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


def create_api_key(
    db: Database,
    principal: Principal,
    name: str,
    *,
    scope: str = "readwrite",
    audit_actor: str | None = None,
    audit_via: str | None = None,
    audit_detail: str | None = None,
) -> str:
    """Mint a new API key; the raw token is returned ONCE (only its hash is stored)."""
    token = "lw_" + secrets.token_urlsafe(32)
    prefix = token[:API_KEY_PREFIX_LEN]
    key_name = (name or "key").strip() or "key"
    if len(key_name) > MAX_API_KEY_NAME_LEN:
        raise ValidationError(
            f"API key name must be at most {MAX_API_KEY_NAME_LEN} characters"
        )
    key_scope = (scope or "readwrite").strip().lower()
    if key_scope not in API_KEY_SCOPES:
        raise ValidationError(
            f"API key scope must be one of {API_KEY_SCOPES} (got {scope!r})"
        )
    with db.writer() as conn:
        # Same generation fence as session minting: a key requested from a stale web
        # session cannot recreate access after password-change revocation commits.
        if not conn.execute(
            "SELECT 1 FROM users WHERE id=? AND is_active=1 AND credential_version=?",
            (principal.user_id, principal.credential_version),
        ).fetchone():
            raise ValidationError("the authenticated user is no longer eligible for an API key")
        conn.execute(
            "INSERT INTO api_keys(user_id, name, key_prefix, key_hash, created_at, scope) "
            "VALUES(?,?,?,?,?,?)",
            (principal.user_id, key_name, prefix, _hash_token(token), now_iso(), key_scope),
        )
        _record_success(
            conn,
            audit_actor=audit_actor or principal.username,
            audit_via=audit_via,
            action="key_mint",
            target=prefix,
            detail=audit_detail,
        )
    return token


def principal_from_api_key(db: Database, raw: str | None) -> Principal | None:
    if not raw or len(raw) < API_KEY_PREFIX_LEN:
        return None
    prefix = raw[:API_KEY_PREFIX_LEN]
    with db.reader() as conn:
        row = conn.execute(
            "SELECT k.id, k.key_hash, k.scope, u.id AS uid, u.username, u.role, "
            "u.credential_version "
            "FROM api_keys k JOIN users u ON u.id=k.user_id "
            "WHERE k.key_prefix=? AND k.revoked_at IS NULL AND u.is_active=1",
            (prefix,),
        ).fetchone()
    if not row or not hmac.compare_digest(row["key_hash"], _hash_token(raw)):
        return None
    if _should_stamp_last_used(row["id"]):
        with db.writer() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=? AND revoked_at IS NULL",
                (now_iso(), row["id"]),
            )
    scope = row["scope"] if row["scope"] in API_KEY_SCOPES else "readwrite"
    return Principal(
        row["uid"],
        row["username"],
        row["role"],
        via="mcp",
        credential_version=row["credential_version"],
        key_scope=scope,
        api_key_id=int(row["id"]),
    )


def list_api_keys(
    db: Database,
    user_id: int,
    *,
    unused_after_days: int = DEFAULT_UNUSED_AFTER_DAYS,
) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, revoked_at, scope "
            "FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    cutoff = datetime.now(UTC) - timedelta(days=max(1, int(unused_after_days)))
    out: list[dict] = []
    for r in rows:
        item = dict(r)
        scope = item.get("scope") or "readwrite"
        item["scope"] = scope if scope in API_KEY_SCOPES else "readwrite"
        last = item.get("last_used_at")
        if not last:
            item["unused"] = True
        else:
            try:
                # Accept trailing Z
                ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                item["unused"] = ts < cutoff
            except ValueError:
                item["unused"] = True
        out.append(item)
    return out


def revoke_api_key(
    db: Database,
    principal: Principal,
    key_id: int,
    *,
    audit_actor: str | None = None,
    audit_via: str | None = None,
) -> str:
    with db.writer() as conn:
        row = conn.execute(
            "SELECT k.key_prefix FROM api_keys k JOIN users u ON u.id=k.user_id "
            "WHERE k.id=? AND k.user_id=? AND k.revoked_at IS NULL "
            "AND u.is_active=1 AND u.credential_version=?",
            (key_id, principal.user_id, principal.credential_version),
        ).fetchone()
        if row is None:
            # Deliberately do not distinguish another user's key from a missing or
            # already-revoked one, and never write a false outcome='ok' audit row.
            raise NotFoundError("active API key not found")
        changed = conn.execute(
            "UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now_iso(), key_id, principal.user_id),
        )
        if changed.rowcount != 1:
            raise NotFoundError("active API key not found")
        prefix = str(row["key_prefix"])
        _record_success(
            conn,
            audit_actor=audit_actor or principal.username,
            audit_via=audit_via,
            action="key_revoke",
            target=prefix,
        )
        return prefix


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
