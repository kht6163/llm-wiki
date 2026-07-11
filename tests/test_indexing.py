"""Embedding maintenance (batch B): vectors land for the current chunk set, and the
dirty flag survives a concurrent rechunk so the new chunks are not orphaned."""
import logging
import sqlite3

import pytest
from prometheus_client import REGISTRY

from llm_wiki import indexing
from llm_wiki.db import Database
from llm_wiki.embedding_contract import EmbeddingBindingChanged
from llm_wiki.services.documents import DocumentService


def _doc_id(ctx, norm):
    with ctx.db.reader() as conn:
        return conn.execute("SELECT id FROM documents WHERE path_norm=?", (norm,)).fetchone()[0]


def _dirty(ctx, doc_id):
    with ctx.db.reader() as conn:
        return conn.execute("SELECT vector_dirty FROM documents WHERE id=?", (doc_id,)).fetchone()[0]


def _vector_count(ctx, doc_id):
    with ctx.db.reader() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE doc_id=?)",
            (doc_id,),
        ).fetchone()[0]


def _vector_payloads(ctx, doc_id):
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT v.chunk_id, hex(v.embedding) AS payload "
            "FROM chunk_vectors v JOIN chunks c ON c.id=v.chunk_id "
            "WHERE c.doc_id=? ORDER BY v.chunk_id",
            (doc_id,),
        ).fetchall()
    return [(row["chunk_id"], row["payload"]) for row in rows]


def _mark_dirty_without_vectors(ctx, doc_id):
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM chunk_vectors WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE doc_id=?)",
            (doc_id,),
        )
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))


def _create_dirty_docs(ctx, principals, prefix, count):
    docs, principal = ctx.docs, principals["editor"]
    doc_ids = []
    for index in range(count):
        path = f"{prefix}-{index}.md"
        docs.create(principal, path, f"# T{index}\n\nbody passage {index}")
        doc_id = _doc_id(ctx, path)
        _mark_dirty_without_vectors(ctx, doc_id)
        doc_ids.append(doc_id)
    return doc_ids


def _replace_with_chunks(ctx, doc_id, count):
    with ctx.db.writer() as conn:
        indexing.clear_chunks(conn, doc_id)
        for ordinal in range(count):
            conn.execute(
                "INSERT INTO chunks("
                "doc_id, ordinal, heading, text, char_start, char_end, heading_path"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    ordinal,
                    f"H{ordinal}",
                    f"passage {ordinal}",
                    ordinal * 10,
                    ordinal * 10 + 9,
                    f"Section {ordinal}",
                ),
            )
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))


def _metric_sample(name):
    value = REGISTRY.get_sample_value(name)
    assert value is not None
    return value


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


def test_embed_doc_returns_true_only_after_clean_publish(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "publish.md", "# T\n\nalpha paragraph")
    doc_id = _doc_id(ctx, "publish.md")
    _mark_dirty_without_vectors(ctx, doc_id)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id) is True
    assert _dirty(ctx, doc_id) == 0
    assert _vector_count(ctx, doc_id) > 0


def test_embed_doc_returns_false_without_encoding_clean_document(
    ctx, principals, monkeypatch
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "already-clean.md", "# T\n\nalpha paragraph")
    doc_id = _doc_id(ctx, "already-clean.md")

    def unexpected_encode(_texts):
        raise AssertionError("clean document must not be encoded")

    monkeypatch.setattr(ctx.embedder, "embed_passages", unexpected_encode)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id) is False


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


