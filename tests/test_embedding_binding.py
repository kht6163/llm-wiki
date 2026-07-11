import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, RLock, get_ident

import pytest

from llm_wiki import indexing
from llm_wiki.db import Database, get_meta
from llm_wiki.embedding import Embedder
from llm_wiki.embedding_contract import EmbeddingBinding, EmbeddingBindingChanged

MODEL = "test/embedding-model"
DIM = 3
PIPELINE = "passage-input-v1"


class _PauseAfterFirstUnlock:
    """Force a context switch immediately after one thread releases its RLock."""

    def __init__(self):
        self._lock = RLock()
        self._depth: dict[int, int] = {}
        self._first_thread: int | None = None
        self._paused = False
        self.first_unlocked = Event()
        self.resume_first = Event()

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            thread_id = get_ident()
            if self._first_thread is None:
                self._first_thread = thread_id
            self._depth[thread_id] = self._depth.get(thread_id, 0) + 1
        return acquired

    def release(self):
        thread_id = get_ident()
        depth = self._depth[thread_id] - 1
        if depth:
            self._depth[thread_id] = depth
        else:
            del self._depth[thread_id]
        self._lock.release()
        if (
            thread_id == self._first_thread
            and depth == 0
            and not self._paused
        ):
            self._paused = True
            self.first_unlocked.set()
            if not self.resume_first.wait(timeout=5):
                raise TimeoutError("second binding update did not finish")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()


def _initialize(tmp_path) -> tuple[Database, object]:
    db = Database(tmp_path / "wiki.db")
    binding = db.initialize(MODEL, DIM, PIPELINE)
    return db, binding


def _insert_document(db: Database, path: str, *, deleted: bool = False) -> int:
    with db.writer() as conn:
        cursor = conn.execute(
            "INSERT INTO documents("
            "path, path_norm, title, content_hash, vector_dirty, is_deleted, created_at, updated_at"
            ") VALUES(?, ?, ?, ?, 0, ?, ?, ?)",
            (path, path.casefold(), path, f"hash-{path}", int(deleted), "now", "now"),
        )
        return int(cursor.lastrowid)


def test_fresh_initialize_returns_and_stores_complete_binding(tmp_path):
    from llm_wiki.embedding_contract import EMBEDDING_PIPELINE, EmbeddingBinding

    db, binding = _initialize(tmp_path)

    expected = EmbeddingBinding(MODEL, DIM, EMBEDDING_PIPELINE, 1)
    assert binding == expected
    assert db._expected_embedding_binding == expected
    with db.reader() as conn:
        assert get_meta(conn, "embedding_model") == MODEL
        assert get_meta(conn, "embedding_dim") == str(DIM)
        assert get_meta(conn, "embedding_pipeline") == PIPELINE
        assert get_meta(conn, "embedding_epoch") == "1"


def test_legacy_binding_bootstrap_preserves_existing_vectors(tmp_path):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
            (101, Embedder.serialize([0.1] * DIM)),
        )
        conn.execute("DELETE FROM meta WHERE k IN ('embedding_pipeline', 'embedding_epoch')")
    db.close()

    reopened = Database(tmp_path / "wiki.db")
    binding = reopened.initialize(MODEL, DIM, PIPELINE)

    assert binding.pipeline == PIPELINE
    assert binding.epoch == 1
    with reopened.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
        assert get_meta(conn, "embedding_pipeline") == PIPELINE
        assert get_meta(conn, "embedding_epoch") == "1"


def test_vector_table_without_any_binding_meta_is_rejected(tmp_path):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
            (101, Embedder.serialize([0.1] * DIM)),
        )
        conn.execute("DELETE FROM meta WHERE k LIKE 'embedding_%'")
    db.close()

    with pytest.raises(RuntimeError) as exc_info:
        Database(tmp_path / "wiki.db").initialize(MODEL, DIM, PIPELINE)

    assert "reindex --reembed" in str(exc_info.value)
    unchanged = Database(tmp_path / "wiki.db")
    with unchanged.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM meta WHERE k LIKE 'embedding_%'"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("missing_key", ["embedding_pipeline", "embedding_epoch"])
