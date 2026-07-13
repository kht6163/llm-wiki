"""Reindexing primitives: FTS rows, chunks, link edges, and vector embeddings.

FTS + chunk text + link edges are cheap and run inside the write transaction.
Embedding is slow on CPU, so it runs *after* the commit (off the write lock) via
``embed_doc`` / ``embed_pending``, keeping write-then-search consistent without
holding the SQLite writer.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import graph
from .embedding import Embedder
from .embedding_contract import (
    EMBEDDING_FLOAT32_MAX,
    EmbeddingBinding,
    EmbeddingBindingChanged,
)
from .markdown_utils import Chunk, Link, chunk_markdown, extract_links, parse_frontmatter
from .metrics import (
    EMBED_CHUNKS,
    EMBED_DURATION,
    EMBED_WORKER_BUSY,
    EMBED_WORKER_FAILURES,
    EMBED_WORKER_LAST_SUCCESS,
    EMBED_WORKER_RUNS,
)

log = logging.getLogger("llm_wiki.indexing")


def _vector_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vectors'"
    ).fetchone() is not None


@dataclass(frozen=True)
class PreparedMarkdown:
    fts_body: str
    chunks: tuple[Chunk, ...]
    links: tuple[Link, ...]


def prepare_markdown(body: str) -> PreparedMarkdown:
    """Parse all index inputs outside the SQLite writer lock."""
    text = body or ""
    return PreparedMarkdown(
        text[parse_frontmatter(text)[1]:],
        tuple(chunk_markdown(text)),
        tuple(extract_links(text)),
    )


def publish_prepared(
    conn: sqlite3.Connection,
    doc_id: int,
    title: str,
    folder: str,
    prepared: PreparedMarkdown,
) -> None:
    """Publish already-parsed FTS/chunk/link rows in the caller's transaction."""
    conn.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))
    conn.execute(
        "INSERT INTO documents_fts(rowid, title, body) VALUES(?,?,?)",
        (doc_id, title or "", prepared.fts_body),
    )
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    vectors_enabled = _vector_table_exists(conn)
    if old:
        if vectors_enabled:
            conn.executemany("DELETE FROM chunk_vectors WHERE chunk_id=?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    for chunk in prepared.chunks:
        conn.execute(
            "INSERT INTO chunks(doc_id,ordinal,heading,text,char_start,char_end,heading_path) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                doc_id,
                chunk.ordinal,
                chunk.heading,
                chunk.text,
                chunk.char_start,
                chunk.char_end,
                chunk.heading_path,
            ),
        )
    graph.store_links(conn, doc_id, list(prepared.links), folder)
    if not vectors_enabled:
        conn.execute("UPDATE documents SET vector_dirty=0 WHERE id=?", (doc_id,))


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
    vectors_enabled = _vector_table_exists(conn)
    if old:
        if vectors_enabled:
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
    if not vectors_enabled:
        conn.execute("UPDATE documents SET vector_dirty=0 WHERE id=?", (doc_id,))
    return out


def clear_chunks(conn: sqlite3.Connection, doc_id: int) -> None:
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    if old:
        if _vector_table_exists(conn):
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


def _embedding_snapshot(
    conn: sqlite3.Connection, doc_id: int
) -> tuple[int, tuple[tuple[int, str], ...]] | None:
    """Read the publisher CAS token and ordered passage inputs in one statement."""
    rows = conn.execute(
        "SELECT d.version AS doc_version, c.id AS chunk_id, c.text, c.heading_path "
        "FROM documents d LEFT JOIN chunks c ON c.doc_id=d.id "
        "WHERE d.id=? AND d.vector_dirty=1 AND d.is_deleted=0 "
        "ORDER BY c.ordinal, c.id",
        (doc_id,),
    ).fetchall()
    if not rows:
        return None
    passages = tuple(
        (int(row["chunk_id"]), _embed_text(row))
        for row in rows
        if row["chunk_id"] is not None
    )
    return int(rows[0]["doc_version"]), passages


