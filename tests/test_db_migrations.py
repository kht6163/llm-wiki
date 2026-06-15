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