def test_partial_binding_is_rejected(tmp_path, missing_key):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute("DELETE FROM meta WHERE k=?", (missing_key,))
    db.close()

    with pytest.raises(RuntimeError, match="embedding binding"):
        Database(tmp_path / "wiki.db").initialize(MODEL, DIM, PIPELINE)


@pytest.mark.parametrize(
    ("key", "value"),
    [("embedding_dim", "not-a-number"), ("embedding_epoch", "not-a-number")],
)
def test_non_numeric_binding_values_are_rejected(tmp_path, key, value):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute("UPDATE meta SET v=? WHERE k=?", (value, key))
    db.close()

    with pytest.raises(RuntimeError, match="embedding binding"):
        Database(tmp_path / "wiki.db").initialize(MODEL, DIM, PIPELINE)


@pytest.mark.parametrize(
    ("requested_dim", "requested_pipeline"),
    [(DIM + 1, PIPELINE), (DIM, "passage-input-v2")],
)
def test_binding_change_requires_explicit_reembed(
    tmp_path, requested_dim, requested_pipeline
):
    db, _ = _initialize(tmp_path)
    db.close()

    with pytest.raises(RuntimeError) as exc_info:
        Database(tmp_path / "wiki.db").initialize(
            MODEL, requested_dim, requested_pipeline
        )

    assert "reindex --reembed" in str(exc_info.value)


def test_vector_table_dimension_must_match_binding(tmp_path):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute("DROP TABLE chunk_vectors")
        conn.execute(
            "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
            "chunk_id INTEGER PRIMARY KEY, embedding float[4] distance_metric=cosine)"
        )
    db.close()

    with pytest.raises(RuntimeError) as exc_info:
        Database(tmp_path / "wiki.db").initialize(MODEL, DIM, PIPELINE)

    assert "reindex --reembed" in str(exc_info.value)


def test_missing_vector_table_is_recreated_and_live_documents_are_marked_dirty(tmp_path):
    db, _ = _initialize(tmp_path)
    live_id = _insert_document(db, "live.md")
    deleted_id = _insert_document(db, "deleted.md", deleted=True)
    with db.writer() as conn:
        conn.execute("DROP TABLE chunk_vectors")
    db.close()

    reopened = Database(tmp_path / "wiki.db")
    reopened.initialize(MODEL, DIM, PIPELINE)

    with reopened.reader() as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunk_vectors'"
        ).fetchone()[0]
        states = dict(
            conn.execute(
                "SELECT id, vector_dirty FROM documents WHERE id IN (?, ?)",
                (live_id, deleted_id),
            ).fetchall()
        )
    assert "float[3]" in table_sql.lower()
    assert states[live_id] == 1
    assert states[deleted_id] == 0


def test_missing_vector_table_recovery_rolls_back_when_dirty_marking_fails(tmp_path):
    db, _ = _initialize(tmp_path)
    live_id = _insert_document(db, "live.md")
    with db.writer() as conn:
        binding_before = dict(
            conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'embedding_%' ORDER BY k"
            ).fetchall()
        )
        conn.execute("DROP TABLE chunk_vectors")
        conn.execute(
            "CREATE TRIGGER fail_vector_dirty_update "
            "BEFORE UPDATE OF vector_dirty ON documents "
            "BEGIN SELECT RAISE(ABORT, 'forced dirty failure'); END"
        )
    db.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced dirty failure"):
        Database(tmp_path / "wiki.db").initialize(MODEL, DIM, PIPELINE)

    after = Database(tmp_path / "wiki.db")
    with after.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='chunk_vectors'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT vector_dirty FROM documents WHERE id=?", (live_id,)
        ).fetchone()[0] == 0
        assert dict(
            conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'embedding_%' ORDER BY k"
            ).fetchall()
        ) == binding_before


