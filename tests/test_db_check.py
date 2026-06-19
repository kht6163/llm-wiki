"""Database integrity check (PRAGMA integrity_check + foreign_key_check) exposed as a
Database method and the `db-check` CLI command. Lets an operator detect corruption (a
real risk under WAL + external edits + the background embed worker) before a backup
copies a broken DB."""
import sqlite3
from types import SimpleNamespace

from llm_wiki import _cli_impl


def _orphan_chunk(db):
    """Insert a chunk referencing a non-existent doc with FK enforcement off, to
    manufacture a foreign-key violation the check must catch."""
    raw = sqlite3.connect(db.path)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO chunks(doc_id, ordinal, text, char_start, char_end) "
                    "VALUES(999999, 0, 'orphan', 0, 6)")
        raw.commit()
    finally:
        raw.close()


def _orphan_vector(db):
    """Insert a chunk_vectors row whose chunk_id has no chunk — the orphan class that
    foreign_key_check can't see (vec0 is a virtual table). The vec0 embedding must match
    the bound dimension, so build a zero JSON vector of that width."""
    import sqlite_vec

    raw = sqlite3.connect(db.path)
    try:
        dim = int(raw.execute("SELECT v FROM meta WHERE k='embedding_dim'").fetchone()[0])
        raw.enable_load_extension(True)
        sqlite_vec.load(raw)
        raw.execute(
            "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
            (888888, "[" + ",".join(["0"] * dim) + "]"),
        )
        raw.commit()
    finally:
        raw.close()


def test_integrity_check_healthy(ctx):
    report = ctx.db.integrity_check()
    assert report["ok"] is True
    assert report["check"] == "integrity_check"
    assert report["integrity"] == ["ok"]
    assert report["foreign_key_violations"] == []
    assert report["orphan_vectors"] == 0


def test_integrity_check_quick_variant(ctx):
    report = ctx.db.integrity_check(quick=True)
    assert report["ok"] is True
    assert report["check"] == "quick_check"


def test_integrity_check_detects_foreign_key_violation(ctx):
    _orphan_chunk(ctx.db)
    report = ctx.db.integrity_check()
    assert report["ok"] is False
    assert any(v["table"] == "chunks" for v in report["foreign_key_violations"])


def test_integrity_check_detects_orphan_vectors(ctx):
    # A vector whose chunk is gone — invisible to foreign_key_check, caught here.
    _orphan_vector(ctx.db)
    report = ctx.db.integrity_check()
    assert report["ok"] is False
    assert report["orphan_vectors"] >= 1
    assert report["integrity"] == ["ok"]              # b-tree is fine; only the vec0 dangler
    assert report["foreign_key_violations"] == []     # FK check genuinely can't see it


def test_delete_orphan_vectors_repairs(ctx):
    _orphan_vector(ctx.db)
    removed = ctx.db.delete_orphan_vectors()
    assert removed >= 1
    assert ctx.db.integrity_check()["orphan_vectors"] == 0
    assert ctx.db.delete_orphan_vectors() == 0        # idempotent: nothing left to remove


def test_db_check_cli_healthy(ctx, monkeypatch, capsys):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._db_check(SimpleNamespace(quick=False, fix_orphan_vectors=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "integrity (integrity_check): ok" in out
    assert "foreign keys: ok" in out
    assert "orphan vectors: ok" in out


def test_db_check_cli_reports_corruption(ctx, monkeypatch, capsys):
    _orphan_chunk(ctx.db)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._db_check(SimpleNamespace(quick=False, fix_orphan_vectors=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "table=chunks" in out


def test_db_check_cli_fixes_orphan_vectors(ctx, monkeypatch, capsys):
    _orphan_vector(ctx.db)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._db_check(SimpleNamespace(quick=False, fix_orphan_vectors=True))
    out = capsys.readouterr().out
    assert "orphan vectors: removed 1" in out
    assert rc == 0  # repaired -> the subsequent check is clean
    assert "orphan vectors: ok" in out
