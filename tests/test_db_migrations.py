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
    # A pre-v7 DB carried the single-column idx_chunks_doc / idx_tags_tag; the v7-v12
    # migrations replace them with composite (doc_id,ordinal)/(tag,doc_id) indexes and
    # add idx_audit_actor — creating each replacement before dropping the old one.
    db = Database(tmp_path / "idx.db")
    db.ensure_schema()
    with db.writer() as conn:
        # Rewind to the v6 index layout: drop the new composites, restore the old ones.
        for name in (
            "idx_chunks_doc_ord",
            "idx_tags_tag_doc",
            "idx_audit_actor",
            "idx_documents_live_content_hash",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("CREATE INDEX idx_chunks_doc ON chunks(doc_id)")
        conn.execute("CREATE INDEX idx_tags_tag ON tags(tag)")
        set_meta(conn, "schema_version", "6")

    db.ensure_schema()  # apply forward migrations

    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION
        idx = _indexes(conn)
        assert {
            "idx_chunks_doc_ord",
            "idx_tags_tag_doc",
            "idx_audit_actor",
            "idx_documents_live_content_hash",
        } <= idx
        assert "idx_chunks_doc" not in idx and "idx_tags_tag" not in idx


def test_downgrade_guard_refuses_newer_db(tmp_path):
    db = Database(tmp_path / "newer.db")
    db.ensure_schema()
    with db.writer() as conn:
        conn.execute("DROP TABLE favorites")
        set_meta(conn, "schema_version", str(SCHEMA_VERSION + 5))
    with pytest.raises(RuntimeError):
        db.ensure_schema()
    with db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='favorites'"
        ).fetchone() is None


def test_old_version_is_bumped_to_current(tmp_path):
    db = Database(tmp_path / "old.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "schema_version", "1")
    db.ensure_schema()
    with db.reader() as conn:
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION


def test_v13_adds_user_credential_version(tmp_path):
    db = Database(tmp_path / "credential-version.db")
    db.ensure_schema()
    with db.writer() as conn:
        conn.execute("ALTER TABLE users DROP COLUMN credential_version")
        set_meta(conn, "schema_version", "12")

    db.ensure_schema()

    with db.reader() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "credential_version" in columns
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION


def test_upgraded_api_key_scope_enforces_same_check_as_fresh_database(tmp_path):
    db = Database(tmp_path / "scope-check.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "schema_version", "14")

    db.ensure_schema()

    with db.writer() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,created_at,updated_at) "
            "VALUES('u','hash','viewer','now','now')"
        )
        user_id = conn.execute("SELECT id FROM users WHERE username='u'").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO api_keys(user_id,name,key_prefix,key_hash,created_at,scope) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, "bad", "prefix", "hash", "now", "invalid"),
            )


def test_sessions_and_share_link_indexes_exist(tmp_path):
    db = Database(tmp_path / "ops-indexes.db")
    db.ensure_schema()
    with db.reader() as conn:
        indexes = _indexes(conn)
    assert {
        "idx_sessions_user",
        "idx_sessions_expires",
        "idx_share_links_doc",
        "idx_share_links_created_by",
        "idx_share_links_active",
    } <= indexes


def test_v18_users_oidc_columns_and_constraints(tmp_path):
    """v18 makes password_hash nullable, adds email/oidc identity, and enforces
    the paired-oidc CHECK plus partial unique indexes on email and (issuer, sub)."""
    db = Database(tmp_path / "oidc-users.db")
    db.ensure_schema()
    # Rewind outside a writer transaction so PRAGMA foreign_keys=OFF takes effect
    # (it is a no-op inside an open transaction).
    conn = db.connect()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "CREATE TABLE users__old ("
            "id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,"
            "password_hash TEXT NOT NULL,"
            "role TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),"
            "is_active INTEGER NOT NULL DEFAULT 1,"
            "credential_version INTEGER NOT NULL DEFAULT 1,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users__old(id,username,password_hash,role,is_active,"
            "credential_version,created_at,updated_at) "
            "SELECT id,username,password_hash,role,is_active,credential_version,"
            "created_at,updated_at FROM users"
        )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users__old RENAME TO users")
        for name in ("idx_users_email", "idx_users_oidc"):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        set_meta(conn, "schema_version", "17")
        conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()
        db.close()  # drop any cached connection that saw the old layout

    db.ensure_schema()

    with db.writer() as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(users)")}
        assert "email" in columns
        assert "oidc_issuer" in columns
        assert "oidc_sub" in columns
        # password_hash is nullable (notnull == 0)
        assert columns["password_hash"][3] == 0
        assert int(get_meta(conn, "schema_version")) == SCHEMA_VERSION
        assert {"idx_users_email", "idx_users_oidc"} <= _indexes(conn)

        # Local password user still valid; SSO-only row with NULL password_hash ok.
        conn.execute(
            "INSERT INTO users(username,password_hash,role,email,oidc_issuer,oidc_sub,"
            "created_at,updated_at) VALUES('sso',NULL,'viewer','sso@ex.com',"
            "'https://idp.example','sub-1','now','now')"
        )
        # CHECK: one of issuer/sub without the other is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users(username,password_hash,role,oidc_issuer,oidc_sub,"
                "created_at,updated_at) VALUES('bad',NULL,'viewer',"
                "'https://idp.example',NULL,'now','now')"
            )
        # Partial unique on email (when not null).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users(username,password_hash,role,email,"
                "created_at,updated_at) VALUES('dup','h','viewer','sso@ex.com','now','now')"
            )
        # Partial unique on (oidc_issuer, oidc_sub).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users(username,password_hash,role,oidc_issuer,oidc_sub,"
                "created_at,updated_at) VALUES('dup2',NULL,'viewer',"
                "'https://idp.example','sub-1','now','now')"
            )


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