def test_rebind_same_binding_tuple_increments_epoch(tmp_path):
    db, original = _initialize(tmp_path)

    rebound = db.rebind_model(MODEL, DIM, PIPELINE)

    expected = EmbeddingBinding(MODEL, DIM, PIPELINE, original.epoch + 1)
    assert rebound == expected
    assert db.expected_embedding_binding() == expected
    with db.reader() as conn:
        conn.execute("BEGIN")
        try:
            assert get_meta(conn, "embedding_epoch") == str(expected.epoch)
            db.verify_embedding_binding(conn, expected)
        finally:
            conn.execute("ROLLBACK")


def test_verify_embedding_binding_requires_active_transaction(tmp_path):
    db, binding = _initialize(tmp_path)

    with db.reader() as conn:
        assert not conn.in_transaction
        with pytest.raises(RuntimeError, match="active transaction"):
            db.verify_embedding_binding(conn, binding)


def test_embedding_read_snapshot_owns_and_closes_its_transaction(tmp_path):
    db, binding = _initialize(tmp_path)

    with db.embedding_read_snapshot(binding) as conn:
        assert conn.in_transaction
        assert get_meta(conn, "embedding_epoch") == "1"

    with db.reader() as conn:
        assert not conn.in_transaction


def test_embedding_read_snapshot_nesting_preserves_outer_transaction(tmp_path):
    db, binding = _initialize(tmp_path)

    with db.embedding_read_snapshot(binding) as outer:
        assert outer.in_transaction
        with db.embedding_read_snapshot(binding) as inner:
            assert inner is outer
            assert inner.in_transaction
        assert outer.in_transaction

    with db.reader() as conn:
        assert not conn.in_transaction


def test_embedding_read_snapshot_does_not_own_writer_transaction(tmp_path):
    db, binding = _initialize(tmp_path)

    with db.writer() as conn:
        with db.embedding_read_snapshot(binding) as snapshot:
            assert snapshot is conn
            snapshot.execute(
                "INSERT INTO meta(k, v) VALUES('snapshot_writer_test', 'committed')"
            )
        assert conn.in_transaction

    with db.reader() as conn:
        assert get_meta(conn, "snapshot_writer_test") == "committed"


def test_embed_doc_rejects_embedder_identity_before_model_call(tmp_path):
    db, _ = _initialize(tmp_path)
    doc_id = _insert_document(db, "identity.md")
    with db.writer() as conn:
        conn.execute(
            "UPDATE documents SET vector_dirty=1 WHERE id=?", (doc_id,)
        )
        conn.execute(
            "INSERT INTO chunks("
            "doc_id, ordinal, heading, text, char_start, char_end, heading_path"
            ") VALUES(?, 0, NULL, 'body', 0, 4, NULL)",
            (doc_id,),
        )

    class WrongEmbedder:
        model_name = "test/wrong-model"
        dim = DIM
        pipeline = PIPELINE

        def embed_passages(self, _texts):
            raise AssertionError("binding mismatch must fail before model call")

    with pytest.raises(EmbeddingBindingChanged, match="embedder"):
        indexing.embed_doc(db, WrongEmbedder(), doc_id)


def test_rebind_without_any_binding_starts_at_epoch_one(tmp_path):
    db = Database(tmp_path / "wiki.db")
    db.ensure_schema()

    rebound = db.rebind_model(MODEL, DIM, PIPELINE)

    assert rebound == EmbeddingBinding(MODEL, DIM, PIPELINE, 1)


