"""Credential-change invalidation (batch A security policy): a password change or
deactivation drops the user's web sessions AND revokes their API keys — and a
later reactivation must NOT resurrect the old keys."""
import pytest

from llm_wiki.services import audit, auth
from llm_wiki.services import users as users_svc
from llm_wiki.services.errors import NotFoundError, ValidationError
from llm_wiki.util import normalize_client_ip


def test_password_change_revokes_sessions_and_keys(ctx, principals):
    db, principal = ctx.db, principals["editor"]
    uid = principal.user_id
    sid = auth.create_session(db, principal)
    key = auth.create_api_key(db, principal, "agent")
    assert auth.principal_from_session(db, sid) is not None
    assert auth.principal_from_api_key(db, key) is not None

    users_svc.set_password(db, uid, "newsecret12")

    assert auth.principal_from_session(db, sid) is None   # session dropped
    assert auth.principal_from_api_key(db, key) is None   # key revoked


def test_deactivation_revokes_sessions_and_keys(ctx, principals):
    db, principal = ctx.db, principals["editor"]
    uid = principal.user_id
    sid = auth.create_session(db, principal)
    key = auth.create_api_key(db, principal, "agent")

    users_svc.set_active(db, uid, False)

    assert auth.principal_from_session(db, sid) is None
    assert auth.principal_from_api_key(db, key) is None


def test_reactivation_does_not_resurrect_revoked_keys(ctx, principals):
    db, principal = ctx.db, principals["editor"]
    uid = principal.user_id
    key = auth.create_api_key(db, principal, "agent")
    users_svc.set_active(db, uid, False)
    users_svc.set_active(db, uid, True)  # back to active...
    # ...but the key was revoked, not merely hidden by the is_active filter.
    assert auth.principal_from_api_key(db, key) is None


def test_inactive_user_cannot_receive_new_session_or_api_key(ctx, principals):
    """Credentials minted while disabled would become live after reactivation."""
    db, uid = ctx.db, principals["editor"].user_id
    users_svc.set_active(db, uid, False)

    with pytest.raises(ValidationError):
        auth.create_session(db, principals["editor"])
    with pytest.raises(ValidationError):
        auth.create_api_key(db, principals["editor"], "disabled-agent")

    users_svc.set_active(db, uid, True)
    with pytest.raises(ValidationError):
        auth.create_session(db, principals["editor"])
    with pytest.raises(ValidationError):
        auth.create_api_key(db, principals["editor"], "still-stale")
    with db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=?", (uid,)
        ).fetchone()[0] == 0


def test_stale_principal_cannot_mint_credentials_after_password_change(ctx):
    uid = auth.create_user(ctx.db, "stale", "secret12", "editor")
    stale = auth.authenticate(ctx.db, "stale", "secret12")
    assert stale is not None and stale.user_id == uid

    users_svc.set_password(ctx.db, uid, "newsecret12")

    with pytest.raises(ValidationError):
        auth.create_session(ctx.db, stale)
    with pytest.raises(ValidationError):
        auth.create_api_key(ctx.db, stale, "late-key")


def test_credential_version_advances_on_revocation_boundaries(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    before = auth.authenticate(db, "alice", "secret12")
    assert before is not None

    users_svc.set_password(db, uid, "newsecret12")
    after_password = auth.authenticate(db, "alice", "newsecret12")
    assert after_password is not None
    assert after_password.credential_version == before.credential_version + 1

    users_svc.set_active(db, uid, False)
    users_svc.set_active(db, uid, True)
    after_reactivation = auth.authenticate(db, "alice", "newsecret12")
    assert after_reactivation is not None
    assert after_reactivation.credential_version == after_password.credential_version + 1


def test_password_change_and_credential_invalidation_roll_back_if_audit_fails(
    ctx, principals, monkeypatch
):
    db, uid = ctx.db, principals["editor"].user_id
    session = auth.create_session(db, principals["editor"])
    key = auth.create_api_key(db, principals["editor"], "agent")

    def fail_record(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record", fail_record)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        users_svc.set_password(
            db,
            uid,
            "newsecret12",
            audit_actor="admin",
            audit_via="web",
        )

    assert auth.authenticate(db, "alice", "secret12") is not None
    assert auth.principal_from_session(db, session) is not None
    assert auth.principal_from_api_key(db, key) is not None


def test_api_key_audit_uses_prefix_and_revoke_is_atomic(ctx, principals, monkeypatch):
    db, principal = ctx.db, principals["editor"]
    key = auth.create_api_key(
        db,
        principal,
        "agent",
        audit_actor=principal.username,
        audit_via="web",
    )
    prefix = key[: auth.API_KEY_PREFIX_LEN]
    with db.reader() as conn:
        key_id = conn.execute(
            "SELECT id FROM api_keys WHERE key_prefix=?", (prefix,)
        ).fetchone()[0]
        mint_target = conn.execute(
            "SELECT target FROM audit_log WHERE action='key_mint' ORDER BY id DESC"
        ).fetchone()[0]
    assert mint_target == prefix

    def fail_record(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record", fail_record)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        auth.revoke_api_key(
            db,
            principal,
            key_id,
            audit_actor=principal.username,
            audit_via="web",
        )
    assert auth.principal_from_api_key(db, key) is not None

    monkeypatch.undo()
    assert auth.revoke_api_key(
        db,
        principal,
        key_id,
        audit_actor=principal.username,
        audit_via="web",
    ) == prefix
    with db.reader() as conn:
        revoke_target = conn.execute(
            "SELECT target FROM audit_log WHERE action='key_revoke' ORDER BY id DESC"
        ).fetchone()[0]
    assert revoke_target == prefix


def test_cannot_revoke_another_users_key_or_audit_false_success(ctx, principals):
    db = ctx.db
    owner = principals["viewer"]
    attacker = principals["editor"]
    key = auth.create_api_key(db, owner, "viewer-key")
    with db.reader() as conn:
        key_id = conn.execute(
            "SELECT id FROM api_keys WHERE key_prefix=?",
            (key[: auth.API_KEY_PREFIX_LEN],),
        ).fetchone()[0]

    with pytest.raises(NotFoundError):
        auth.revoke_api_key(
            db,
            attacker,
            key_id,
            audit_actor=attacker.username,
            audit_via="web",
        )

    assert auth.principal_from_api_key(db, key) is not None
    with db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='key_revoke' AND outcome='ok' AND actor=?",
            (attacker.username,),
        ).fetchone()[0] == 0


