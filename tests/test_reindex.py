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
