"""External-edit reconciliation + crash recovery (batch B). These paths had no
coverage and guard the "DB is canonical; .md is a projection" invariant."""
from types import SimpleNamespace

import pytest

from llm_wiki import _cli_impl, indexing
from llm_wiki.db import Database
from llm_wiki.util import path_norm


def _write(ctx, rel, text):
    p = ctx.settings.vault_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_reindex_create_update_unchanged_missing(ctx, principals):
    docs = ctx.docs
    _write(ctx, "ext.md", "# Ext\n\nhello")
    res = docs.reindex_all()
    assert res["created"] == 1
    assert "hello" in docs.get("ext.md")["content"]

    _write(ctx, "ext.md", "# Ext\n\nhello world")
    res = docs.reindex_all()
    assert res["updated"] == 1
    ops = {r["op"] for r in docs.revisions("ext.md")["revisions"]}
    assert "external-reconcile" in ops

    res = docs.reindex_all()  # no-op run
    assert res["unchanged"] >= 1

    # A DB document whose on-disk file disappears is reported, not deleted.
    docs.create(principals["editor"], "db_only.md", "body")
    (ctx.settings.vault_path / "db_only.md").unlink()
    res = docs.reindex_all()
    assert "db_only.md" in res["missing_files"]


def test_reindex_rejects_invalid_utf8_without_adopting_or_mutating(ctx):
    invalid = ctx.settings.vault_path / "invalid.md"
    original = b"valid prefix\xffinvalid suffix"
    invalid.write_bytes(original)

    report = ctx.docs.reindex_all()

    assert report["created"] == 0
    assert any(
        item["path"] == "invalid.md" and item["reason"] == "invalid_encoding"
        for item in report["skipped_conflicts"]
    )
    assert not ctx.docs.exists("invalid.md")
    assert invalid.read_bytes() == original


def test_reindex_treats_external_rename_as_move(ctx, principals):
    import os
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "old.md", "# Note\n\nunique body content here")
    v = ctx.settings.vault_path
    os.replace(v / "old.md", v / "new.md")  # external `mv old.md new.md`
    res = docs.reindex_all()
    assert res["renamed"] == 1 and res["created"] == 0
    assert "old.md" not in res["missing_files"]      # it moved, it didn't vanish
    assert docs.exists("new.md") and not docs.exists("old.md")
    moved = docs.get("new.md")
    assert "unique body content here" in moved["content"]
    # Same document continued (create + rename revisions), not a fresh duplicate.
    ops = [r["op"] for r in docs.revisions("new.md")["revisions"]]
    assert "create" in ops and "rename" in ops


def test_reindex_ambiguous_rename_falls_back_to_create(ctx, principals):
    import os
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "duplicate body")
    docs.create(p, "b.md", "duplicate body")  # identical content -> identical hash
    v = ctx.settings.vault_path
    os.replace(v / "a.md", v / "c.md")  # a's exact file reappears as c.md
    os.remove(v / "b.md")
    res = docs.reindex_all()
    # Two DB docs share that content hash, so the move target is ambiguous; rather than
    # relocate the wrong one, c.md is created fresh and both originals report missing.
    assert res["renamed"] == 0 and res["created"] == 1
    assert set(res["missing_files"]) >= {"a.md", "b.md"}


def test_reindex_does_not_revive_soft_deleted(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "gone.md", "body one")
    docs.delete(p, "gone.md")
    assert not docs.exists("gone.md")
    # An external .md reappears at the same path during reindex.
    _write(ctx, "gone.md", "body two")
    res = docs.reindex_all()
    assert "gone.md" in res["skipped_deleted"]
    assert not docs.exists("gone.md")  # the tombstone wins; not silently revived


def test_recover_pending_reprojects_file(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "rp.md", "durable body")
    f = ctx.settings.vault_path / "rp.md"
    # Simulate a crash between commit and file write: pending + missing file.
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE path_norm=?", (path_norm("rp.md"),))
    f.unlink()

    assert docs.recover_pending() == 1
    assert f.exists() and "durable body" in f.read_text(encoding="utf-8")
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE path_norm=?", (path_norm("rp.md"),)
        ).fetchone()[0]
    assert state == "clean"


def test_reindex_records_audit_for_reconcile(ctx, principals):
    # External reconciliation is a silent batch op; it must leave an audit trail like
    # every other write path (one row per created/updated file).
    docs = ctx.docs
    _write(ctx, "ext.md", "# Ext\n\nhello")
    docs.reindex_all()                       # external create
    _write(ctx, "ext.md", "# Ext\n\nhello world")
    docs.reindex_all()                       # external update
    with ctx.db.reader() as conn:
        actions = [r[0] for r in conn.execute(
            "SELECT action FROM audit_log WHERE target=? AND action='doc_reconcile' ORDER BY id",
            ("ext.md",))]
    assert len(actions) == 2  # one create-reconcile + one update-reconcile


