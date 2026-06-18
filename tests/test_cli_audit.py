"""CLI credential commands (create-admin/create-user/create-api-key) write an audit
row, mirroring the web admin path's trail (action=user_create/key_mint). Without this
the CLI could silently create or promote an account — a gap when investigating "who
created this admin / minted this key" on a host where only the CLI was used."""
from types import SimpleNamespace

from llm_wiki import _cli_impl
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _ctx(tmp_path):
    s = Settings(
        vault_path=tmp_path / "vault", db_path=tmp_path / "data" / "wiki.db",
        embedding_model=TEST_MODEL, gui_port=8190, mcp_port=8191, session_secret="x")
    return build_context(s, full=True)


def _rows(ctx, action):
    with ctx.db.reader() as conn:
        return conn.execute(
            "SELECT actor, via, action, target, detail FROM audit_log WHERE action=? ORDER BY id",
            (action,)).fetchall()


def test_create_admin_audited(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._create_admin(
        SimpleNamespace(username="root", password="secret12", force=False))
    assert rc == 0
    rows = _rows(ctx, "user_create")
    assert len(rows) == 1
    r = rows[0]
    assert r["via"] == "cli" and r["target"] == "root" and r["detail"] == "role=admin"
    assert r["actor"].startswith("cli:") or r["actor"] == "-"


def test_create_user_audited(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    _cli_impl._create_admin(SimpleNamespace(username="root", password="secret12", force=False))
    rc = _cli_impl._create_user(
        SimpleNamespace(username="ed", password="secret12", role="editor"))
    assert rc == 0
    targets = {r["target"]: r["detail"] for r in _rows(ctx, "user_create")}
    assert targets == {"root": "role=admin", "ed": "role=editor"}


def test_create_api_key_audited(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    _cli_impl._create_admin(SimpleNamespace(username="root", password="secret12", force=False))
    rc = _cli_impl._create_api_key(SimpleNamespace(username="root", name="laptop"))
    assert rc == 0
    rows = _rows(ctx, "key_mint")
    assert len(rows) == 1
    assert rows[0]["via"] == "cli" and rows[0]["target"] == "laptop"
    assert rows[0]["detail"] == "user=root"


def test_create_api_key_unknown_user_not_audited(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._create_api_key(SimpleNamespace(username="ghost", name="laptop"))
    assert rc == 1
    assert _rows(ctx, "key_mint") == []
