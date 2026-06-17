"""Background embedding worker: when attached (serving), a write defers the slow
forward pass to the worker and only flags the document vector_dirty; the worker sweep
clears it. Without a worker (the default in tests/CLI), embedding stays inline so a
write is immediately visible to vector search."""
import time

from llm_wiki import indexing
from llm_wiki.services.documents import DocumentService


def _dirty(db, path):
    with db.reader() as conn:
        r = conn.execute("SELECT vector_dirty FROM documents WHERE path=?", (path,)).fetchone()
    return r[0] if r else None


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