def test_rebind_legacy_model_and_dimension_advances_logical_epoch_one(tmp_path):
    db, _ = _initialize(tmp_path)
    with db.writer() as conn:
        conn.execute("DELETE FROM meta WHERE k IN ('embedding_pipeline', 'embedding_epoch')")
    db.close()
    legacy = Database(tmp_path / "wiki.db")

    rebound = legacy.rebind_model(MODEL, DIM, PIPELINE)

    assert rebound == EmbeddingBinding(MODEL, DIM, PIPELINE, 2)


def test_rebind_stores_new_binding_tuple_in_meta_and_local_token(tmp_path):
    db, _ = _initialize(tmp_path)

    rebound = db.rebind_model("test/new-model", DIM + 1, "passage-input-v2")

    expected = EmbeddingBinding("test/new-model", DIM + 1, "passage-input-v2", 2)
    assert rebound == expected
    assert db.expected_embedding_binding() == expected
    with db.reader() as conn:
        stored = dict(
            conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'embedding_%' ORDER BY k"
            ).fetchall()
        )
    assert stored == {
        "embedding_dim": str(DIM + 1),
        "embedding_epoch": "2",
        "embedding_model": "test/new-model",
        "embedding_pipeline": "passage-input-v2",
    }


@pytest.mark.parametrize(
    "present_keys",
    [
        ("embedding_model",),
        ("embedding_dim",),
        ("embedding_pipeline",),
        ("embedding_epoch",),
        ("embedding_model", "embedding_pipeline"),
        ("embedding_model", "embedding_epoch"),
        ("embedding_dim", "embedding_pipeline"),
        ("embedding_dim", "embedding_epoch"),
        ("embedding_pipeline", "embedding_epoch"),
        ("embedding_model", "embedding_dim", "embedding_pipeline"),
        ("embedding_model", "embedding_dim", "embedding_epoch"),
        ("embedding_model", "embedding_pipeline", "embedding_epoch"),
        ("embedding_dim", "embedding_pipeline", "embedding_epoch"),
    ],
)
def test_rebind_rejects_partial_binding_states(tmp_path, present_keys):
    db, original = _initialize(tmp_path)
    all_keys = {
        "embedding_model",
        "embedding_dim",
        "embedding_pipeline",
        "embedding_epoch",
    }
    with db.writer() as conn:
        for key in all_keys - set(present_keys):
            conn.execute("DELETE FROM meta WHERE k=?", (key,))

    with pytest.raises(RuntimeError, match="embedding binding"):
        db.rebind_model(MODEL, DIM, PIPELINE)

    assert db.expected_embedding_binding() == original


def test_sequential_rebinds_from_different_instances_use_distinct_epochs(tmp_path):
    db_a, epoch_one = _initialize(tmp_path)
    db_b = Database(tmp_path / "wiki.db")
    assert db_b.initialize(MODEL, DIM, PIPELINE) == epoch_one

    epoch_two = db_a.rebind_model(MODEL, DIM, PIPELINE)
    epoch_three = db_b.rebind_model(MODEL, DIM, PIPELINE)

    assert (epoch_two.epoch, epoch_three.epoch) == (2, 3)


def test_concurrent_rebinds_from_different_instances_serialize_epochs(tmp_path):
    db_a, epoch_one = _initialize(tmp_path)
    db_b = Database(tmp_path / "wiki.db")
    assert db_b.initialize(MODEL, DIM, PIPELINE) == epoch_one
    barrier = Barrier(2)

    def rebind(db):
        barrier.wait()
        return db.rebind_model(MODEL, DIM, PIPELINE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rebind, (db_a, db_b)))

    assert sorted(binding.epoch for binding in results) == [2, 3]