@pytest.mark.parametrize("race", ["version", "heading_path"])
def test_embed_doc_rejects_changed_version_or_passage_input(
    ctx, principals, monkeypatch, race
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, f"{race}.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, f"{race}.md")
    _mark_dirty_without_vectors(ctx, doc_id)
    real = ctx.embedder.embed_passages

    def racing_embed(texts):
        out = real(texts)
        with ctx.db.writer() as conn:
            if race == "version":
                conn.execute(
                    "UPDATE documents SET version=version+1 WHERE id=?", (doc_id,)
                )
            else:
                conn.execute(
                    "UPDATE chunks SET heading_path='Changed > Section' WHERE doc_id=?",
                    (doc_id,),
                )
        return out

    monkeypatch.setattr(ctx.embedder, "embed_passages", racing_embed)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id) is False
    assert _dirty(ctx, doc_id) == 1
    assert _vector_count(ctx, doc_id) == 0


def test_embed_doc_fences_rebind_from_another_database_instance(
    ctx, principals, monkeypatch
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "epoch-race.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, "epoch-race.md")
    _mark_dirty_without_vectors(ctx, doc_id)
    other = Database(ctx.db.path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    real = ctx.embedder.embed_passages

    def racing_embed(texts):
        out = real(texts)
        other.rebind_model(
            ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
        )
        return out

    monkeypatch.setattr(ctx.embedder, "embed_passages", racing_embed)
    try:
        with pytest.raises(EmbeddingBindingChanged):
            indexing.embed_doc(ctx.db, ctx.embedder, doc_id)
    finally:
        other.close()

    assert _dirty(ctx, doc_id) == 1
    assert _vector_count(ctx, doc_id) == 0


def test_rebind_after_publish_discards_vectors_and_redirties_document(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "publish-then-rebind.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, "publish-then-rebind.md")
    _mark_dirty_without_vectors(ctx, doc_id)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id) is True
    assert _vector_count(ctx, doc_id) > 0

    ctx.db.rebind_model(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )

    assert _vector_count(ctx, doc_id) == 0
    assert _dirty(ctx, doc_id) == 1


@pytest.mark.parametrize("malformed", ["count", "dimension"])
def test_embed_doc_rejects_invalid_encoder_output_without_writing(
    ctx, principals, monkeypatch, malformed
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, f"bad-{malformed}.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, f"bad-{malformed}.md")
    _mark_dirty_without_vectors(ctx, doc_id)

    def malformed_embed(texts):
        if malformed == "count":
            return []
        return [[0.1] * (ctx.embedder.dim - 1) for _ in texts]

    monkeypatch.setattr(ctx.embedder, "embed_passages", malformed_embed)

    with pytest.raises(ValueError, match="embedding output"):
        indexing.embed_doc(ctx.db, ctx.embedder, doc_id)

    assert _dirty(ctx, doc_id) == 1
    assert _vector_count(ctx, doc_id) == 0


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_embed_doc_rejects_non_finite_output_without_changing_vectors(
    ctx, principals, monkeypatch, non_finite
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "non-finite.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, "non-finite.md")
    before = _vector_payloads(ctx, doc_id)
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))

    def malformed_embed(texts):
        return [
            [non_finite, *([0.1] * (ctx.embedder.dim - 1))]
            for _text in texts
        ]

    monkeypatch.setattr(ctx.embedder, "embed_passages", malformed_embed)

    with pytest.raises(ValueError, match="finite"):
        indexing.embed_doc(ctx.db, ctx.embedder, doc_id)

    assert _dirty(ctx, doc_id) == 1
    assert _vector_payloads(ctx, doc_id) == before


def test_embed_doc_rejects_float32_overflow_before_writer(
    ctx, principals, monkeypatch
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "overflow.md", "# T\n\nalpha paragraph one")
    doc_id = _doc_id(ctx, "overflow.md")
    before = _vector_payloads(ctx, doc_id)
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))

    def overflow_embed(texts):
        return [
            [1e39, *([0.1] * (ctx.embedder.dim - 1))]
            for _text in texts
        ]

    monkeypatch.setattr(ctx.embedder, "embed_passages", overflow_embed)
    writer_called = False

    def unexpected_writer():
        nonlocal writer_called
        writer_called = True
        raise AssertionError("writer must not open for a float32-overflowed vector")

    monkeypatch.setattr(ctx.db, "writer", unexpected_writer)

    with pytest.raises(ValueError, match="float32"):
        indexing.embed_doc(ctx.db, ctx.embedder, doc_id)

    assert writer_called is False
    assert _dirty(ctx, doc_id) == 1
    assert _vector_payloads(ctx, doc_id) == before


