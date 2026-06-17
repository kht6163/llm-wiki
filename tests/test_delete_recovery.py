"""Delete is crash-safe: the .md is trashed under a file_state='pending' guard, the
same way create()/update() guard their file write. A crash between the delete commit
and the trash leaves the row pending; recover_pending() finishes the trash on the next
start so an on-disk orphan can't outlive the DB row that marks it deleted."""

from llm_wiki.util import path_norm, safe_join


def _vault_file(docs, rel):
    return safe_join(docs.vault, rel)


def test_normal_delete_trashes_file_and_clears_state(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "d.md", "# D\n\nbody")
    assert _vault_file(docs, "d.md").exists()
    docs.delete(p, "d.md")
    assert not _vault_file(docs, "d.md").exists()          # moved out of the vault
    assert (docs.vault / ".trash" / "d.md").exists()       # into trash
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT file_state FROM documents WHERE path_norm=?",
                            (path_norm("d.md"),)).fetchone()
    assert row["file_state"] == "clean"
    assert docs.recover_pending() == 0                      # nothing left pending


def test_recover_pending_trashes_orphan_from_crashed_delete(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "d.md", "# D\n\nbody")
    # Simulate a crash AFTER the delete commit (is_deleted=1, file_state='pending')
    # but BEFORE _trash_file ran: the file is still sitting in the vault.
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET is_deleted=1, file_state='pending' WHERE path_norm=?",
                     (path_norm("d.md"),))
    assert _vault_file(docs, "d.md").exists()              # the orphan

    n = docs.recover_pending()
    assert n == 1
    assert not _vault_file(docs, "d.md").exists()          # now trashed
    assert (docs.vault / ".trash" / "d.md").exists()
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT file_state FROM documents WHERE path_norm=?",
                            (path_norm("d.md"),)).fetchone()
    assert row["file_state"] == "clean"
    assert docs.recover_pending() == 0                     # idempotent


def test_recover_pending_still_reprojects_live_docs(ctx, principals):
    # The live-doc branch must keep working alongside the new deleted-doc branch.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "live.md", "# Live\n\nbody")
    f = _vault_file(docs, "live.md")
    f.unlink()                                              # simulate a lost projection
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE path_norm=?",
                     (path_norm("live.md"),))
    assert docs.recover_pending() == 1
    assert f.exists() and "body" in f.read_text(encoding="utf-8")
