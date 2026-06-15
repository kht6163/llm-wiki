"""Schema migration framework: fresh stamp, downgrade guard, version bump."""
import pytest

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