def test_create_session_purges_expired_rows(ctx, principals):
    # Expired sessions are only filtered at read time; a login must sweep them so the
    # table stays bounded over time.
    db, uid = ctx.db, principals["editor"].user_id
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            ("stale-sid", uid, "2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z"),
        )
    new_sid = auth.create_session(db, principals["editor"])
    with db.reader() as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    assert "stale-sid" not in ids and new_sid in ids


def test_invalidate_counts(ctx, principals):
    db, principal = ctx.db, principals["editor"]
    uid = principal.user_id
    auth.create_session(db, principal)
    auth.create_api_key(db, principal, "k1")
    auth.create_api_key(db, principal, "k2")
    with db.writer() as conn:
        counts = auth.invalidate_credentials(conn, uid)
    assert counts["sessions"] == 1 and counts["api_keys"] == 2


def test_authenticate_unknown_user_still_verifies_a_hash(ctx, principals, monkeypatch):
    # Username enumeration guard: a login for a non-existent user must spend the same
    # Argon2 work as a real user, so verify_password is invoked either way.
    calls: list[str] = []
    real_verify = auth.verify_password

    def counting_verify(stored_hash, password):
        calls.append(stored_hash)
        return real_verify(stored_hash, password)

    monkeypatch.setattr(auth, "verify_password", counting_verify)

    assert auth.authenticate(ctx.db, "no-such-user", "whatever12") is None
    assert len(calls) == 1  # the dummy hash was verified despite no matching row
    assert calls[0] == auth._DUMMY_PASSWORD_HASH

    calls.clear()
    assert auth.authenticate(ctx.db, "alice", "secret12") is not None
    assert len(calls) == 1 and calls[0] != auth._DUMMY_PASSWORD_HASH


def test_authentication_input_lengths_are_bounded(ctx, monkeypatch):
    username = "u" * auth.MAX_USERNAME_LEN
    password = "p" * auth.MAX_PASSWORD_LEN
    uid = auth.create_user(ctx.db, username, password, "viewer")
    assert uid > 0
    assert auth.authenticate(ctx.db, username, password) is not None

    with pytest.raises(ValidationError):
        auth.create_user(ctx.db, username + "x", "secret12", "viewer")
    with pytest.raises(ValidationError):
        auth.create_user(ctx.db, "bounded", password + "x", "viewer")
    with pytest.raises(ValidationError):
        auth.create_user(ctx.db, "log\ninjection", "secret12", "viewer")

    legacy_username = "l" * (auth.MAX_USERNAME_LEN + 1)
    with ctx.db.writer() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,is_active,created_at,updated_at) "
            "VALUES(?,?, 'viewer',1,'now','now')",
            (legacy_username, auth.hash_password("legacy-pass")),
        )
    assert auth.authenticate(ctx.db, legacy_username, "legacy-pass") is not None

    def should_not_hash(*args, **kwargs):
        raise AssertionError("oversized login input reached Argon2")

    monkeypatch.setattr(auth, "verify_password", should_not_hash)
    assert auth.authenticate(ctx.db, username + "x", "secret12") is None
    assert auth.authenticate(ctx.db, "bounded", password + "x") is None


@pytest.mark.parametrize("host,expected", [
    ("::ffff:192.0.2.5", "192.0.2.5"),   # IPv4-mapped IPv6 collapses to IPv4
    ("192.0.2.5", "192.0.2.5"),          # plain IPv4 unchanged
    ("2001:db8::1", "2001:db8::1"),      # genuine IPv6 preserved
    ("proxy-host", "proxy-host"),        # non-IP literal passes through
    (None, "?"),                          # missing client
    ("", "?"),
])
def test_normalize_client_ip(host, expected):
    assert normalize_client_ip(host) == expected
