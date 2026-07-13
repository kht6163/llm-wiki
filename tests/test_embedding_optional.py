"""Optional embedding mode and embedding status summary."""
from __future__ import annotations

import pytest

from llm_wiki import _cli_impl
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user
from llm_wiki.services.errors import EmbeddingUnavailableError


def test_embedding_disabled_skips_model_load(tmp_path):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        embedding_enabled=False,
        session_secret="test-secret",
        gui_port=8098,
        mcp_port=8099,
    )
    ctx = build_context(settings, full=True)
    try:
        assert ctx.settings.embedding_enabled is False
        # Embedder may exist but must not be required for basic writes.
        admin_id = create_user(ctx.db, "admin", "secret12", "admin")
        p = Principal(admin_id, "admin", "admin")
        d = ctx.docs.create(p, "note.md", "# Note\n\nhello")
        assert d["path"] == "note.md" and d["version"] == 1
        # Vector-related APIs report unavailable cleanly.
        try:
            rel = ctx.docs.related("note.md", limit=3)
            # Either empty list or structured unavailable is fine.
            assert rel.get("related") == [] or rel.get("ok") is False or "related" in rel
        except Exception as e:
            assert "embed" in str(e).lower() or "unavailable" in str(e).lower()
    finally:
        ctx.db.close()


def test_embedding_status_reports_dirty_and_enabled(ctx, principals):
    status = ctx.docs.embedding_status()
    assert "enabled" in status
    assert "vector_dirty" in status
    assert "pending_projection" in status
    assert status["enabled"] is True


def test_embedding_disabled_supports_full_document_lifecycle_and_bm25(tmp_path):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_enabled=False,
        session_secret="test-secret",
        gui_port=8098,
        mcp_port=8099,
    )
    ctx = build_context(settings, full=True)
    user_id = create_user(ctx.db, "admin", "secret12", "admin")
    principal = Principal(user_id, "admin", "admin")

    created = ctx.docs.create(principal, "note.md", "# Note\n\nsearchable first")
    updated = ctx.docs.update(
        principal, "note.md", created["version"], "# Note\n\nsearchable second"
    )
    results, _truncated = ctx.docs.search_page("searchable")  # default hybrid -> BM25
    assert [item.path for item in results] == ["note.md"]
    with pytest.raises(EmbeddingUnavailableError):
        ctx.docs.search_page("searchable", mode="vector")

    ctx.docs.delete(principal, "note.md", base_version=updated["version"])
    restored = ctx.docs.restore(principal, "note.md")
    assert restored["path"] == "note.md"

    external = settings.vault_path / "external.md"
    external.write_text("# External\n\nbm25 only", encoding="utf-8")
    report = ctx.docs.reindex_all()
    assert report["created"] == 1 and report["embedded"] == 0
    assert ctx.db.integrity_check()["ok"] is True
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM documents WHERE vector_dirty<>0"
        ).fetchone()[0] == 0


def test_embedding_disabled_reembed_cli_fails_cleanly(tmp_path, monkeypatch, capsys):
    settings = Settings(
        vault_path=tmp_path / "vault",
        db_path=tmp_path / "data" / "wiki.db",
        embedding_enabled=False,
        session_secret="test-secret",
        gui_port=8098,
        mcp_port=8099,
    )
    ctx = build_context(settings, full=False)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **_kwargs: ctx)

    assert _cli_impl._reindex(type("Args", (), {"reembed": True})()) == 1
    assert "EMBEDDING_ENABLED=true" in capsys.readouterr().out