def test_reindex_records_audit_for_skipped_deleted(ctx, principals):
    # A tombstoned doc whose file reappears is intentionally skipped — and that skip
    # must be auditable so an operator can see why the file was ignored.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "gone.md", "body one")
    docs.delete(p, "gone.md")
    _write(ctx, "gone.md", "body two")  # external file reappears at the tombstoned path
    docs.reindex_all()
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT outcome FROM audit_log WHERE target=? AND action='doc_reconcile_skip'",
            ("gone.md",)).fetchone()
    assert row is not None and row["outcome"] == "skipped"


def test_embed_pending_reembeds_crash_dirtied_docs(ctx, principals):
    # Simulate a crash that committed a write (vector_dirty=1) but died before the
    # post-commit embed: the doc has chunks but no vectors. The startup sweep must
    # bring it back into vector search.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "vec.md", "# Vec\n\nsome searchable words here")
    norm = path_norm("vec.md")
    with ctx.db.writer() as conn:
        doc_id = conn.execute("SELECT id FROM documents WHERE path_norm=?", (norm,)).fetchone()[0]
        cids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        for cid in cids:
            conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (cid,))
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))

    assert cids  # sanity: the doc produced chunks
    assert ctx.docs.embed_pending() == 1
    with ctx.db.reader() as conn:
        dirty = conn.execute("SELECT vector_dirty FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
        nvec = conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE doc_id=?)", (doc_id,)).fetchone()[0]
    assert dirty == 0 and nvec == len(cids)


def test_startup_sweep_recovers_rebind_interruption_without_vault_files(
    ctx, principals
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "present.md", "# Present\n\nsearchable present body")
    docs.create(editor, "db-only.md", "# DB only\n\nsearchable database body")
    missing_file = ctx.settings.vault_path / "db-only.md"
    missing_file.unlink()

    # Model migration committed, then the process stopped before reindex/re-embed.
    ctx.db.rebind_model(
        ctx.settings.embedding_model, ctx.embedder.dim, ctx.embedder.pipeline
    )
    with ctx.db.reader() as conn:
        interrupted = conn.execute(
            "SELECT COUNT(*) AS live_docs, SUM(vector_dirty) AS dirty_docs "
            "FROM documents WHERE is_deleted=0"
        ).fetchone()
        assert interrupted["live_docs"] == 2
        assert interrupted["dirty_docs"] == 2
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0

    # A new serving process validates the binding and runs only its startup dirty
    # sweep. It must not depend on reindex_all() or the projected Markdown file.
    restarted_db = Database(ctx.settings.db_path)
    restarted_db.initialize(
        ctx.settings.embedding_model, ctx.embedder.dim, ctx.embedder.pipeline
    )
    assert indexing.embed_pending(restarted_db, ctx.embedder) == 2

    with restarted_db.reader() as conn:
        recovered = conn.execute(
            "SELECT d.path, d.vector_dirty, COUNT(c.id) AS chunks, "
            "COUNT(v.chunk_id) AS vectors "
            "FROM documents d "
            "LEFT JOIN chunks c ON c.doc_id=d.id "
            "LEFT JOIN chunk_vectors v ON v.chunk_id=c.id "
            "WHERE d.is_deleted=0 GROUP BY d.id ORDER BY d.id"
        ).fetchall()
    assert [(row["path"], row["vector_dirty"]) for row in recovered] == [
        ("present.md", 0),
        ("db-only.md", 0),
    ]
    assert all(
        row["chunks"] > 0 and row["vectors"] == row["chunks"] for row in recovered
    )
    assert not missing_file.exists()


def test_rebind_model_recreates_vector_table_at_new_dim(ctx):
    # rebind_model is the supported EMBEDDING_MODEL migration: it drops + recreates the
    # vector table at the new dimension and updates the meta binding (initialize() refuses
    # a model change to protect the fixed dim).
    from llm_wiki.db import get_meta
    from llm_wiki.embedding import Embedder

    ctx.db.rebind_model("fake/other-model", 16, ctx.embedder.pipeline)
    with ctx.db.reader() as conn:
        assert get_meta(conn, "embedding_model") == "fake/other-model"
        assert get_meta(conn, "embedding_dim") == "16"
    # Proof the table was recreated at dim 16: a 16-float vector inserts cleanly (on the
    # original 384-dim table this would fail, so success means the rebind took effect).
    with ctx.db.writer() as conn:
        conn.execute("INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(1, ?)",
                     (Embedder.serialize([0.1] * 16),))
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1


