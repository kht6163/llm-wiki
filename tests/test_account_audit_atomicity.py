"""Security-sensitive account changes and their success audits commit together."""

import pytest

from llm_wiki.services import audit, auth
from llm_wiki.services import users as users_svc


def _fail_audit(*args, **kwargs):
    raise RuntimeError("audit unavailable")


def test_user_create_rolls_back_if_success_audit_fails(ctx, monkeypatch):
    monkeypatch.setattr(audit, "record", _fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        auth.create_user(
            ctx.db,
            "not-created",
            "secret12",
            "editor",
            audit_actor="admin",
            audit_via="web",
        )
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM users WHERE username='not-created'"
        ).fetchone() is None


def test_session_and_key_mint_roll_back_if_success_audit_fails(
    ctx, principals, monkeypatch
):
    principal = principals["editor"]
    monkeypatch.setattr(audit, "record", _fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        auth.create_session(
            ctx.db,
            principal,
            audit_actor=principal.username,
            audit_via="web",
        )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        auth.create_api_key(
            ctx.db,
            principal,
            "not-created",
            audit_actor=principal.username,
            audit_via="web",
        )

    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?", (principal.user_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=?", (principal.user_id,)
        ).fetchone()[0] == 0


def test_role_active_and_delete_roll_back_if_success_audit_fails(
    ctx, principals, monkeypatch
):
    principal = principals["editor"]
    session = auth.create_session(ctx.db, principal)
    key = auth.create_api_key(ctx.db, principal, "survives")
    monkeypatch.setattr(audit, "record", _fail_audit)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        users_svc.set_role(
            ctx.db,
            principal.user_id,
            "viewer",
            audit_actor="admin",
            audit_via="web",
        )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        users_svc.set_active(
            ctx.db,
            principal.user_id,
            False,
            audit_actor="admin",
            audit_via="web",
        )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        users_svc.delete_user(
            ctx.db,
            principal.user_id,
            audit_actor="admin",
            audit_via="web",
        )

    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT role,is_active FROM users WHERE id=?", (principal.user_id,)
        ).fetchone()
    assert tuple(row) == ("editor", 1)
    assert auth.principal_from_session(ctx.db, session) is not None
    assert auth.principal_from_api_key(ctx.db, key) is not None
