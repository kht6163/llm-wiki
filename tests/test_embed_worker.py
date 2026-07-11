"""Background embedding worker: when attached (serving), a write defers the slow
forward pass to the worker and only flags the document vector_dirty; the worker sweep
clears it. Without a worker (the default in tests/CLI), embedding stays inline so a
write is immediately visible to vector search."""
import time

import pytest

from llm_wiki import indexing
from llm_wiki.services.documents import DocumentService


def _dirty(db, path):
    with db.reader() as conn:
        r = conn.execute("SELECT vector_dirty FROM documents WHERE path=?", (path,)).fetchone()
    return r[0] if r else None


def _vector_counts(db, paths):
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT d.path, COUNT(c.id) AS chunks, COUNT(v.chunk_id) AS vectors "
            "FROM documents d "
            "LEFT JOIN chunks c ON c.doc_id=d.id "
            "LEFT JOIN chunk_vectors v ON v.chunk_id=c.id "
            f"WHERE d.path IN ({','.join('?' for _ in paths)}) "
            "GROUP BY d.id",
            paths,
        ).fetchall()
    return {row["path"]: (row["chunks"], row["vectors"]) for row in rows}


def test_worker_defers_embedding_and_flags_dirty(ctx, principals, monkeypatch):
    # With a worker attached, create() notifies it instead of embedding inline.
    calls = {"embed_doc": 0, "notify": 0}
    monkeypatch.setattr(indexing, "embed_doc",
                        lambda *a, **k: calls.__setitem__("embed_doc", calls["embed_doc"] + 1))

    class FakeWorker:
        def notify(self):
            calls["notify"] += 1

    docs = DocumentService(ctx.db, ctx.embedder, ctx.settings.vault_path, embed_worker=FakeWorker())
    docs.create(principals["editor"], "deferred.md", "# D\n\n" + "alpha " * 40)
    assert calls["notify"] == 1 and calls["embed_doc"] == 0
    assert _dirty(ctx.db, "deferred.md") == 1  # left for the worker to drain


def test_real_worker_sweeps_dirty(ctx, principals):
    # The real thread: a deferred write is embedded by the worker, clearing vector_dirty.
    worker = indexing.EmbeddingWorker(ctx.db, ctx.embedder, idle_interval=0.05)
    docs = DocumentService(ctx.db, ctx.embedder, ctx.settings.vault_path, embed_worker=worker)
    worker.start()
    try:
        docs.create(principals["editor"], "sweep.md", "# S\n\n" + "beta " * 60)
        deadline = time.time() + 15
        while time.time() < deadline and _dirty(ctx.db, "sweep.md") != 0:
            time.sleep(0.1)
        assert _dirty(ctx.db, "sweep.md") == 0
    finally:
        worker.stop()


def test_worker_sweeps_backlog_across_multiple_document_pages(
    ctx, principals, monkeypatch
):
    doc_ids = []
    for index in range(5):
        path = f"worker-page-{index}.md"
        ctx.docs.create(
            principals["editor"], path, f"# P{index}\n\nworker backlog {index}"
        )
        with ctx.db.reader() as conn:
            doc_ids.append(
                conn.execute(
                    "SELECT id FROM documents WHERE path=?", (path,)
                ).fetchone()[0]
            )
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM chunk_vectors WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE doc_id IN (?,?,?,?,?))",
            doc_ids,
        )
        conn.execute(
            "UPDATE documents SET vector_dirty=1 WHERE id IN (?,?,?,?,?)", doc_ids
        )

    real_embed_pending = indexing.embed_pending

    def sweep_with_small_pages(db, embedder):
        return real_embed_pending(db, embedder, doc_batch_size=2)

    monkeypatch.setattr(indexing, "embed_pending", sweep_with_small_pages)
    worker = indexing.EmbeddingWorker(ctx.db, ctx.embedder, idle_interval=0.05)
    worker.start()
    try:
        worker.notify()
        deadline = time.time() + 15
        while time.time() < deadline:
            with ctx.db.reader() as conn:
                dirty = conn.execute(
                    "SELECT COUNT(*) FROM documents "
                    "WHERE id IN (?,?,?,?,?) AND vector_dirty=1",
                    doc_ids,
                ).fetchone()[0]
            if dirty == 0:
                break
            time.sleep(0.1)
        assert dirty == 0
    finally:
        worker.stop()


def test_rebind_partial_startup_sweep_is_resumed_by_next_sweep(
    ctx, principals, monkeypatch
):
    paths = [f"rebind-retry-{index}.md" for index in range(3)]
    for index, path in enumerate(paths):
        ctx.docs.create(
            principals["editor"], path, f"# Retry {index}\n\nrecoverable body {index}"
        )

    ctx.db.rebind_model(
        ctx.settings.embedding_model, ctx.embedder.dim, ctx.embedder.pipeline
    )
    real_embed_passages = ctx.embedder.embed_passages
    calls = 0

    def fail_second_document(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted second document")
        return real_embed_passages(texts)

    monkeypatch.setattr(ctx.embedder, "embed_passages", fail_second_document)
    with pytest.raises(RuntimeError, match="interrupted second document"):
        ctx.docs.embed_pending()

    assert [_dirty(ctx.db, path) for path in paths] == [0, 1, 1]
    after_failure = _vector_counts(ctx.db, paths)
    assert after_failure[paths[0]][0] == after_failure[paths[0]][1] > 0
    assert after_failure[paths[1]][1] == after_failure[paths[2]][1] == 0

    monkeypatch.setattr(ctx.embedder, "embed_passages", real_embed_passages)
    assert ctx.docs.embed_pending() == 2
    assert [_dirty(ctx.db, path) for path in paths] == [0, 0, 0]
    after_retry = _vector_counts(ctx.db, paths)
    assert all(
        chunks == vectors and vectors > 0 for chunks, vectors in after_retry.values()
    )


def test_default_context_embeds_inline(ctx, principals):
    # The default build_context has no worker -> inline embedding -> not dirty after write.
    ctx.docs.create(principals["editor"], "inline.md", "# I\n\n" + "gamma " * 40)
    assert _dirty(ctx.db, "inline.md") == 0


def test_worker_records_health_metric(ctx, principals):
    # A successful sweep bumps the ok counter — the signal that a backgrounded worker
    # is alive and succeeding (silent stall = silently rotting RAG otherwise).
    from llm_wiki.metrics import EMBED_WORKER_RUNS
    before = EMBED_WORKER_RUNS.labels("ok")._value.get()
    worker = indexing.EmbeddingWorker(ctx.db, ctx.embedder, idle_interval=0.05)
    docs = DocumentService(ctx.db, ctx.embedder, ctx.settings.vault_path, embed_worker=worker)
    worker.start()
    try:
        docs.create(principals["editor"], "metric.md", "# M\n\n" + "alpha " * 60)
        deadline = time.time() + 15
        while time.time() < deadline and EMBED_WORKER_RUNS.labels("ok")._value.get() <= before:
            time.sleep(0.1)
        assert EMBED_WORKER_RUNS.labels("ok")._value.get() > before
    finally:
        worker.stop()


def test_db_sets_wal_autocheckpoint(ctx):
    # WAL growth cap is configured on every connection.
    with ctx.db.reader() as conn:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
