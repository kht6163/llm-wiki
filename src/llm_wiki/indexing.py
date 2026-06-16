"""Reindexing primitives: FTS rows, chunks, link edges, and vector embeddings.

FTS + chunk text + link edges are cheap and run inside the write transaction.
Embedding is slow on CPU, so it runs *after* the commit (off the write lock) via
``embed_doc`` / ``embed_pending``, keeping write-then-search consistent without
holding the SQLite writer.
"""
from __future__ import annotations

import logging
import sqlite3

from . import graph
from .embedding import Embedder
from .markdown_utils import chunk_markdown, extract_links

log = logging.getLogger("llm_wiki.indexing")


def reindex_fts(conn: sqlite3.Connection, doc_id: int, title: str, body: str) -> None:
    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))
    conn.execute(
        "INSERT INTO documents_fts(rowid, title, body) VALUES(?,?,?)",
        (doc_id, title or "", body or ""),
    )


def remove_fts(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))


def rechunk(conn: sqlite3.Connection, doc_id: int, body: str) -> list[tuple[int, str]]:
    """Replace a document's chunks (and drop their vectors). Returns (chunk_id, text)."""
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    if old:
        conn.executemany("DELETE FROM chunk_vectors WHERE chunk_id=?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    out: list[tuple[int, str]] = []
    for ch in chunk_markdown(body):
        cur = conn.execute(
            "INSERT INTO chunks(doc_id, ordinal, heading, text, char_start, char_end, heading_path) "
            "VALUES(?,?,?,?,?,?,?)",
            (doc_id, ch.ordinal, ch.heading, ch.text, ch.char_start, ch.char_end, ch.heading_path),
        )
        assert cur.lastrowid is not None
        out.append((cur.lastrowid, ch.text))
    return out


def clear_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    if old:
        conn.executemany("DELETE FROM chunk_vectors WHERE chunk_id=?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def reindex_links(conn: sqlite3.Connection, doc_id: int, body: str, folder: str) -> None:
    graph.store_links(conn, doc_id, extract_links(body), folder)


def embed_doc(db, embedder: Embedder, doc_id: int) -> None:
    """Compute + upsert vectors for one document's chunks, then clear vector_dirty.
    Runs outside the write transaction that produced the chunks.

    Concurrency: another edit may rechunk this doc between the read below and the
    write. We match by (chunk_id, text) — not id alone, since SQLite reuses rowids
    after a delete — and only write a vector when the chunk's text still matches what
    we embedded. vector_dirty is cleared only when every current chunk was matched;
    otherwise the changed chunks stay dirty for a later embed."""
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT id, text FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)
        ).fetchall()
    embs = embedder.embed_passages([r["text"] for r in rows]) if rows else []
    embedded = {r["id"]: (r["text"], emb) for r, emb in zip(rows, embs, strict=False)}
    with db.writer() as conn:
        current = {
            r["id"]: r["text"]
            for r in conn.execute("SELECT id, text FROM chunks WHERE doc_id=?", (doc_id,))
        }
        all_matched = True
        for cid, ctext in current.items():
            hit = embedded.get(cid)
            if hit is not None and hit[0] == ctext:
                # vec0 doesn't honor INSERT OR REPLACE; delete any prior vector first.
                conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (cid,))
                conn.execute(
                    "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?,?)",
                    (cid, Embedder.serialize(hit[1])),
                )
            else:
                all_matched = False
        if all_matched:  # current set fully (re)embedded — true also when there are no chunks
            conn.execute("UPDATE documents SET vector_dirty=0 WHERE id=?", (doc_id,))


def embed_pending(db, embedder: Embedder, doc_id: int | None = None, batch_size: int = 64) -> int:
    """Embed a single doc, or sweep all docs with vector_dirty=1. Returns the number
    of documents whose vectors were brought up to date.

    The sweep path (doc_id is None, used by reindex) batches the expensive encode
    across *all* dirty chunks, then writes in one transaction. The write is verified
    per document — exactly like embed_doc — so vector_dirty is cleared ONLY for docs
    whose current chunk set was fully embedded with matching (chunk_id, text). This
    prevents a doc created or rechunked mid-sweep (possibly from another process) from
    being marked clean without its vectors and then never retried."""
    if doc_id is not None:
        embed_doc(db, embedder, doc_id)
        return 1

    with db.reader() as conn:
        dirty = [r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE vector_dirty=1 AND is_deleted=0")]
        rows = conn.execute(
            "SELECT c.id AS chunk_id, c.text AS text "
            "FROM chunks c JOIN documents d ON d.id=c.doc_id "
            "WHERE d.vector_dirty=1 AND d.is_deleted=0 ORDER BY c.doc_id, c.ordinal"
        ).fetchall()
    if not dirty:
        return 0
    texts = [r["text"] for r in rows]
    log.info("embed_pending: embedding %d chunk(s) across %d document(s)", len(texts), len(dirty))
    vectors: list = []
    for i in range(0, len(texts), batch_size):
        vectors.extend(embedder.embed_passages(texts[i:i + batch_size]))
    embedded = {r["chunk_id"]: (r["text"], emb) for r, emb in zip(rows, vectors, strict=False)}

    cleared = 0
    with db.writer() as conn:
        for did in dirty:
            current = {
                r["id"]: r["text"]
                for r in conn.execute("SELECT id, text FROM chunks WHERE doc_id=?", (did,))
            }
            all_matched = True
            for cid, ctext in current.items():
                hit = embedded.get(cid)
                if hit is not None and hit[0] == ctext:
                    # vec0 doesn't honor INSERT OR REPLACE; delete any prior vector first.
                    conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (cid,))
                    conn.execute(
                        "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?,?)",
                        (cid, Embedder.serialize(hit[1])),
                    )
                else:
                    all_matched = False
            if all_matched:  # fully (re)embedded — also true for a chunk-less doc
                conn.execute("UPDATE documents SET vector_dirty=0 WHERE id=?", (did,))
                cleared += 1
    log.info("embed_pending: cleared vector_dirty on %d/%d document(s)", cleared, len(dirty))
    return cleared