def _verify_embedder_identity(
    expected: EmbeddingBinding, embedder: Embedder
) -> None:
    actual_identity = (
        embedder.model_name,
        embedder.pipeline,
        str(getattr(embedder, "revision", expected.revision)),
    )
    expected_identity = (expected.model, expected.pipeline, expected.revision)
    if actual_identity != expected_identity:
        raise EmbeddingBindingChanged(
            f"Process embedder {actual_identity} does not match expected binding "
            f"{expected_identity}."
        )
    actual = (
        embedder.model_name,
        int(embedder.dim),
        embedder.pipeline,
        actual_identity[2],
    )
    wanted = (expected.model, expected.dim, expected.pipeline, expected.revision)
    if actual != wanted:
        raise EmbeddingBindingChanged(
            f"Process embedder {actual} does not match expected binding {wanted}."
        )


def embed_doc(
    db,
    embedder: Embedder,
    doc_id: int,
    batch_size: int = 64,
    on_batch: Callable[[int], None] | None = None,
) -> bool:
    """Atomically publish one dirty document's vectors for a stable input snapshot.

    Encoding and serialization happen without a SQLite writer lock. Publication then
    fences the process embedding generation and compares the document version plus
    every ordered passage input in one short writer transaction. A document race is a
    normal ``False`` result; a generation change raises ``EmbeddingBindingChanged`` so
    an old process cannot silently publish into a newly rebound vector table.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    expected = db.expected_embedding_binding()
    _verify_embedder_identity(expected, embedder)
    with db.reader() as conn:
        snapshot = _embedding_snapshot(conn, doc_id)
    if snapshot is None:
        return False
    version, passages = snapshot

    serialized: list[tuple[int, bytes]] = []
    if passages:
        for offset in range(0, len(passages), batch_size):
            batch = passages[offset:offset + batch_size]
            texts = [text for _chunk_id, text in batch]
            EMBED_CHUNKS.inc(len(texts))
            t0 = time.perf_counter()
            try:
                outputs = embedder.embed_passages(texts)
            finally:
                EMBED_DURATION.observe(time.perf_counter() - t0)
            try:
                output_count = len(outputs)
            except TypeError as exc:
                raise ValueError(
                    "embedding output must be a sized sequence of vectors"
                ) from exc
            if output_count != len(batch):
                raise ValueError(
                    "embedding output count does not match passage input count: "
                    f"expected {len(batch)}, got {output_count}"
                )
            for (chunk_id, _text), vector in zip(batch, outputs, strict=True):
                try:
                    values = [float(value) for value in vector]
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "embedding output vector must contain numeric values"
                    ) from exc
                if len(values) != expected.dim:
                    raise ValueError(
                        "embedding output dimension does not match binding: "
                        f"expected {expected.dim}, got {len(values)}"
                    )
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(
                        "embedding output vector must contain only finite values"
                    )
                if any(abs(value) > EMBEDDING_FLOAT32_MAX for value in values):
                    raise ValueError(
                        "embedding output vector values must fit the float32 range"
                    )
                serialized.append((chunk_id, Embedder.serialize(values)))
            if on_batch is not None:
                on_batch(len(batch))

    with db.writer() as conn:
        db.verify_embedding_binding(conn, expected)
        if _embedding_snapshot(conn, doc_id) != snapshot:
            return False
        updated = conn.execute(
            "UPDATE documents SET vector_dirty=0 "
            "WHERE id=? AND version=? AND vector_dirty=1 AND is_deleted=0",
            (doc_id, version),
        )
        if updated.rowcount != 1:
            return False
        if passages:
            conn.executemany(
                "DELETE FROM chunk_vectors WHERE chunk_id=?",
                [(chunk_id,) for chunk_id, _text in passages],
            )
            conn.executemany(
                "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?,?)",
                serialized,
            )
    return True


def embed_pending(
    db,
    embedder: Embedder,
    doc_id: int | None = None,
    batch_size: int = 64,
    progress: Callable[[int, int], None] | None = None,
    doc_batch_size: int = 64,
) -> int:
    """Embed a single doc, or sweep all docs with vector_dirty=1. Returns the number
    of documents whose vectors were brought up to date. ``progress``, if given, is
    called ``progress(done_chunks, total_chunks)`` when snapshot progress advances
    after a batch and once at completion — used by the reindex CLI to show a progress
    line on a long re-embed.

    A full sweep snapshots only scalar progress bounds, then keyset-pages document IDs.
    Each document is encoded and atomically published independently through
    :func:`embed_doc`; a document race therefore stays dirty without starving later
    IDs, while an encoder or binding error stops the sweep without rolling back prior
    document commits."""
    if not getattr(embedder, "enabled", True):
        return 0
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if doc_batch_size <= 0:
        raise ValueError("doc_batch_size must be positive")

    if doc_id is not None and progress is None:
        return int(embed_doc(db, embedder, doc_id, batch_size=batch_size))

    with db.reader() as conn:
        params: tuple[int, ...] = () if doc_id is None else (doc_id,)
        doc_filter = "" if doc_id is None else " AND d.id=?"
        if progress is None:
            stats = conn.execute(
                "SELECT COALESCE(MAX(d.id), 0) AS max_id, "
                "COUNT(*) AS doc_count FROM documents d "
                f"WHERE d.vector_dirty=1 AND d.is_deleted=0{doc_filter}",
                params,
            ).fetchone()
        else:
            stats = conn.execute(
                "SELECT COALESCE(MAX(d.id), 0) AS max_id, COUNT(*) AS doc_count, "
                "COALESCE(SUM((SELECT COUNT(*) FROM chunks c "
                "WHERE c.doc_id=d.id)), 0) AS chunk_count "
                "FROM documents d "
                f"WHERE d.vector_dirty=1 AND d.is_deleted=0{doc_filter}",
                params,
            ).fetchone()

    max_id = int(stats["max_id"] or 0)
    target_docs = int(stats["doc_count"])
    total_chunks = int(stats["chunk_count"]) if progress is not None else 0
    done_chunks = 0
    last_progress: tuple[int, int] | None = None

    def on_batch(count: int) -> None:
        nonlocal done_chunks, last_progress
        done_chunks = min(done_chunks + count, total_chunks)
        next_progress = (done_chunks, total_chunks)
        if progress is not None and next_progress != last_progress:
            last_progress = next_progress
            progress(*next_progress)

    def finish_progress() -> None:
        complete = (total_chunks, total_chunks)
        if progress is not None and last_progress != complete:
            progress(*complete)

    if doc_id is not None:
        published = embed_doc(
            db,
            embedder,
            doc_id,
            batch_size=batch_size,
            on_batch=on_batch if progress is not None else None,
        )
        finish_progress()
        return int(published)

    if not target_docs:
        finish_progress()
        return 0

    if progress is None:
        log.info("embed_pending: embedding %d document(s)", target_docs)
    else:
        log.info(
            "embed_pending: embedding %d chunk(s) across %d document(s)",
            total_chunks,
            target_docs,
        )
    cleared = 0
    last_id = 0
    while last_id < max_id:
        with db.reader() as conn:
            page = tuple(
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM documents "
                    "WHERE vector_dirty=1 AND is_deleted=0 AND id>? AND id<=? "
                    "ORDER BY id LIMIT ?",
                    (last_id, max_id, doc_batch_size),
                ).fetchall()
            )
        if not page:
            break
        for pending_id in page:
            last_id = pending_id
            if embed_doc(
                db,
                embedder,
                pending_id,
                batch_size=batch_size,
                on_batch=on_batch if progress is not None else None,
            ):
                cleared += 1

    finish_progress()
    log.info(
        "embed_pending: cleared vector_dirty on %d/%d document(s)",
        cleared,
        target_docs,
    )
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
