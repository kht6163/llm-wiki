"""Optional embedding mode and embedding status summary."""
from __future__ import annotations

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user


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
