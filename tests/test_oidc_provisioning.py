"""OIDC user linking / auto-provision and SSO-only password login behaviour."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_wiki.services import auth
from llm_wiki.services.errors import ValidationError


def _settings(**kw):
    base = dict(
        oidc_default_role="viewer",
        oidc_auto_provision=True,
        oidc_allowed_email_domains="",
        oidc_require_email_verified=True,
        oidc_username_claim="preferred_username",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_authenticate_sso_only_user_fails_with_dummy_argon2(ctx):
    db = ctx.db
    now = "2020-01-01T00:00:00Z"
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,is_active,email,"
            "oidc_issuer,oidc_sub,created_at,updated_at) "
            "VALUES('sso_only',NULL,'viewer',1,'sso@ex.com',"
            "'https://idp.example','sub-1',?,?)",
            (now, now),
        )
    assert auth.authenticate(db, "sso_only", "any-password-here") is None
    # Unknown user still fails with the same generic outcome.
    assert auth.authenticate(db, "no_such_user", "any-password-here") is None


def test_password_user_still_authenticates(ctx, principals):
    p = auth.authenticate(ctx.db, "alice", "secret12")
    assert p is not None
    assert p.username == "alice"


def test_provision_new_oidc_user(ctx):
    p = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="sub-new",
        email="New.User@Example.COM",
        preferred_username="newuser",
        settings=_settings(),
    )
    assert p.username == "newuser"
    assert p.role == "viewer"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT password_hash, email, oidc_issuer, oidc_sub, role FROM users "
            "WHERE id=?",
            (p.user_id,),
        ).fetchone()
    assert row["password_hash"] is None
    assert row["email"] == "new.user@example.com"
    assert row["oidc_issuer"] == "https://idp.example"
    assert row["oidc_sub"] == "sub-new"
    assert row["role"] == "viewer"


def test_link_by_issuer_sub_on_second_login(ctx):
    first = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="stable-sub",
        email="a@ex.com",
        preferred_username="alice_sso",
        settings=_settings(),
    )
    second = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="stable-sub",
        email="a@ex.com",
        preferred_username="alice_sso",
        settings=_settings(),
    )
    assert second.user_id == first.user_id
    with ctx.db.reader() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE oidc_sub='stable-sub'"
        ).fetchone()[0]
    assert n == 1


def test_link_by_email_to_existing_password_user(ctx, principals):
    # Give alice an email, then SSO with same email should link OIDC ids.
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE users SET email=? WHERE username=?",
            ("alice@corp.example", "alice"),
        )
    p = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="idp-alice",
        email="Alice@Corp.Example",
        preferred_username="alice",
        settings=_settings(),
    )
    assert p.username == "alice"
    assert p.user_id == principals["editor"].user_id
    # Password login still works after linking.
    assert auth.authenticate(ctx.db, "alice", "secret12") is not None
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT oidc_issuer, oidc_sub FROM users WHERE username='alice'"
        ).fetchone()
    assert row["oidc_issuer"] == "https://idp.example"
    assert row["oidc_sub"] == "idp-alice"


def test_domain_allowlist_rejects(ctx):
    with pytest.raises(ValidationError, match="domain"):
        auth.resolve_or_provision_oidc_user(
            ctx.db,
            issuer="https://idp.example",
            sub="x",
            email="user@evil.com",
            preferred_username="x",
            settings=_settings(oidc_allowed_email_domains="corp.example"),
        )


def test_auto_provision_disabled(ctx):
    with pytest.raises(ValidationError, match="auto-provision"):
        auth.resolve_or_provision_oidc_user(
            ctx.db,
            issuer="https://idp.example",
            sub="nobody",
            email="nobody@ex.com",
            preferred_username="nobody",
            settings=_settings(oidc_auto_provision=False),
        )


def test_default_role_never_auto_admin(ctx):
    p = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="r1",
        email="r1@ex.com",
        preferred_username="rolecheck",
        settings=_settings(oidc_default_role="editor"),
    )
    assert p.role == "editor"
    # Explicitly not admin unless configured (and config default is viewer).
    p2 = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="r2",
        email="r2@ex.com",
        preferred_username="rolecheck2",
        settings=_settings(),
    )
    assert p2.role == "viewer"


def test_username_collision_gets_suffix(ctx):
    auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="c1",
        email="c1@ex.com",
        preferred_username="dupname",
        settings=_settings(),
    )
    p2 = auth.resolve_or_provision_oidc_user(
        ctx.db,
        issuer="https://idp.example",
        sub="c2",
        email="c2@ex.com",
        preferred_username="dupname",
        settings=_settings(),
    )
    assert p2.username == "dupname_1"
