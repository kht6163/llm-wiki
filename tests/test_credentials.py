"""Credential-change invalidation (batch A security policy): a password change or
deactivation drops the user's web sessions AND revokes their API keys — and a
later reactivation must NOT resurrect the old keys."""
import pytest

from llm_wiki.services import auth
from llm_wiki.services import users as users_svc
from llm_wiki.util import normalize_client_ip


def test_password_change_revokes_sessions_and_keys(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    sid = auth.create_session(db, uid)
    key = auth.create_api_key(db, uid, "agent")
    assert auth.principal_from_session(db, sid) is not None
    assert auth.principal_from_api_key(db, key) is not None

    users_svc.set_password(db, uid, "newsecret12")

    assert auth.principal_from_session(db, sid) is None   # session dropped
    assert auth.principal_from_api_key(db, key) is None   # key revoked


def test_deactivation_revokes_sessions_and_keys(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    sid = auth.create_session(db, uid)
    key = auth.create_api_key(db, uid, "agent")

    users_svc.set_active(db, uid, False)

    assert auth.principal_from_session(db, sid) is None
    assert auth.principal_from_api_key(db, key) is None


def test_reactivation_does_not_resurrect_revoked_keys(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    key = auth.create_api_key(db, uid, "agent")
    users_svc.set_active(db, uid, False)
    users_svc.set_active(db, uid, True)  # back to active...
    # ...but the key was revoked, not merely hidden by the is_active filter.
    assert auth.principal_from_api_key(db, key) is None


def test_create_session_purges_expired_rows(ctx, principals):
    # Expired sessions are only filtered at read time; a login must sweep them so the
    # table stays bounded over time.
    db, uid = ctx.db, principals["editor"].user_id
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO sessions(id, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            ("stale-sid", uid, "2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z"),
        )
    new_sid = auth.create_session(db, uid)
    with db.reader() as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    assert "stale-sid" not in ids and new_sid in ids


def test_invalidate_counts(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    auth.create_session(db, uid)
    auth.create_api_key(db, uid, "k1")
    auth.create_api_key(db, uid, "k2")
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
