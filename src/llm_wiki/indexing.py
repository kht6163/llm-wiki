"""Reindexing primitives: FTS rows, chunks, link edges, and vector embeddings.

FTS + chunk text + link edges are cheap and run inside the write transaction.
Embedding is slow on CPU, so it runs *after* the commit (off the write lock) via
``embed_doc`` / ``embed_pending``, keeping write-then-search consistent without
holding the SQLite writer.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable

from . import graph
from .embedding import Embedder
from .markdown_utils import chunk_markdown, extract_links, parse_frontmatter
from .metrics import (
    EMBED_CHUNKS,
    EMBED_DURATION,
    EMBED_WORKER_BUSY,
    EMBED_WORKER_FAILURES,
    EMBED_WORKER_LAST_SUCCESS,
    EMBED_WORKER_RUNS,
)

log = logging.getLogger("llm_wiki.indexing")


def reindex_fts(conn: sqlite3.Connection, doc_id: int, title: str, body: str) -> None:
    # Index body prose only: YAML frontmatter is metadata (title is its own column,
    # tags live in the tags table), so leaving it in just leaks `tags: [...]` / `---`
    # into BM25 snippets. chunk_markdown already strips it, so this aligns the FTS
    # leg with the vector leg. Existing rows pick this up on the next `reindex`.
    text = body or ""
    body = text[parse_frontmatter(text)[1]:]
    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))
    conn.execute(
        "INSERT INTO documents_fts(rowid, title, body) VALUES(?,?,?)",
        (doc_id, title or "", body),
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


def _embed_text(row) -> str:
    """The text actually sent to the embedder for a chunk: its heading breadcrumb
    (``heading_path``, e.g. "Install > Linux") prepended to the body. The stored chunk
    ``text`` — what read_chunk / assemble_context cite — is left unchanged; only the
    embedding INPUT is enriched, so a short code/table chunk inherits its structural
    context in vector space and matches better. Changing this alters vector meaning →
    requires ``reindex --reembed`` (dimension is unchanged, so startup is not refused)."""
    text = row["text"] or ""
    hp = row["heading_path"]
    return f"{hp}\n\n{text}" if hp else text


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
            "SELECT id, text, heading_path FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)
        ).fetchall()
    if rows:
        t0 = time.perf_counter()
        embs = embedder.embed_passages([_embed_text(r) for r in rows])
        EMBED_DURATION.observe(time.perf_counter() - t0)
        EMBED_CHUNKS.inc(len(rows))
    else:
        embs = []
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


def embed_pending(db, embedder: Embedder, doc_id: int | None = None, batch_size: int = 64,
                  progress: Callable[[int, int], None] | None = None) -> int:
    """Embed a single doc, or sweep all docs with vector_dirty=1. Returns the number
    of documents whose vectors were brought up to date. ``progress``, if given, is
    called ``progress(done_chunks, total_chunks)`` after each batch — used by the
    reindex CLI to show a progress line on a long re-embed.

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
            "SELECT c.id AS chunk_id, c.text AS text, c.heading_path AS heading_path "
            "FROM chunks c JOIN documents d ON d.id=c.doc_id "
            "WHERE d.vector_dirty=1 AND d.is_deleted=0 ORDER BY c.doc_id, c.ordinal"
        ).fetchall()
    if not dirty:
        return 0
    texts = [_embed_text(r) for r in rows]
    log.info("embed_pending: embedding %d chunk(s) across %d document(s)", len(texts), len(dirty))
    vectors: list = []
    t0 = time.perf_counter()
    total = len(texts)
    for i in range(0, total, batch_size):
        vectors.extend(embedder.embed_passages(texts[i:i + batch_size]))
        if progress is not None:
            progress(min(i + batch_size, total), total)
    EMBED_DURATION.observe(time.perf_counter() - t0)
    EMBED_CHUNKS.inc(len(texts))
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


class EmbeddingWorker:
    """Background thread that drains ``vector_dirty`` documents off the write path.

    Writers set ``vector_dirty=1`` inside their commit, then call :meth:`notify`; the
    worker wakes and runs :func:`embed_pending` to (re)embed everything still dirty. A
    periodic idle sweep also runs, so documents made dirty without a notify (e.g. an
    external ``reindex``) are still picked up. This keeps the embedding forward pass —
    the slowest CPU step — out of the request that saved the document.

    Used only while serving; tests/CLI leave it None and embed inline so write-then-search
    stays immediately consistent. Anything still dirty at shutdown is embedded by the next
    startup sweep, so a bounded join on :meth:`stop` never loses vectors."""

    def __init__(self, db, embedder: Embedder, *, idle_interval: float = 30.0) -> None:
        self._db = db
        self._embedder = embedder
        self._idle_interval = idle_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="llmwiki-embed", daemon=True)
        # Observable state (read by status() / surfaced on /readyz) so a silently stalled
        # or failing worker is visible rather than just "RAG quietly rotting".
        self._busy = False
        self._failures = 0
        self._last_error: str | None = None
        self._last_duration = 0.0
        self._last_run_at: float | None = None

    def start(self) -> None:
        self._thread.start()

    def notify(self) -> None:
        """Wake the worker to embed freshly-dirtied documents (called after a write)."""
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # An in-flight sweep outran the grace period; it's a daemon thread so
                # the process can still exit, and anything left vector_dirty is embedded
                # by the next startup sweep — but surface it so it isn't a silent stall.
                log.warning("embedding worker still running after %.0fs; "
                            "pending vectors will be re-embedded on next start", timeout)

    def status(self) -> dict:
        """A snapshot of worker health for /readyz and ops: whether a sweep is running,
        how many consecutive failures, the last error/duration, and the current
        vector_dirty backlog (documents still awaiting embedding)."""
        try:
            with self._db.reader() as conn:
                backlog = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE vector_dirty=1 AND is_deleted=0"
                ).fetchone()[0]
        except Exception:
            backlog = None
        return {
            "running": self._busy,
            "consecutive_failures": self._failures,
            "last_error": self._last_error,
            "last_duration_s": round(self._last_duration, 3),
            "backlog": backlog,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wake on notify() or sooner-of-idle_interval; back off (capped) after
            # consecutive failures so a persistent error (OOM, disk full) doesn't
            # busy-spin the logs/CPU.
            wait = self._idle_interval * (2 ** min(self._failures, 5))
            self._wake.wait(wait)
            self._wake.clear()
            if self._stop.is_set():
                break
            self._busy = True
            EMBED_WORKER_BUSY.set(1)
            t0 = time.perf_counter()
            try:
                embed_pending(self._db, self._embedder)
                EMBED_WORKER_RUNS.labels("ok").inc()
                EMBED_WORKER_LAST_SUCCESS.set_to_current_time()
                self._failures = 0
                self._last_error = None
                # Best-effort WAL truncation: a long-lived reader can otherwise keep the
                # -wal file from being reset by autocheckpoint. The worker is the natural
                # periodic hook; TRUNCATE no-ops harmlessly if other readers are active.
                try:
                    with self._db.reader() as conn:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
            except Exception as e:
                self._failures += 1
                self._last_error = str(e)[:200]
                EMBED_WORKER_RUNS.labels("error").inc()
                (log.error if self._failures >= 3 else log.warning)(
                    "embed worker: sweep failed (%d consecutive); backing off", self._failures)
                log.debug("embed worker traceback", exc_info=True)
            finally:
                self._busy = False
                EMBED_WORKER_BUSY.set(0)
                self._last_duration = time.perf_counter() - t0
                self._last_run_at = time.time()
                EMBED_WORKER_FAILURES.set(self._failures)