def test_embed_doc_bounds_model_calls_by_batch_size(ctx, principals, monkeypatch):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "large.md", "placeholder")
    doc_id = _doc_id(ctx, "large.md")
    _replace_with_chunks(ctx, doc_id, 5)
    call_sizes = []

    def fake_embed(texts):
        call_sizes.append(len(texts))
        return [[0.1] * ctx.embedder.dim for _ in texts]

    monkeypatch.setattr(ctx.embedder, "embed_passages", fake_embed)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id, batch_size=2) is True
    assert call_sizes == [2, 2, 1]
    assert _vector_count(ctx, doc_id) == 5


def test_embed_doc_records_metrics_per_encoder_batch(ctx, principals, monkeypatch):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "batch-metrics.md", "placeholder")
    doc_id = _doc_id(ctx, "batch-metrics.md")
    _replace_with_chunks(ctx, doc_id, 3)
    before_batches = _metric_sample("llmwiki_embed_duration_seconds_count")
    before_chunks = _metric_sample("llmwiki_embedded_chunks_total")

    def fake_embed(texts):
        return [[0.1] * ctx.embedder.dim for _text in texts]

    monkeypatch.setattr(ctx.embedder, "embed_passages", fake_embed)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id, batch_size=1) is True
    assert _metric_sample("llmwiki_embed_duration_seconds_count") == before_batches + 3
    assert _metric_sample("llmwiki_embedded_chunks_total") == before_chunks + 3


def test_embed_doc_records_attempted_batch_metrics_when_encoder_fails(
    ctx, principals, monkeypatch
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "failed-batch-metrics.md", "placeholder")
    doc_id = _doc_id(ctx, "failed-batch-metrics.md")
    _replace_with_chunks(ctx, doc_id, 2)
    before_batches = _metric_sample("llmwiki_embed_duration_seconds_count")
    before_chunks = _metric_sample("llmwiki_embedded_chunks_total")

    def failed_embed(_texts):
        raise RuntimeError("encoder failed")

    monkeypatch.setattr(ctx.embedder, "embed_passages", failed_embed)

    with pytest.raises(RuntimeError, match="encoder failed"):
        indexing.embed_doc(ctx.db, ctx.embedder, doc_id, batch_size=1)

    assert _metric_sample("llmwiki_embed_duration_seconds_count") == before_batches + 1
    assert _metric_sample("llmwiki_embedded_chunks_total") == before_chunks + 1
    assert _dirty(ctx, doc_id) == 1


def test_embed_doc_cleans_active_dirty_document_without_chunks(
    ctx, principals, monkeypatch
):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "empty.md", "placeholder")
    doc_id = _doc_id(ctx, "empty.md")
    with ctx.db.writer() as conn:
        indexing.clear_chunks(conn, doc_id)
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))

    def unexpected_encode(_texts):
        raise AssertionError("empty document must not be encoded")

    monkeypatch.setattr(ctx.embedder, "embed_passages", unexpected_encode)

    assert indexing.embed_doc(ctx.db, ctx.embedder, doc_id) is True
    assert _dirty(ctx, doc_id) == 0


def test_embed_pending_sweep_clears_dirty(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "s.md", "# S\n\nbody words here")
    doc_id = _doc_id(ctx, "s.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))
    n = indexing.embed_pending(ctx.db, ctx.embedder)
    assert n >= 1
    assert _dirty(ctx, doc_id) == 0


def test_embed_pending_drains_multiple_keyset_pages(ctx, principals):
    doc_ids = _create_dirty_docs(ctx, principals, "paged", 5)

    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_batch_size=2
    ) == len(doc_ids)
    assert [_dirty(ctx, doc_id) for doc_id in doc_ids] == [0] * len(doc_ids)
    assert all(_vector_count(ctx, doc_id) > 0 for doc_id in doc_ids)


