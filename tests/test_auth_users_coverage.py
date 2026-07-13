"""Behavioral coverage for authentication and administrator account boundaries."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services import audit, auth
from llm_wiki.services import users as users_svc
from llm_wiki.services.errors import NotFoundError, ValidationError


@contextmanager
def _isolated_last_used_marks():
    original = auth._last_used_marks
    original_values = dict(original)
    auth._last_used_marks = dict(original_values)
    try:
        yield
    finally:
        auth._last_used_marks = original
        original.clear()
        original.update(original_values)


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


def test_key_throttle_name_limit_and_update_race_preserve_active_key(
    ctx, principals, monkeypatch, tmp_path
):
    principal = principals["editor"]
    token = auth.create_api_key(ctx.db, principal, "bounded")
    key = auth.list_api_keys(ctx.db, principal.user_id)[0]
    future_mark = 10**12

    with _isolated_last_used_marks():
        monkeypatch.setattr(auth.time, "monotonic", lambda: future_mark)
        assert auth.principal_from_api_key(ctx.db, token) == auth.Principal(
            principal.user_id,
            principal.username,
            principal.role,
            via="mcp",
            credential_version=principal.credential_version,
            api_key_id=key["id"],
        )
        first_used = auth.list_api_keys(ctx.db, principal.user_id)[0]["last_used_at"]
        assert first_used is not None
        assert auth.principal_from_api_key(ctx.db, token) is not None
        assert auth.list_api_keys(ctx.db, principal.user_id)[0]["last_used_at"] == first_used

    fresh_settings = Settings(
        vault_path=tmp_path / "fresh-vault",
        db_path=tmp_path / "fresh-data" / "wiki.db",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        gui_port=8280,
        mcp_port=8281,
        session_secret="test-secret",
    )
    fresh_ctx = build_context(fresh_settings, full=True)
    fresh_user_id = auth.create_user(fresh_ctx.db, "fresh", "secret12")
    auth.create_api_key(fresh_ctx.db, auth.Principal(fresh_user_id, "fresh", "editor"), "fresh")
    fresh_key_id = auth.list_api_keys(fresh_ctx.db, fresh_user_id)[0]["id"]
    assert fresh_key_id == key["id"]
    assert auth._last_used_marks.get(fresh_key_id) != future_mark

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


def test_auth_faults_never_persist_or_expose_raw_credentials(ctx, principals, monkeypatch, caplog):
    principal = principals["editor"]
    raw_token = auth.create_api_key(ctx.db, principal, "redaction-check")
    raw_password = "never-store-or-report-this-password"

    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable") as password_error:
        users_svc.set_password(
            ctx.db,
            principal.user_id,
            raw_password,
            audit_actor="admin",
            audit_via="web",
        )

    with ctx.db.writer() as conn:
        conn.execute(
            "CREATE TRIGGER redact_revoke BEFORE UPDATE OF revoked_at ON api_keys "
            "BEGIN SELECT RAISE(IGNORE); END"
        )
    key_id = auth.list_api_keys(ctx.db, principal.user_id)[0]["id"]
    with pytest.raises(NotFoundError, match="active API key not found") as token_error:
        auth.revoke_api_key(ctx.db, principal, key_id)

    with ctx.db.reader() as conn:
        persisted = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("users", "api_keys", "sessions", "audit_log")
        }
    serialized_rows = json.dumps(persisted, ensure_ascii=False, default=str)
    serialized_errors = json.dumps(
        {"password": str(password_error.value), "token": str(token_error.value)}
    )
    for secret in (raw_token, raw_password):
        assert secret not in serialized_rows
        assert secret not in serialized_errors
        assert secret not in caplog.text

    assert auth.authenticate(ctx.db, principal.username, "secret12") is not None
    assert auth.authenticate(ctx.db, principal.username, raw_password) is None
    assert auth.principal_from_api_key(ctx.db, raw_token) is not None


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
