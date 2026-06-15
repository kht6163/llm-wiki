"""Credential-change invalidation (batch A security policy): a password change or
deactivation drops the user's web sessions AND revokes their API keys — and a
later reactivation must NOT resurrect the old keys."""
from llm_wiki.services import auth
from llm_wiki.services import users as users_svc


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


def test_invalidate_counts(ctx, principals):
    db, uid = ctx.db, principals["editor"].user_id
    auth.create_session(db, uid)
    auth.create_api_key(db, uid, "k1")
    auth.create_api_key(db, uid, "k2")
    with db.writer() as conn:
        counts = auth.invalidate_credentials(conn, uid)
    assert counts["sessions"] == 1 and counts["api_keys"] == 2