@pytest.mark.parametrize("first_operation", ["initialize", "rebind"])
def test_same_instance_expected_binding_follows_transaction_commit_order(
    tmp_path, monkeypatch, first_operation
):
    db, _ = _initialize(tmp_path)
    monkeypatch.setattr(db, "ensure_schema", lambda: None)
    coordinated_lock = _PauseAfterFirstUnlock()
    db._write_lock = coordinated_lock

    def first_update():
        if first_operation == "initialize":
            return db.initialize(MODEL, DIM, PIPELINE)
        return db.rebind_model(MODEL, DIM, PIPELINE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_update)
        assert coordinated_lock.first_unlocked.wait(timeout=5)
        second_future = pool.submit(db.rebind_model, MODEL, DIM, PIPELINE)
        try:
            second = second_future.result(timeout=5)
        finally:
            coordinated_lock.resume_first.set()
        first_future.result(timeout=5)

    assert db.expected_embedding_binding() == second
    with db.reader() as conn:
        assert get_meta(conn, "embedding_epoch") == str(second.epoch)


def test_rebind_empties_vectors_and_resets_live_and_deleted_dirty_states(tmp_path):
    db, _ = _initialize(tmp_path)
    live_id = _insert_document(db, "live.md")
    deleted_id = _insert_document(db, "deleted.md", deleted=True)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
            (101, Embedder.serialize([0.1] * DIM)),
        )
        conn.execute(
            "UPDATE documents SET vector_dirty=1 WHERE id=?", (deleted_id,)
        )

    db.rebind_model(MODEL, DIM, PIPELINE)

    with db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0
        states = dict(
            conn.execute(
                "SELECT id, vector_dirty FROM documents WHERE id IN (?, ?)",
                (live_id, deleted_id),
            ).fetchall()
        )
    assert states == {live_id: 1, deleted_id: 0}


def test_rebind_rolls_back_vectors_binding_and_dirty_flags_on_failure(tmp_path):
    db, original = _initialize(tmp_path)
    live_id = _insert_document(db, "live.md")
    deleted_id = _insert_document(db, "deleted.md", deleted=True)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
            (101, Embedder.serialize([0.1] * DIM)),
        )
        conn.execute(
            "UPDATE documents SET vector_dirty=1 WHERE id=?", (deleted_id,)
        )
        binding_before = dict(
            conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'embedding_%' ORDER BY k"
            ).fetchall()
        )
        table_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunk_vectors'"
        ).fetchone()[0]
        states_before = dict(
            conn.execute(
                "SELECT id, vector_dirty FROM documents WHERE id IN (?, ?)",
                (live_id, deleted_id),
            ).fetchall()
        )
        conn.execute(
            "CREATE TRIGGER fail_rebind_dirty_update "
            "BEFORE UPDATE OF vector_dirty ON documents "
            "BEGIN SELECT RAISE(ABORT, 'forced rebind dirty failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced rebind dirty failure"):
        db.rebind_model("test/new-model", DIM + 1, "passage-input-v2")

    assert db.expected_embedding_binding() == original
    with db.reader() as conn:
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunk_vectors'"
        ).fetchone()[0] == table_before
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 1
        assert dict(
            conn.execute(
                "SELECT k, v FROM meta WHERE k LIKE 'embedding_%' ORDER BY k"
            ).fetchall()
        ) == binding_before
        assert dict(
            conn.execute(
                "SELECT id, vector_dirty FROM documents WHERE id IN (?, ?)",
                (live_id, deleted_id),
            ).fetchall()
        ) == states_before


def test_rebind_updates_only_calling_database_expected_token(tmp_path):
    db_a, epoch_one = _initialize(tmp_path)
    db_b = Database(tmp_path / "wiki.db")
    assert db_b.initialize(MODEL, DIM, PIPELINE) == epoch_one

    epoch_two = db_b.rebind_model(MODEL, DIM, PIPELINE)

    assert epoch_two.epoch == epoch_one.epoch + 1
    assert db_a.expected_embedding_binding() == epoch_one
    assert db_b.expected_embedding_binding() == epoch_two
    with db_a.reader() as conn:
        conn.execute("BEGIN")
        try:
            with pytest.raises(EmbeddingBindingChanged):
                db_a.verify_embedding_binding(conn, epoch_one)
        finally:
            conn.execute("ROLLBACK")