def test_embed_pending_without_progress_does_not_read_chunks_for_empty_backlog(ctx):
    with ctx.db.reader() as conn:
        def deny_chunk_reads(action, table, _column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == "chunks":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_chunk_reads)
        try:
            assert indexing.embed_pending(ctx.db, ctx.embedder) == 0
        finally:
            conn.set_authorizer(None)


def test_embed_pending_without_progress_logs_only_document_count(
    ctx, principals, caplog
):
    doc_ids = _create_dirty_docs(ctx, principals, "log-backlog", 2)
    caplog.set_level(logging.INFO, logger="llm_wiki.indexing")

    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_batch_size=1
    ) == len(doc_ids)

    start_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "llm_wiki.indexing"
        and record.getMessage().startswith("embed_pending: embedding")
    ]
    assert start_messages == ["embed_pending: embedding 2 document(s)"]


def test_embed_pending_progress_stats_plan_avoids_temp_btree(ctx):
    statements = []
    events = []
    with ctx.db.reader() as conn:
        conn.set_trace_callback(statements.append)
        try:
            assert indexing.embed_pending(
                ctx.db,
                ctx.embedder,
                progress=lambda done, total: events.append((done, total)),
            ) == 0
        finally:
            conn.set_trace_callback(None)

        stats_sql = next(
            statement for statement in statements if "AS chunk_count" in statement
        )
        plan = [
            row["detail"]
            for row in conn.execute(f"EXPLAIN QUERY PLAN {stats_sql}").fetchall()
        ]

    assert events == [(0, 0)]
    assert not any("USE TEMP B-TREE" in detail for detail in plan)
    assert any("idx_chunks_doc_ord" in detail for detail in plan)


def test_embed_pending_keeps_prior_commit_when_later_encoder_fails(
    ctx, principals, monkeypatch
):
    doc_ids = _create_dirty_docs(ctx, principals, "partial", 3)
    real = ctx.embedder.embed_passages
    calls = 0

    def fail_second_document(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second document failed")
        return real(texts)

    monkeypatch.setattr(ctx.embedder, "embed_passages", fail_second_document)

    with pytest.raises(RuntimeError, match="second document failed"):
        indexing.embed_pending(
            ctx.db, ctx.embedder, batch_size=1, doc_batch_size=2
        )

    assert [_dirty(ctx, doc_id) for doc_id in doc_ids] == [0, 1, 1]
    assert _vector_count(ctx, doc_ids[0]) > 0
    assert _vector_count(ctx, doc_ids[1]) == 0
    assert _vector_count(ctx, doc_ids[2]) == 0


def test_embed_pending_keeps_prior_commit_when_later_progress_callback_fails(
    ctx, principals
):
    doc_ids = _create_dirty_docs(ctx, principals, "progress-failure", 3)
    events = []

    def fail_on_second_document(done, total):
        events.append((done, total))
        if done == 2:
            raise RuntimeError("progress callback failed")

    with pytest.raises(RuntimeError, match="progress callback failed"):
        indexing.embed_pending(
            ctx.db,
            ctx.embedder,
            batch_size=1,
            doc_batch_size=1,
            progress=fail_on_second_document,
        )

    assert events == [(1, 3), (2, 3)]
    assert [_dirty(ctx, doc_id) for doc_id in doc_ids] == [0, 1, 1]
    assert _vector_count(ctx, doc_ids[0]) > 0
    assert _vector_count(ctx, doc_ids[1]) == 0
    assert _vector_count(ctx, doc_ids[2]) == 0


def test_embed_pending_single_doc_returns_cas_result_and_forwards_batch_size(
    ctx, principals, monkeypatch
):
    docs, principal = ctx.docs, principals["editor"]
    docs.create(principal, "single.md", "placeholder")
    doc_id = _doc_id(ctx, "single.md")
    _replace_with_chunks(ctx, doc_id, 3)
    real = ctx.embedder.embed_passages
    call_sizes = []
    raced = False

    def race_once(texts):
        nonlocal raced
        call_sizes.append(len(texts))
        outputs = real(texts)
        if not raced:
            raced = True
            with ctx.db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET version=version+1 WHERE id=?", (doc_id,)
                )
        return outputs

    monkeypatch.setattr(ctx.embedder, "embed_passages", race_once)

    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_id=doc_id, batch_size=2
    ) == 0
    assert _dirty(ctx, doc_id) == 1
    assert call_sizes == [2, 1]

    call_sizes.clear()
    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_id=doc_id, batch_size=2
    ) == 1
    assert _dirty(ctx, doc_id) == 0
    assert call_sizes == [2, 1]


