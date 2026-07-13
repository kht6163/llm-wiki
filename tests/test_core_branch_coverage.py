from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from llm_wiki import file_projection as fp
from llm_wiki import indexing, search, snapshot
from llm_wiki.db import SCHEMA_VERSION, Database, set_meta
from llm_wiki.embedding import DisabledEmbedder, Embedder
from llm_wiki.embedding_contract import EMBEDDING_PIPELINE
from llm_wiki.services import audit, auth
from llm_wiki.services.auth import Principal, create_user
from llm_wiki.services.errors import EmbeddingUnavailableError


def _insert_document(
    db: Database,
    *,
    path: str = "note.md",
    body: str = "# Note\n\nalpha",
    file_state: str = "clean",
) -> int:
    digest = hashlib.sha256(body.encode()).hexdigest()
    with db.writer() as conn:
        cursor = conn.execute(
            "INSERT INTO documents("
            "path,path_norm,title,version,content_hash,folder,file_state,vector_dirty,"
            "is_deleted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (path, path.casefold(), "Note", 1, digest, "", file_state, 0, 0, "now", "now"),
        )
        doc_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO revisions(doc_id,version,body,title,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, 1, body, "Note", digest, "now"),
        )
    return doc_id


def test_database_schema_edge_states_and_vectorless_cleanup(tmp_path):
    vectorless = Database(tmp_path / "vectorless.db")
    vectorless.ensure_schema()
    assert vectorless.delete_orphan_vectors() == 0

    missing_stamp_path = tmp_path / "missing-stamp.db"
    with sqlite3.connect(missing_stamp_path) as conn:
        conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    missing_stamp = Database(missing_stamp_path)
    missing_stamp.ensure_schema()

    invalid_path = tmp_path / "invalid-stamp.db"
    with sqlite3.connect(invalid_path) as conn:
        conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO meta VALUES('schema_version', 'not-an-integer')")
    with pytest.raises(RuntimeError, match="not a valid integer"):
        Database(invalid_path).ensure_schema()

    with missing_stamp.writer() as conn:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION + 1))
    with pytest.raises(RuntimeError, match="newer than this build"):
        missing_stamp._apply_migrations(missing_stamp.connect())


@pytest.mark.parametrize(
    ("epoch", "message"),
    [("invalid", "valid integer"), ("0", "must be positive")],
)
def test_rebind_rejects_legacy_revision_binding_with_bad_epoch(tmp_path, epoch, message):
    db = Database(tmp_path / f"legacy-{epoch}.db")
    db.ensure_schema()
    with db.writer() as conn:
        set_meta(conn, "embedding_model", "fake/model")
        set_meta(conn, "embedding_dim", "2")
        set_meta(conn, "embedding_pipeline", EMBEDDING_PIPELINE)
        set_meta(conn, "embedding_epoch", epoch)
        conn.execute("DELETE FROM meta WHERE k='embedding_revision'")

    with pytest.raises(RuntimeError, match=message):
        db.rebind_model("fake/model", 2, EMBEDDING_PIPELINE, "commit-a")


@pytest.mark.parametrize(
    ("modules", "expected"),
    [
        ({}, "requested-tag"),
        (
            {
                "bad": SimpleNamespace(
                    auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=None))
                ),
                "good": SimpleNamespace(
                    auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash="commit-b"))
                ),
            },
            "commit-b",
        ),
    ],
)
def test_embedder_revision_resolution_fallbacks(monkeypatch, modules, expected):
    model = SimpleNamespace(_modules=modules)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=lambda _name, **_kwargs: model),
    )
    assert Embedder("fake/model", "requested-tag").revision == expected
    assert DisabledEmbedder().revision == "disabled"


def test_read_confined_bytes_size_and_generation_guards(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "asset.bin"
    target.write_bytes(b"data")

    assert fp.read_confined_bytes(vault, "asset.bin") == (target, b"data")

    with pytest.raises(fp.UnsafeProjectionPath, match="size limit"):
        fp.read_confined_bytes(vault, "asset.bin", max_bytes=3)

    real_fstat = fp.os.fstat
    changed = False

    def changed_open_fstat(fd):
        nonlocal changed
        value = real_fstat(fd)
        if stat.S_ISREG(value.st_mode) and not changed:
            changed = True
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_dev=value.st_dev,
                st_ino=value.st_ino + 1,
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns,
                st_ctime_ns=value.st_ctime_ns,
            )
        return value

    monkeypatch.setattr(fp.os, "fstat", changed_open_fstat)
    with pytest.raises(fp.FileGenerationChanged, match="while opening"):
        fp.read_confined_bytes(vault, "asset.bin")

    monkeypatch.setattr(fp, "_stat_at_signature", lambda *_args, **_kwargs: None)
    with pytest.raises(FileNotFoundError):
        fp.read_confined_bytes(vault, "asset.bin")


