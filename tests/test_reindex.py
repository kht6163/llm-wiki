"""External-edit reconciliation + crash recovery (batch B). These paths had no
coverage and guard the "DB is canonical; .md is a projection" invariant."""
import pytest

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


@pytest.mark.parametrize("reembed", [False, True])
def test_reindex_reembed_flag_runs(ctx, reembed):
    docs = ctx.docs
    _write(ctx, "r.md", "# R\n\nsome words to embed")
    docs.reindex_all()
    res = docs.reindex_all(reembed=reembed)
    # With reembed, the unchanged file is still re-processed (updated), not skipped.
    if reembed:
        assert res["updated"] >= 1
    else:
        assert res["unchanged"] >= 1