def test_cli_reindex_reembed_passes_current_embedding_pipeline(ctx, monkeypatch):
    original_rebind = ctx.db.rebind_model
    rebind_args = []
    build_args = []

    def rebind(model, dim, pipeline, revision):
        rebind_args.append((model, dim, pipeline, revision))
        return original_rebind(model, dim, pipeline, revision)

    def build_context(**kwargs):
        build_args.append(kwargs)
        return ctx

    monkeypatch.setattr(ctx.db, "rebind_model", rebind)
    monkeypatch.setattr(_cli_impl, "build_context", build_context)

    assert _cli_impl._reindex(SimpleNamespace(reembed=True)) == 0
    assert build_args == [{"full": False}]
    assert rebind_args == [
        (
            ctx.settings.embedding_model,
            ctx.embedder.dim,
            ctx.embedder.pipeline,
            ctx.embedder.revision,
        )
    ]


def test_cli_reindex_prints_conflicts_and_returns_one(monkeypatch, capsys):
    report = {
        "created": 0,
        "updated": 1,
        "renamed": 0,
        "unchanged": 0,
        "embedded": 1,
        "renames": [],
        "recovered_pending": 2,
        "retried": 3,
        "missing_files": [],
        "skipped_deleted": [],
        "skipped_conflicts": [
            {"path": "race.md", "reason": "file_changed", "attempts": 3}
        ],
    }
    docs = SimpleNamespace(reindex_all=lambda **_kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **_kwargs: SimpleNamespace(docs=docs),
    )

    result = _cli_impl._reindex(SimpleNamespace(reembed=False))

    output = capsys.readouterr().out
    assert result == 1
    assert "recovered_pending=2 retried=3 conflicts=1" in output
    assert "race.md" in output
    assert "file_changed" in output
    assert "attempts=3" in output


def test_cli_reindex_missing_and_deleted_warnings_still_return_zero(
    monkeypatch, capsys
):
    report = {
        "created": 0,
        "updated": 0,
        "renamed": 0,
        "unchanged": 0,
        "embedded": 0,
        "renames": [],
        "recovered_pending": 0,
        "retried": 0,
        "missing_files": ["missing.md"],
        "skipped_deleted": ["deleted.md"],
        "skipped_conflicts": [],
    }
    docs = SimpleNamespace(reindex_all=lambda **_kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **_kwargs: SimpleNamespace(docs=docs),
    )

    result = _cli_impl._reindex(SimpleNamespace(reembed=False))

    output = capsys.readouterr().out
    assert result == 0
    assert "missing.md" in output
    assert "deleted.md" in output


def test_reindex_reembed_after_rebind_repopulates_vectors(ctx, principals):
    # The `reindex --reembed` flow rebinds the model (drops + recreates the vector table)
    # then re-embeds every document. Simulate it here with the SAME model (no dim change):
    # the re-embed must not crash and must repopulate chunk_vectors.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "v.md", "# Vec\n\nsearchable 문서 본문 내용 here")
    ctx.db.rebind_model(
        ctx.settings.embedding_model, ctx.embedder.dim, ctx.embedder.pipeline
    )
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0  # dropped
    res = docs.reindex_all(reembed=True)
    assert res["embedded"] >= 1
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] >= 1  # repopulated


def test_initialize_refuses_model_change_but_rebind_recovers(ctx, principals):
    # Regression: changing EMBEDDING_MODEL must have a WORKING recovery. The serve/init
    # path (initialize) refuses a model change to protect the fixed vector dim — that's
    # the crash an operator hit when following the docs before the rebind path existed.
    # `reindex --reembed`'s rebind is the fix.
    from llm_wiki.db import get_meta

    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "doc.md", "# D\n\nbody content 본문")
    # Simulate the DB having been bound to a DIFFERENT model previously.
    with ctx.db.writer() as conn:
        conn.execute("UPDATE meta SET v=? WHERE k='embedding_model'", ("prev/model",))
    with pytest.raises(RuntimeError):
        ctx.db.initialize(ctx.settings.embedding_model, ctx.embedder.dim)
    # rebind + reembed recovers: the binding flips to the configured model and vectors rebuild.
    ctx.db.rebind_model(
        ctx.settings.embedding_model, ctx.embedder.dim, ctx.embedder.pipeline
    )
    res = docs.reindex_all(reembed=True)
    assert res["embedded"] >= 1
    with ctx.db.reader() as conn:
        assert get_meta(conn, "embedding_model") == ctx.settings.embedding_model


@pytest.mark.parametrize("reembed", [False, True])
def test_reindex_reembed_flag_runs(ctx, reembed):
    docs = ctx.docs
    _write(ctx, "r.md", "# R\n\nsome words to embed")
    docs.reindex_all()
    before = docs.get("r.md")
    res = docs.reindex_all(reembed=reembed)
    after = docs.get("r.md")
    # Re-embedding is index maintenance: it refreshes vectors without manufacturing a
    # user-visible edit revision or changing recency metadata.
    if reembed:
        assert res["unchanged"] >= 1
        assert res["embedded"] >= 1
        assert (after["version"], after["updated_at"]) == (
            before["version"],
            before["updated_at"],
        )
    else:
        assert res["unchanged"] >= 1
