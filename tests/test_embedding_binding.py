import sqlite3

import pytest

from llm_wiki.db import Database, get_meta
from llm_wiki.embedding import Embedder

MODEL = "test/embedding-model"
DIM = 3
PIPELINE = "passage-input-v1"


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