def test_embed_pending_reports_monotonic_progress_per_model_batch(
    ctx, principals, monkeypatch
):
    doc_ids = _create_dirty_docs(ctx, principals, "progress", 2)
    _replace_with_chunks(ctx, doc_ids[0], 3)
    _replace_with_chunks(ctx, doc_ids[1], 2)
    events = []
    call_sizes = []

    def fake_embed(texts):
        call_sizes.append(len(texts))
        return [[0.1] * ctx.embedder.dim for _text in texts]

    monkeypatch.setattr(ctx.embedder, "embed_passages", fake_embed)

    assert indexing.embed_pending(
        ctx.db,
        ctx.embedder,
        batch_size=2,
        doc_batch_size=1,
        progress=lambda done, total: events.append((done, total)),
    ) == 2

    assert call_sizes == [2, 1, 2]
    assert events == [(2, 5), (3, 5), (5, 5)]
    assert events == sorted(events)


def test_embed_pending_reports_completion_once_when_chunk_total_grows(
    ctx, principals, monkeypatch
):
    docs, principal = ctx.docs, principals["editor"]
    docs.create(principal, "growth-a.md", "# A\n\ninitial passage")
    docs.create(principal, "growth-b.md", "placeholder")
    docs.create(principal, "growth-c.md", "placeholder")
    a_id = _doc_id(ctx, "growth-a.md")
    b_id = _doc_id(ctx, "growth-b.md")
    c_id = _doc_id(ctx, "growth-c.md")
    _mark_dirty_without_vectors(ctx, a_id)
    _replace_with_chunks(ctx, b_id, 3)
    assert indexing.embed_doc(ctx.db, ctx.embedder, b_id) is True
    with ctx.db.writer() as conn:
        indexing.clear_chunks(conn, c_id)
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (c_id,))

    real = ctx.embedder.embed_passages
    queued_b = False

    def queue_larger_document(texts):
        nonlocal queued_b
        outputs = real(texts)
        if not queued_b:
            queued_b = True
            _mark_dirty_without_vectors(ctx, b_id)
        return outputs

    monkeypatch.setattr(ctx.embedder, "embed_passages", queue_larger_document)
    events = []

    assert indexing.embed_pending(
        ctx.db,
        ctx.embedder,
        batch_size=1,
        doc_batch_size=1,
        progress=lambda done, total: events.append((done, total)),
    ) == 3

    assert events == [(1, 1)]
    assert [_dirty(ctx, doc_id) for doc_id in (a_id, b_id, c_id)] == [0, 0, 0]


