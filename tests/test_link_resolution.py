"""Bare-name link resolution must obey same-folder-first both at write time AND on
backfill, and break ties deterministically. A new same-folder note has to claim the
folder-local [[name]] links that previously resolved to a note in another folder."""

from llm_wiki.graph import resolve_path


def _dst_of(ctx, src_path, anchor_name):
    """The path the (single) bare link in src_path currently resolves to, or None."""
    docs = ctx.docs
    src = docs.get(src_path)
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT dst.path AS path FROM links l "
            "JOIN documents s ON s.id=l.src_doc_id "
            "LEFT JOIN documents dst ON dst.id=l.dst_doc_id "
            "WHERE s.path_norm=? AND l.dst_name=? ",
            (src["path"].lower(), anchor_name),
        ).fetchone()
    return row["path"] if row and row["path"] else None


def test_new_same_folder_note_claims_folder_local_link(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    # A root note exists; a folder-local [[A]] resolves to it (no same-folder match yet).
    docs.create(p, "A.md", "# root A")
    docs.create(p, "folder/C.md", "see [[A]]")
    assert _dst_of(ctx, "folder/C.md", "a") == "A.md"
    # Now a same-folder A appears -> folder/C.md's [[A]] must repoint to it.
    docs.create(p, "folder/A.md", "# folder A")
    assert _dst_of(ctx, "folder/C.md", "a") == "folder/A.md"


def test_backfill_respects_source_folder_for_dangling_links(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    # Two dangling [[A]] in different folders, no A anywhere yet.
    docs.create(p, "x/c.md", "ref [[A]]")
    docs.create(p, "y/d.md", "ref [[A]]")
    # Create an A only in folder x: x/c keeps a same-folder target; y/d falls back to it
    # (the only A) — but each is resolved per its OWN folder, not blindly.
    docs.create(p, "x/A.md", "# x A")
    assert _dst_of(ctx, "x/c.md", "a") == "x/A.md"   # same folder
    assert _dst_of(ctx, "y/d.md", "a") == "x/A.md"   # only candidate
    # Add a same-folder A for y -> y/d must repoint; x/c stays on x/A.
    docs.create(p, "y/A.md", "# y A")
    assert _dst_of(ctx, "x/c.md", "a") == "x/A.md"
    assert _dst_of(ctx, "y/d.md", "a") == "y/A.md"


def test_bare_name_tiebreaker_is_deterministic(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    # No same-folder match for the root source -> deterministic tiebreak: shallowest
    # folder first, then path. Both candidates are nested, so 'm/dup' < 'z/dup'.
    docs.create(p, "z/dup.md", "# z")
    docs.create(p, "m/dup.md", "# m")
    with ctx.db.reader() as conn:
        assert resolve_path(conn, "dup", src_folder="") == "m/dup.md"
