"""Schema migration framework: fresh stamp, downgrade guard, version bump."""
import sqlite3

import pytest

from llm_wiki import db as db_module
from llm_wiki.db import SCHEMA_VERSION, Database, get_meta, set_meta


def test_fresh_db_stamped_and_has_audit_log(tmp_path):
    db = Database(tmp_path / "fresh.db")
    db.ensure_schema()
    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION
        conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()  # table exists


def test_projection_intent_tables_apply_to_current_stamped_database(tmp_path):
    db = Database(tmp_path / "projection.db")
    db.ensure_schema()
    with db.writer() as conn:
        conn.execute("DROP TABLE document_purge_intents")
        conn.execute("DROP TABLE file_projection_cleanup")
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION

    db.ensure_schema()
    with db.writer() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"file_projection_cleanup", "document_purge_intents"} <= tables
        assert "idx_file_projection_cleanup_path" in _indexes(conn)

        conn.execute(
            "INSERT INTO documents(path,path_norm,title,version,content_hash,folder,"
            "file_state,vector_dirty,is_deleted,created_at,updated_at) "
            "VALUES('a.md','a.md','A',1,'hash','','pending',0,0,'now','now')"
        )
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm='a.md'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO file_projection_cleanup("
                "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                "VALUES(?,?,?,1,NULL,NULL,NULL,NULL,NULL,1,'now')",
                (doc_id, "old.md", "old.md"),
            )
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
            "VALUES(?,?,?,0,NULL,NULL,NULL,NULL,NULL,1,'now')",
            (doc_id, "old.md", "old.md"),
        )
        conn.execute(
            "INSERT INTO document_purge_intents("
            "doc_id,path,path_norm,version,actor,via,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (doc_id, "a.md", "a.md", 1, "admin", "web", "now"),
        )
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        assert conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents"
        ).fetchone()[0] == 0


def _indexes(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def test_query_shape_indexes_replace_single_column_ones(tmp_path):
    # A pre-v7 DB carried the single-column idx_chunks_doc / idx_tags_tag; the v7-v11
    # migrations replace them with composite (doc_id,ordinal)/(tag,doc_id) indexes and
    # add idx_audit_actor — creating each replacement before dropping the old one.
    db = Database(tmp_path / "idx.db")
    db.ensure_schema()
    with db.writer() as conn:
        # Rewind to the v6 index layout: drop the new composites, restore the old ones.
        for name in ("idx_chunks_doc_ord", "idx_tags_tag_doc", "idx_audit_actor"):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("CREATE INDEX idx_chunks_doc ON chunks(doc_id)")
        conn.execute("CREATE INDEX idx_tags_tag ON tags(tag)")
        set_meta(conn, "schema_version", "6")

    db.ensure_schema()  # apply forward migrations

    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION
        idx = _indexes(conn)
        assert {"idx_chunks_doc_ord", "idx_tags_tag_doc", "idx_audit_actor"} <= idx
        assert "idx_chunks_doc" not in idx and "idx_tags_tag" not in idx


def test_downgrade_guard_refuses_newer_db(tmp_path):
    db = Database(tmp_path / "newer.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION + 5))
    with pytest.raises(RuntimeError):
        db.ensure_schema()


def test_old_version_is_bumped_to_current(tmp_path):
    db = Database(tmp_path / "old.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "schema_version", "1")
    db.ensure_schema()
    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION


def test_migration_failure_is_atomic_and_resumable(tmp_path, monkeypatch):
    # A failing migration must roll back its own DDL and leave schema_version at the
    # last fully-applied step — never a half-migrated DB — and a re-run must resume.
    db = Database(tmp_path / "mig.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "schema_version", "0")  # pretend an older DB with migrations pending

    # Migration 1 succeeds; migration 2 fails (the table it creates already exists).
    monkeypatch.setattr(db_module, "MIGRATIONS", [
        (1, "CREATE TABLE mig_step(x INTEGER)"),
        (2, "CREATE TABLE mig_step(y INTEGER)"),  # duplicate -> OperationalError
    ])
    with pytest.raises(sqlite3.OperationalError):
        db.ensure_schema()

    with db.reader() as conn:
        # Step 1 durably applied + stamped; step 2 rolled back (no partial state).
        assert int(get_meta(conn, "schema_version")) == 1
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mig_step'").fetchone()

    # Fix the broken migration and re-run: it resumes from version 1 to SCHEMA_VERSION.
    monkeypatch.setattr(db_module, "MIGRATIONS", [
        (1, "CREATE TABLE mig_step(x INTEGER)"),
        (2, "CREATE TABLE mig_step2(z INTEGER)"),
    ])
    db.ensure_schema()
    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mig_step2'").fetchone()