def test_embed_pending_defers_document_created_above_snapshot_max_id(
    ctx, principals, monkeypatch
):
    docs, principal = ctx.docs, principals["editor"]
    docs.create(principal, "snapshot-existing.md", "# Existing\n\nfirst passage")
    existing_id = _doc_id(ctx, "snapshot-existing.md")
    _mark_dirty_without_vectors(ctx, existing_id)

    class DeferredWorker:
        def notify(self):
            pass

    deferred_docs = DocumentService(
        ctx.db,
        ctx.embedder,
        ctx.settings.vault_path,
        embed_worker=DeferredWorker(),
    )
    real = ctx.embedder.embed_passages
    created = False

    def create_newer_document(texts):
        nonlocal created
        outputs = real(texts)
        if not created:
            created = True
            deferred_docs.create(
                principal, "snapshot-new.md", "# New\n\ncreated during sweep"
            )
        return outputs

    monkeypatch.setattr(ctx.embedder, "embed_passages", create_newer_document)

    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_batch_size=1
    ) == 1
    new_id = _doc_id(ctx, "snapshot-new.md")
    assert new_id > existing_id
    assert _dirty(ctx, existing_id) == 0
    assert _dirty(ctx, new_id) == 1

    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_batch_size=1
    ) == 1
    assert _dirty(ctx, new_id) == 0


def test_embed_pending_stops_immediately_when_binding_changes(
    ctx, principals, monkeypatch
):
    doc_ids = _create_dirty_docs(ctx, principals, "binding", 2)
    other = Database(ctx.db.path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    real = ctx.embedder.embed_passages
    calls = 0

    def rebind_after_first_encode(texts):
        nonlocal calls
        calls += 1
        outputs = real(texts)
        other.rebind_model(
            ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
        )
        return outputs

    monkeypatch.setattr(ctx.embedder, "embed_passages", rebind_after_first_encode)
    try:
        with pytest.raises(EmbeddingBindingChanged):
            indexing.embed_pending(
                ctx.db, ctx.embedder, batch_size=1, doc_batch_size=1
            )
    finally:
        other.close()

    assert calls == 1
    assert [_dirty(ctx, doc_id) for doc_id in doc_ids] == [1, 1]
    assert [_vector_count(ctx, doc_id) for doc_id in doc_ids] == [0, 0]


def test_metadata_only_update_preserves_pending_dirty(ctx, principals):
    # A doc with a queued-but-unembedded vector (vector_dirty=1, no vectors yet — the
    # state reindex leaves) must NOT have that flag cleared by a metadata-only edit.
    # Clearing it would cancel the embedding and the doc would vanish from vector
    # search with no later retry.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "meta.md", "# T\n\nstable body text")
    doc_id = _doc_id(ctx, "meta.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,))
        conn.execute("DELETE FROM chunk_vectors WHERE chunk_id IN "
                     "(SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
    cur = docs.get("meta.md")
    # Identical body, only the title changes -> content_changed is False.
    docs.update(p, "meta.md", cur["version"], cur["content"], title="New Title")
    assert _dirty(ctx, doc_id) == 1  # pending flag preserved, not silently cleared
    indexing.embed_pending(ctx.db, ctx.embedder)
    assert _dirty(ctx, doc_id) == 0  # a later sweep actually embeds it


def test_embed_pending_skips_doc_rechunked_mid_sweep(ctx, principals, monkeypatch):
    # The batch sweep must not mark a doc clean if its chunks changed under it; that
    # doc's vectors would be stale/missing and it would never be retried.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "A.md", "# A\n\nalpha alpha alpha")
    docs.create(p, "B.md", "# B\n\nbeta beta beta")
    docs.create(p, "C.md", "# C\n\ncharlie charlie charlie")
    a_id, b_id, c_id = (
        _doc_id(ctx, "a.md"),
        _doc_id(ctx, "b.md"),
        _doc_id(ctx, "c.md"),
    )
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET vector_dirty=1 WHERE id IN (?,?,?)",
            (a_id, b_id, c_id),
        )

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
    assert indexing.embed_pending(
        ctx.db, ctx.embedder, doc_batch_size=1
    ) == 2
    assert _dirty(ctx, a_id) == 1  # rechunked mid-sweep -> left dirty for a later sweep
    assert _dirty(ctx, b_id) == 0  # cleanly embedded -> cleared
    assert _dirty(ctx, c_id) == 0  # later pages are not starved by the failed CAS
