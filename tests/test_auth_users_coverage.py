"""Behavioral coverage for authentication and administrator account boundaries."""

from __future__ import annotations

import pytest

from llm_wiki.services import auth
from llm_wiki.services import users as users_svc
from llm_wiki.services.errors import NotFoundError, ValidationError


def test_password_hash_failures_and_user_validation_leave_no_rows(ctx):
    assert auth.verify_password("not-an-argon-hash", "secret12") is False

    with pytest.raises(ValidationError, match="at most 128"):
        auth.create_user(ctx.db, "u" * 129, "secret12")
    with pytest.raises(ValidationError, match="username is required"):
        auth.create_user(ctx.db, "   ", "secret12")
    with pytest.raises(ValidationError, match="role must be one of"):
        auth.create_user(ctx.db, "invalid-role", "secret12", "owner")
    user_id = auth.create_user(ctx.db, "duplicate", "secret12")
    with pytest.raises(ValidationError, match="already exists"):
        auth.create_user(ctx.db, " duplicate ", "different12")

    assert users_svc.get_user(ctx.db, user_id)["username"] == "duplicate"
    assert [user for user in users_svc.list_users(ctx.db) if user["id"] == user_id] == [
        {
            "id": user_id,
            "username": "duplicate",
            "role": "editor",
            "is_active": 1,
            "created_at": users_svc.get_user(ctx.db, user_id)["created_at"],
        }
    ]
    with pytest.raises(NotFoundError, match="user not found"):
        users_svc.get_user(ctx.db, user_id + 10_000)

    with ctx.db.reader() as conn:
        rows = conn.execute("SELECT username FROM users WHERE username='duplicate'").fetchall()
    assert [row["username"] for row in rows] == ["duplicate"]


def test_key_throttle_name_limit_and_update_race_preserve_active_key(ctx, principals, monkeypatch):
    monkeypatch.setattr(auth.time, "monotonic", lambda: 10**12)
    principal = principals["editor"]
    token = auth.create_api_key(ctx.db, principal, "bounded")
    key = auth.list_api_keys(ctx.db, principal.user_id)[0]

    assert auth.principal_from_api_key(ctx.db, token) == auth.Principal(
        principal.user_id,
        principal.username,
        principal.role,
        via="mcp",
        credential_version=principal.credential_version,
    )
    first_used = auth.list_api_keys(ctx.db, principal.user_id)[0]["last_used_at"]
    assert first_used is not None
    assert auth.principal_from_api_key(ctx.db, token) is not None
    assert auth.list_api_keys(ctx.db, principal.user_id)[0]["last_used_at"] == first_used

    with pytest.raises(ValidationError, match="at most 128"):
        auth.create_api_key(ctx.db, principal, "n" * 129)

    with ctx.db.writer() as conn:
        conn.execute(
            "CREATE TRIGGER ignore_key_revoke BEFORE UPDATE OF revoked_at ON api_keys "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    with pytest.raises(NotFoundError, match="active API key not found"):
        # A database-level no-op models the race after lookup without replacing
        # service internals.
        auth.revoke_api_key(ctx.db, principal, key["id"])

    assert auth.principal_from_api_key(ctx.db, token) is not None
    assert auth.list_api_keys(ctx.db, principal.user_id)[0]["revoked_at"] is None


def test_session_secret_is_generated_once_and_persisted(ctx):
    auth.delete_session(ctx.db, None)
    generated = auth.get_or_create_session_secret(ctx.db, "")
    assert len(generated) == 64
    assert auth.get_or_create_session_secret(ctx.db, "") == generated
    assert auth.get_or_create_session_secret(ctx.db, "configured") == "configured"
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT v FROM meta WHERE k='session_secret'").fetchone()[0] == generated
        )


def test_user_mutation_errors_are_atomic_and_last_admin_is_protected(ctx, principals):
    admin = principals["admin"]
    editor = principals["editor"]
    missing = 100_000

    with pytest.raises(ValidationError, match="last active admin"):
        users_svc.set_role(ctx.db, admin.user_id, "viewer")
    with pytest.raises(ValidationError, match="role must be one of"):
        users_svc.set_role(ctx.db, admin.user_id, "owner")
    with pytest.raises(ValidationError, match="last active admin"):
        users_svc.set_active(ctx.db, admin.user_id, False)
    with pytest.raises(ValidationError, match="last active admin"):
        users_svc.delete_user(ctx.db, admin.user_id)
    with pytest.raises(NotFoundError, match="user not found"):
        users_svc.set_role(ctx.db, missing, "viewer")
    with pytest.raises(NotFoundError, match="user not found"):
        users_svc.set_active(ctx.db, missing, False)
    with pytest.raises(NotFoundError, match="user not found"):
        users_svc.set_password(ctx.db, missing, "replacement12")
    with pytest.raises(NotFoundError, match="user not found"):
        users_svc.delete_user(ctx.db, missing)
    with pytest.raises(ValidationError, match="at least 8"):
        users_svc.set_password(ctx.db, editor.user_id, "short")
    with pytest.raises(ValidationError, match="at most 1024"):
        users_svc.set_password(ctx.db, editor.user_id, "x" * 1025)

    assert users_svc.get_user(ctx.db, admin.user_id)["role"] == "admin"
    assert users_svc.get_user(ctx.db, admin.user_id)["is_active"] == 1
    assert auth.authenticate(ctx.db, editor.username, "secret12") is not None


def test_unaudited_role_active_and_delete_paths_commit(ctx, principals):
    editor = principals["editor"]
    users_svc.set_role(ctx.db, editor.user_id, "viewer")
    assert users_svc.get_user(ctx.db, editor.user_id)["role"] == "viewer"

    users_svc.set_active(ctx.db, editor.user_id, True)
    assert users_svc.get_user(ctx.db, editor.user_id)["is_active"] == 1

    users_svc.delete_user(ctx.db, editor.user_id)
    with pytest.raises(NotFoundError):
        users_svc.get_user(ctx.db, editor.user_id)
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE target=?",
                (str(editor.user_id),),
            ).fetchone()[0]
            == 0
        )