def test_read_confined_bytes_detects_growth_and_anchored_replacement(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "growth.bin").write_bytes(b"data")
    real_read = fp.os.read

    def oversized_read(fd, size):
        data = real_read(fd, size)
        return data + b"!" if data else data

    monkeypatch.setattr(fp.os, "read", oversized_read)
    with pytest.raises(fp.UnsafeProjectionPath, match="size limit"):
        fp.read_confined_bytes(vault, "growth.bin", max_bytes=4)

    monkeypatch.setattr(fp.os, "read", real_read)
    real_stat_at = fp._stat_at_signature
    calls = 0

    def replaced_anchor(directory_fd, name, path, *, missing_ok=False):
        nonlocal calls
        signature = real_stat_at(directory_fd, name, path, missing_ok=missing_ok)
        calls += 1
        if calls == 2 and signature is not None:
            return replace(signature, mtime_ns=signature.mtime_ns + 1)
        return signature

    monkeypatch.setattr(fp, "_stat_at_signature", replaced_anchor)
    with pytest.raises(fp.FileGenerationChanged, match="while reading"):
        fp.read_confined_bytes(vault, "growth.bin")


def test_write_confined_bytes_retries_interrupt_and_rejects_short_write(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    real_write = fp.os.write
    interrupted = False

    def interrupt_once(fd, data):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_write(fd, data)

    monkeypatch.setattr(fp.os, "write", interrupt_once)
    written = fp.write_confined_bytes(vault, "nested/good.bin", b"payload")
    assert written.read_bytes() == b"payload"

    monkeypatch.setattr(fp.os, "write", lambda _fd, _data: 0)
    with pytest.raises(OSError, match="short write"):
        fp.write_confined_bytes(vault, "nested/short.bin", b"payload")


def test_write_confined_bytes_handles_publish_and_cleanup_races(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    real_link = fp.os.link
    real_unlink = fp.os.unlink

    def publish_then_report_exists(*args, **kwargs):
        real_link(*args, **kwargs)
        raise FileExistsError

    def unlink_then_report_missing(*args, **kwargs):
        real_unlink(*args, **kwargs)
        raise FileNotFoundError

    monkeypatch.setattr(fp.os, "link", publish_then_report_exists)
    monkeypatch.setattr(fp.os, "unlink", unlink_then_report_missing)
    target = fp.write_confined_bytes(vault, "race.bin", b"payload")
    assert target.read_bytes() == b"payload"


def test_publish_prepared_removes_old_vectors(tmp_path):
    db = Database(tmp_path / "index.db")
    db.initialize("fake/model", 2, EMBEDDING_PIPELINE, "commit")
    doc_id = _insert_document(db)
    with db.writer() as conn:
        indexing.publish_prepared(
            conn, doc_id, "Note", "", indexing.prepare_markdown("# Note\n\nfirst")
        )
        chunk_id = int(
            conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO chunk_vectors(chunk_id,embedding) VALUES(?,?)",
            (chunk_id, Embedder.serialize([0.0, 0.0])),
        )
        indexing.publish_prepared(
            conn, doc_id, "Note", "", indexing.prepare_markdown("# Note\n\nsecond")
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id=?", (chunk_id,)
        ).fetchone()[0] == 0

    vectorless = Database(tmp_path / "index-vectorless.db")
    vectorless.ensure_schema()
    vectorless_id = _insert_document(vectorless)
    with vectorless.writer() as conn:
        first = indexing.prepare_markdown("# Note\n\nfirst")
        indexing.publish_prepared(conn, vectorless_id, "Note", "", first)
        indexing.publish_prepared(conn, vectorless_id, "Note", "", first)


def test_search_empty_vector_helpers_and_active_filter_sql():
    assert search._resolve_vector_rows(None, []) == []
    assert search._eligible_vector_ids(
        None, [], folder=None, tags=None, filters=search.QueryFilters()
    ) == set()
    fragment, params = search._vector_filter_sql(
        None,
        None,
        search.QueryFilters(title_contains=("note",)),
    )
    assert "LOWER" in fragment
    assert params == ["%note%"]
    fragment, params = search._vector_filter_sql(None, None, search.QueryFilters())
    assert fragment == " AND d.is_deleted=0" and params == []


def test_search_reapplies_folder_filter_to_ranked_candidate(tmp_path, monkeypatch):
    db = Database(tmp_path / "search.db")
    db.ensure_schema()
    doc_id = _insert_document(db)
    with db.writer() as conn:
        indexing.publish_prepared(
            conn, doc_id, "Note", "", indexing.prepare_markdown("# Note\n\nalpha")
        )
    monkeypatch.setattr(
        search,
        "_rank",
        lambda *_args, **_kwargs: ([(doc_id, 1.0)], {}, '"alpha"'),
    )
    results, truncated = search.search_page(
        db,
        DisabledEmbedder(),
        "alpha",
        mode="bm25",
        folder="elsewhere",
    )
    assert results == [] and truncated is False

    monkeypatch.setattr(
        search,
        "_rank",
        lambda *_args, **_kwargs: ([(doc_id + 999, 1.0)], {}, '"alpha"'),
    )
    assert search.search_page(db, DisabledEmbedder(), "alpha", mode="bm25") == ([], False)


def test_disabled_embedder_context_falls_back_or_rejects_vector(tmp_path):
    db = Database(tmp_path / "context.db")
    db.ensure_schema()
    disabled = DisabledEmbedder()
    with pytest.raises(EmbeddingUnavailableError):
        search.assemble_context(db, disabled, "question", mode="vector")
    result = search.assemble_context(db, disabled, "question", mode="hybrid")
    assert result["count"] == 0


def test_audit_without_filters_and_naive_api_key_timestamp(tmp_path):
    db = Database(tmp_path / "audit.db")
    db.ensure_schema()
    user_id = create_user(db, "alice", "secret12", "editor")
    principal = Principal(user_id, "alice", "editor")
    audit.record_tx(db, actor="alice", via="web", action="doc_update")
    assert audit.via_counts(db)["web"] >= 1

    auth.create_api_key(db, principal, "naive-time")
    with db.writer() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at='2000-01-01T00:00:00' WHERE name='naive-time'"
        )
    key = next(
        item
        for item in auth.list_api_keys(db, user_id)
        if item["name"] == "naive-time"
    )
    assert key["unused"] is True


def test_snapshot_reconciliation_pending_and_read_failures(tmp_path, monkeypatch):
    db = Database(tmp_path / "snapshot.db")
    db.ensure_schema()
    vault = tmp_path / "vault"
    vault.mkdir()
    _insert_document(db, path="pending.md", file_state="pending")
    with db.reader() as conn:
        snapshot._assert_reconciled_markdown(conn, vault)

    with db.writer() as conn:
        conn.execute("DELETE FROM documents")
    _insert_document(db, path="clean.md")
    (vault / "clean.md").write_text("placeholder", encoding="utf-8")

    for reason, detail in (
        ("invalid_encoding", "invalid UTF-8"),
        ("file_disappeared", "missing clean.md"),
        ("file_changed", "unstable clean.md"),
    ):
        monkeypatch.setattr(
            fp,
            "read_stable_markdown",
            lambda *_args, _reason=reason: (_ for _ in ()).throw(
                fp.StableFileError(_reason)
            ),
        )
        with db.reader() as conn, pytest.raises(RuntimeError, match=detail):
            snapshot._assert_reconciled_markdown(conn, vault)


def test_snapshot_reconciliation_rejects_unsafe_entries_and_skips_internal(tmp_path):
    db = Database(tmp_path / "scan.db")
    db.ensure_schema()
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = vault / "link.md"
    link.symlink_to(outside)
    with db.reader() as conn, pytest.raises(ValueError, match="unsafe vault path"):
        snapshot._assert_reconciled_markdown(conn, vault)
    link.unlink()

    if hasattr(os, "mkfifo"):
        fifo = vault / "special"
        os.mkfifo(fifo)
        with db.reader() as conn, pytest.raises(ValueError, match="regular file"):
            snapshot._assert_reconciled_markdown(conn, vault)
        fifo.unlink()

    (vault / ".trash").mkdir()
    (vault / ".trash" / "old.md").write_text("old", encoding="utf-8")
    (vault / "_templates").mkdir()
    (vault / "_templates" / "template.md").write_text("template", encoding="utf-8")
    with db.reader() as conn:
        snapshot._assert_reconciled_markdown(conn, vault)
        assert snapshot._database_validation_errors(conn) == []


def _checkpoint(db: Database) -> None:
    path = db.path
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_staged_database_reports_invalid_sqlite(tmp_path, monkeypatch):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not sqlite")
    monkeypatch.setattr(snapshot, "_database_validation_errors", lambda _conn: [])
    with pytest.raises(ValueError, match="invalid snapshot database"):
        snapshot._validate_staged_database(bad, tmp_path, {}, {})


def test_staged_database_detects_missing_revision_after_integrity_checks(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "missing-revision.db")
    db.ensure_schema()
    digest = hashlib.sha256(b"body").hexdigest()
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO documents("
            "path,path_norm,title,version,content_hash,folder,file_state,vector_dirty,"
            "is_deleted,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("note.md", "note.md", "Note", 1, digest, "", "clean", 0, 0, "now", "now"),
        )
    _checkpoint(db)
    monkeypatch.setattr(snapshot, "_database_validation_errors", lambda _conn: [])
    with pytest.raises(ValueError, match="no current revision"):
        snapshot._validate_staged_database(
            db.path,
            tmp_path,
            {"schema_version": SCHEMA_VERSION, "doc_count": 1},
            {},
        )


def test_staged_database_detects_document_hash_mismatch(tmp_path, monkeypatch):
    db = Database(tmp_path / "hash-mismatch.db")
    db.ensure_schema()
    _insert_document(db, body="body")
    with db.writer() as conn:
        conn.execute("UPDATE documents SET content_hash='bad'")
    _checkpoint(db)
    vault = tmp_path / "staged-vault"
    vault.mkdir()
    (vault / "note.md").write_text("body", encoding="utf-8")
    monkeypatch.setattr(snapshot, "_database_validation_errors", lambda _conn: [])
    with pytest.raises(ValueError, match="document hash mismatch"):
        snapshot._validate_staged_database(
            db.path,
            vault,
            {"schema_version": SCHEMA_VERSION, "doc_count": 1},
            {"vault/note.md": {"kind": "managed"}},
        )


def test_best_effort_writer_skips_when_process_writer_lock_is_busy(tmp_path):
    db = Database(tmp_path / "busy-telemetry.db")
    db.ensure_schema()
    locked = threading.Event()
    release = threading.Event()

    def hold_lock():
        with db._write_lock:
            locked.set()
            assert release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert locked.wait(timeout=5)
    try:
        assert db.try_write("UPDATE meta SET v=v") is False
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_rebind_accepts_positive_legacy_epoch_without_revision(tmp_path):
    db = Database(tmp_path / "legacy-positive.db")
    db.initialize("model", 2, EMBEDDING_PIPELINE)
    with db.writer() as conn:
        conn.execute("DELETE FROM meta WHERE k='embedding_revision'")
    rebound = db.rebind_model("model", 2, EMBEDDING_PIPELINE, "commit")
    assert rebound.epoch == 2 and rebound.revision == "commit"


def test_live_snapshot_helpers_cover_removed_deleted_and_unmanaged_generations(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "live-helper.db")
    db.ensure_schema()
    vault = tmp_path / "live-helper-vault"
    vault.mkdir()

    first = _insert_document(db, path="removed.md")
    with db.reader() as conn:
        removed_clone = conn.execute(
            "SELECT id,path,path_norm,version,file_state FROM documents WHERE id=?",
            (first,),
        ).fetchone()
    with db.writer() as conn:
        conn.execute("DELETE FROM documents WHERE id=?", (first,))
    assert snapshot._live_managed_change_explains_disk(
        db, removed_clone, disk_body=None
    )

    second = _insert_document(db, path="deleted.md")
    with db.reader() as conn:
        deleted_clone = conn.execute(
            "SELECT id,path,path_norm,version,file_state FROM documents WHERE id=?",
            (second,),
        ).fetchone()
    with db.writer() as conn:
        conn.execute("UPDATE documents SET is_deleted=1 WHERE id=?", (second,))
    assert snapshot._live_managed_change_explains_disk(
        db, deleted_clone, disk_body=None
    )

    with db.writer() as conn:
        conn.execute("DELETE FROM documents")
    unmanaged = vault / "unmanaged.md"
    unmanaged.write_text("external", encoding="utf-8")
    with db.reader() as conn, pytest.raises(RuntimeError, match="unmanaged"):
        snapshot._assert_reconciled_markdown(conn, vault)

    monkeypatch.setattr(
        fp,
        "read_stable_markdown",
        lambda *_args: (_ for _ in ()).throw(fp.StableFileError("file_changed")),
    )
    with db.reader() as conn, pytest.raises(RuntimeError, match="unstable unmanaged.md"):
        snapshot._assert_reconciled_markdown(conn, vault, live_db=db)
