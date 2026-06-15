"""Embedding maintenance (batch B): vectors land for the current chunk set, and the
dirty flag survives a concurrent rechunk so the new chunks are not orphaned."""
from llm_wiki import indexing


def _doc_id(ctx, norm):
    with ctx.db.reader() as conn:
        return conn.execute("SELECT id FROM documents WHERE path_norm=?", (norm,)).fetchone()[0]


def _dirty(ctx, doc_id):
    with ctx.db.reader() as conn:
        return conn.execute("SELECT vector_dirty FROM documents WHERE id=?", (doc_id,)).fetchone()[0]


def test_create_embeds_every_chunk(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "v.md", "# T\n\n" + ("word " * 80))
    doc_id = _doc_id(ctx, "v.md")
    with ctx.db.reader() as conn:
        chunks = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
        ph = ",".join("?" * len(chunks))
        vecs = [r[0] for r in conn.execute(
            f"SELECT chunk_id FROM chunk_vectors WHERE chunk_id IN ({ph})", chunks)]
    assert chunks and set(vecs) == set(chunks)
    assert _dirty(ctx, doc_id) == 0


def test_embed_doc_stays_dirty_when_chunks_change_under_it(ctx, principals, monkeypatch):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "race.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, "race.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))

    real = ctx.embedder.embed_passages

    def racing_embed(texts):
        out = real(texts)
        # A concurrent edit replaces this doc's chunks (new ids) mid-encode.
        with ctx.db.writer() as conn:
            indexing.rechunk(conn, doc_id, "# T\n\ncompletely different beta gamma delta")
        return out

    monkeypatch.setattr(ctx.embedder, "embed_passages", racing_embed)
    indexing.embed_doc(ctx.db, ctx.embedder, doc_id)
    # The chunk set we embedded no longer matches the current one, so the flag is
    # left set for a later sweep — the new chunks are not silently left unembedded.
    assert _dirty(ctx, doc_id) == 1


def test_embed_pending_sweep_clears_dirty(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "s.md", "# S\n\nbody words here")
    doc_id = _doc_id(ctx, "s.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))
    n = indexing.embed_pending(ctx.db, ctx.embedder)
    assert n >= 1
    assert _dirty(ctx, doc_id) == 0


def test_embed_pending_skips_doc_rechunked_mid_sweep(ctx, principals, monkeypatch):
    # The batch sweep must not mark a doc clean if its chunks changed under it; that
    # doc's vectors would be stale/missing and it would never be retried.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "A.md", "# A\n\nalpha alpha alpha")
    docs.create(p, "B.md", "# B\n\nbeta beta beta")
    a_id, b_id = _doc_id(ctx, "a.md"), _doc_id(ctx, "b.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id IN (?,?)", (a_id, b_id))

    real = ctx.embedder.embed_passages
    fired = {"once": False}

    def racing_embed(texts):
        out = real(texts)
        if not fired["once"]:
            fired["once"] = True
            with ctx.db.writer() as conn:
                indexing.rechunk(conn, a_id, "# A\n\ntotally different gamma delta epsilon")
        return out

    monkeypatch.setattr(ctx.embedder, "embed_passages", racing_embed)
    indexing.embed_pending(ctx.db, ctx.embedder)
    assert _dirty(ctx, a_id) == 1  # rechunked mid-sweep -> left dirty for a later sweep
    assert _dirty(ctx, b_id) == 0  # cleanly embedded -> cleared
