"""Focused branch coverage for crash-safe document projection and recovery."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.services import documents as documents_module
from llm_wiki.services.documents import CleanupIssue
from llm_wiki.util import now_iso, path_norm


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _queue_absent_cleanup(ctx, doc_id: int, rel: str) -> None:
    with ctx.db.writer() as conn:
        version = int(
            conn.execute(
                "SELECT version FROM documents WHERE id=?", (doc_id,)
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,?,?)",
            (doc_id, rel, path_norm(rel), version, now_iso()),
        )


def _queue_existing_cleanup(ctx, doc_id: int, rel: str) -> None:
    target = ctx.docs.vault / rel
    signature = fp.confined_file_signature(ctx.docs.vault, target, missing_ok=False)
    assert signature is not None
    with ctx.db.writer() as conn:
        version = int(
            conn.execute(
                "SELECT version FROM documents WHERE id=?", (doc_id,)
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
            "VALUES(?,?,?,1,?,?,?,?,?,?,?)",
            (
                doc_id,
                rel,
                path_norm(rel),
                signature.dev,
                signature.ino,
                signature.size,
                signature.mtime_ns,
                signature.ctime_ns,
                version,
                now_iso(),
            ),
        )


def _leave_purge_intent(ctx, admin, rel: str, monkeypatch) -> int:
    docs = ctx.docs
    doc_id = _doc_id(ctx, rel)
    original_finish = docs._finish_purge

    def interrupt(_doc_id: int):
        raise RuntimeError("post-commit interruption")

    monkeypatch.setattr(docs, "_finish_purge", interrupt)
    with pytest.raises(RuntimeError, match="post-commit interruption"):
        docs.purge(admin, rel)
    monkeypatch.setattr(docs, "_finish_purge", original_finish)
    return doc_id


def _start_projector(ctx, doc_id: int, *, name: str):
    results: list[fp.ProjectionResult] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(ctx.docs._project_current(doc_id))
        except BaseException as exc:  # surfaced in the main pytest thread
            errors.append(exc)
        finally:
            ctx.db.close()

    worker = threading.Thread(target=run, name=name)
    worker.start()
    return worker, results, errors


def test_staged_deleted_generation_cannot_overwrite_public_restore(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "restore-after-stage.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "restore-after-stage.md")
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.delete(editor, "restore-after-stage.md")
    monkeypatch.setattr(docs, "_require_projection", original_require)

    deleted_staged = threading.Event()
    release_deleted = threading.Event()
    real_stage = fp.stage_text

    def pause_deleted_stage(vault, target, body):
        staged = real_stage(vault, target, body)
        if threading.current_thread().name == "staged-delete-projector":
            deleted_staged.set()
            assert release_deleted.wait(timeout=10), "restore did not release projector"
        return staged

    monkeypatch.setattr(fp, "stage_text", pause_deleted_stage)
    worker, results, errors = _start_projector(
        ctx, doc_id, name="staged-delete-projector"
    )
    assert deleted_staged.wait(timeout=10), "deleted generation was not staged"
    try:
        restored = docs.restore(editor, "restore-after-stage.md")
    finally:
        release_deleted.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert restored["restored"] is True
    assert len(results) == 1 and results[0].settled
    assert (docs.vault / "restore-after-stage.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert not (docs.vault / ".trash" / "restore-after-stage.md").exists()
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT version,is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert (row["version"], row["is_deleted"], row["file_state"]) == (3, 0, "clean")


def test_cleanup_commit_then_public_move_preserves_new_path_and_cleanup_intent(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "cleanup-old.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "cleanup-old.md")
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, "cleanup-old.md", "cleanup-current.md")
    monkeypatch.setattr(docs, "_require_projection", original_require)

    cleanup_committed = threading.Event()
    release_cleanup = threading.Event()
    real_writer = ctx.db.writer

    @contextmanager
    def pause_after_cleanup_commit():
        with real_writer() as conn:
            yield conn
        if (
            threading.current_thread().name == "cleanup-page-projector"
            and not cleanup_committed.is_set()
        ):
            with ctx.db.reader() as conn:
                cleanup_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()[0]
                )
                state = conn.execute(
                    "SELECT file_state FROM documents WHERE id=?", (doc_id,)
                ).fetchone()[0]
            if cleanup_count == 0 and state == "pending":
                cleanup_committed.set()
                assert release_cleanup.wait(timeout=10), "move did not release projector"

    monkeypatch.setattr(ctx.db, "writer", pause_after_cleanup_commit)
    worker, results, errors = _start_projector(
        ctx, doc_id, name="cleanup-page-projector"
    )
    assert cleanup_committed.wait(timeout=10), "cleanup page did not commit"
    try:
        monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
        moved = docs.move(editor, "cleanup-current.md", "cleanup-latest.md")
        monkeypatch.setattr(docs, "_require_projection", original_require)
        with ctx.db.reader() as conn:
            row = conn.execute(
                "SELECT path,file_state FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            intents = conn.execute(
                "SELECT path FROM file_projection_cleanup WHERE doc_id=?",
                (doc_id,),
            ).fetchall()
        assert (row["path"], row["file_state"]) == ("cleanup-latest.md", "pending")
        assert [str(intent["path"]) for intent in intents] == ["cleanup-current.md"]
    finally:
        monkeypatch.setattr(docs, "_require_projection", original_require)
        release_cleanup.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert moved["path"] == "cleanup-latest.md"
    assert len(results) == 1 and results[0].settled
    assert not (docs.vault / "cleanup-old.md").exists()
    assert not (docs.vault / "cleanup-current.md").exists()
    assert (docs.vault / "cleanup-latest.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT path,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["path"], row["file_state"], cleanup_count) == (
        "cleanup-latest.md",
        "clean",
        0,
    )


def test_final_stage_cannot_publish_after_public_delete_and_purge_intent(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "final-old.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "final-old.md")
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, "final-old.md", "final-current.md")
    monkeypatch.setattr(docs, "_require_projection", original_require)

    final_staged = threading.Event()
    release_final = threading.Event()
    real_stage = fp.stage_text

    def pause_final_stage(vault, target, body):
        staged = real_stage(vault, target, body)
        if threading.current_thread().name == "final-stage-projector":
            with ctx.db.reader() as conn:
                cleanup_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()[0]
                )
            if cleanup_count == 0:
                final_staged.set()
                assert release_final.wait(timeout=10), "purge did not release projector"
        return staged

    monkeypatch.setattr(fp, "stage_text", pause_final_stage)
    worker, results, errors = _start_projector(
        ctx, doc_id, name="final-stage-projector"
    )
    assert final_staged.wait(timeout=10), "projector did not enter final stage"
    try:
        docs.delete(editor, "final-current.md")
        _leave_purge_intent(ctx, admin, "final-current.md", monkeypatch)
        with ctx.db.reader() as conn:
            row = conn.execute(
                "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            intent_count = conn.execute(
                "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]
        assert (row["is_deleted"], row["file_state"], intent_count) == (1, "pending", 1)
    finally:
        release_final.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0].reason == "purge_pending"
    assert not (docs.vault / "final-current.md").exists()
    assert (docs.vault / ".trash" / "final-current.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert docs.recover_pending() == 1
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (doc_id,)
        ).fetchone() is None
    assert not (docs.vault / "final-current.md").exists()
    assert not (docs.vault / ".trash" / "final-current.md").exists()


def test_final_stage_retries_after_public_update_and_keeps_latest_revision(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "final-update-old.md", "version one", embed=False)
    doc_id = _doc_id(ctx, "final-update-old.md")
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, "final-update-old.md", "final-update.md")
    monkeypatch.setattr(docs, "_require_projection", original_require)

    final_staged = threading.Event()
    release_final = threading.Event()
    real_stage = fp.stage_text

    def pause_final_stage(vault, target, body):
        staged = real_stage(vault, target, body)
        if threading.current_thread().name == "final-update-projector":
            with ctx.db.reader() as conn:
                cleanup_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                        (doc_id,),
                    ).fetchone()[0]
                )
            if cleanup_count == 0:
                final_staged.set()
                assert release_final.wait(timeout=10), "update did not release projector"
        return staged

    monkeypatch.setattr(fp, "stage_text", pause_final_stage)
    worker, results, errors = _start_projector(
        ctx, doc_id, name="final-update-projector"
    )
    assert final_staged.wait(timeout=10), "projector did not enter final stage"
    try:
        updated = docs.update(
            editor, "final-update.md", 2, "version three", embed=False
        )
    finally:
        release_final.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert errors == []
    assert updated["version"] == 3
    assert len(results) == 1 and results[0].settled
    assert (docs.vault / "final-update.md").read_text(
        encoding="utf-8"
    ) == "version three"
    assert not (docs.vault / "final-update-old.md").exists()
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.version,d.file_state,r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.id=?",
            (doc_id,),
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["version"], row["file_state"], row["body"], cleanup_count) == (
        3,
        "clean",
        "version three",
        0,
    )


def test_projection_state_helpers_enforce_exact_database_fences(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "fence.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "fence.md")

    with ctx.db.reader() as conn:
        snapshot = docs._projection_snapshot(conn, doc_id)
        assert snapshot is not None
        assert docs._projection_snapshot(conn, doc_id + 10_000) is None
        assert docs._projection_token_state(conn, snapshot) == "settled"

    with ctx.db.writer() as conn:
        with pytest.raises(RuntimeError, match="projection fence changed"):
            docs._mark_projection_clean(conn, snapshot, None)

        with pytest.raises(sqlite3.IntegrityError, match="file_state"):
            conn.execute(
                "UPDATE documents SET file_state='unexpected' WHERE id=?", (doc_id,)
            )

        conn.execute("UPDATE documents SET file_state='clean' WHERE id=?", (doc_id,))
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,1,?)",
            (doc_id, "historical.md", path_norm("historical.md"), now_iso()),
        )
        assert docs._projection_token_state(conn, snapshot) == "cleanup_pending"

        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        assert docs._projection_token_state(conn, snapshot) == "missing"


def test_cleanup_schema_rejects_incomplete_signature(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "signature.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "signature.md")
    with ctx.db.writer() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            conn.execute(
                "INSERT INTO file_projection_cleanup("
                "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                "VALUES(?,?,?,1,NULL,NULL,NULL,NULL,NULL,1,?)",
                (doc_id, "historical.md", path_norm("historical.md"), now_iso()),
            )

    assert (docs.vault / "signature.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0] == 0


def test_recovery_rejects_inconsistent_purge_intent_and_preserves_tombstone(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-fence.md", "canonical", embed=False)
    docs.delete(editor, "purge-fence.md")
    doc_id = _doc_id(ctx, "purge-fence.md")

    original_finish = docs._finish_purge

    def interrupt(_doc_id: int):
        raise RuntimeError("post-commit interruption")

    monkeypatch.setattr(docs, "_finish_purge", interrupt)
    with pytest.raises(RuntimeError, match="post-commit interruption"):
        docs.purge(admin, "purge-fence.md")
    monkeypatch.setattr(docs, "_finish_purge", original_finish)
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE document_purge_intents SET version=version+1 WHERE doc_id=?",
            (doc_id,),
        )

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
        (doc_id, "recovery_error")
    ]
    assert (docs.vault / ".trash" / "purge-fence.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intent_count = conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["is_deleted"], row["file_state"], intent_count) == (1, "pending", 1)


def test_projector_argument_and_missing_document_boundaries(ctx):
    docs = ctx.docs
    with pytest.raises(ValueError, match="at least 1"):
        docs._project_current(99_999, max_attempts=0)
    result = docs._project_current(99_999)
    assert (result.settled, result.reason, result.path) == (True, "missing", None)


@pytest.mark.parametrize("failure", ["absence_changed", "unlink_changed"])
def test_cleanup_generation_change_preserves_external_file_and_intent(
    ctx, principals, monkeypatch, failure
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "cleanup-owner.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "cleanup-owner.md")
    historical = docs.vault / "historical-generation.md"
    if failure == "absence_changed":
        _queue_absent_cleanup(ctx, doc_id, "historical-generation.md")
        real_confirm = fp.confirm_confined_absence

        def changed_absence(vault, target):
            if target == historical:
                return False
            return real_confirm(vault, target)

        monkeypatch.setattr(fp, "confirm_confined_absence", changed_absence)
    else:
        historical.write_text("external generation", encoding="utf-8")
        _queue_existing_cleanup(ctx, doc_id, "historical-generation.md")
        real_unlink = fp.unlink_regular

        def changed_unlink(target, *args, **kwargs):
            if target == historical:
                return False
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(fp, "unlink_regular", changed_unlink)

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["cleanup_changed"]
    assert historical.exists() is (failure == "unlink_changed")
    assert (docs.vault / "cleanup-owner.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0]
        intents = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (state, intents) == ("pending", 1)


def test_purge_cleanup_retires_stale_authority_without_removing_new_generations(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-cleanup.md", "canonical", embed=False)
    docs.delete(editor, "purge-cleanup.md")
    docs.create(editor, "owned.md", "owned generation", embed=False)
    doc_id = _leave_purge_intent(ctx, admin, "purge-cleanup.md", monkeypatch)

    external = docs.vault / "external.md"
    external.write_text("external generation", encoding="utf-8")
    removable = docs.vault / "removable.md"
    removable.write_text("old generation", encoding="utf-8")
    unchanged = docs.vault / "unchanged.md"
    unchanged.write_text("preserved generation", encoding="utf-8")
    normally_removed = docs.vault / "normally-removed.md"
    normally_removed.write_text("owned old generation", encoding="utf-8")
    _queue_existing_cleanup(ctx, doc_id, "removable.md")
    _queue_existing_cleanup(ctx, doc_id, "unchanged.md")
    _queue_existing_cleanup(ctx, doc_id, "normally-removed.md")
    for rel in ("purge-cleanup.md", "owned.md", "absence-raced.md", "external.md"):
        _queue_absent_cleanup(ctx, doc_id, rel)

    real_confirm = fp.confirm_confined_absence
    real_unlink = fp.unlink_regular

    def absence_raced(vault, target):
        if target == docs.vault / "absence-raced.md":
            return False
        return real_confirm(vault, target)

    def remove_then_report_changed(target, *args, **kwargs):
        if target == removable:
            assert real_unlink(target, *args, **kwargs)
            return False
        if target == unchanged:
            return False
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(fp, "confirm_confined_absence", absence_raced)
    monkeypatch.setattr(fp, "unlink_regular", remove_then_report_changed)

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 1
    assert report.issues == ()
    assert docs.get("owned.md")["content"] == "owned generation"
    assert external.read_text(encoding="utf-8") == "external generation"
    assert not removable.exists()
    assert unchanged.read_text(encoding="utf-8") == "preserved generation"
    assert not normally_removed.exists()
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (doc_id,)
        ).fetchone() is None


def test_purge_cleanup_io_error_remains_durable_until_next_recovery(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-io.md", "canonical", embed=False)
    docs.delete(editor, "purge-io.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-io.md", monkeypatch)
    _queue_absent_cleanup(ctx, doc_id, "blocked-cleanup.md")
    real_managed = fp.managed_path

    def blocked(vault, rel, *, namespace):
        if rel == "blocked-cleanup.md":
            raise OSError("injected cleanup path failure")
        return real_managed(vault, rel, namespace=namespace)

    monkeypatch.setattr(fp, "managed_path", blocked)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["purge_cleanup_io_error"]
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intents = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        purge_intent = conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["file_state"], intents, purge_intent) == ("pending", 1, 1)

    monkeypatch.setattr(fp, "managed_path", real_managed)
    assert docs.recover_pending() == 1


def test_projector_defers_to_durable_purge_intent(ctx, principals, monkeypatch):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-projector.md", "canonical", embed=False)
    docs.delete(editor, "purge-projector.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-projector.md", monkeypatch)

    result = docs._project_current(doc_id)

    assert not result.settled
    assert result.reason == "purge_pending"
    assert result.is_deleted is True
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intents = conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["is_deleted"], row["file_state"], intents) == (1, "pending", 1)


def test_purge_preserves_intent_when_trash_absence_cannot_be_confirmed(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-trash-race.md", "canonical", embed=False)
    docs.delete(editor, "purge-trash-race.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-trash-race.md", monkeypatch)
    trash = docs.vault / ".trash" / "purge-trash-race.md"
    real_signature = fp.confined_file_signature
    real_confirm = fp.confirm_confined_absence

    def raced_signature(vault, target, *, missing_ok):
        if target == trash:
            return None
        return real_signature(vault, target, missing_ok=missing_ok)

    def raced_absence(vault, target):
        if target == trash:
            return False
        return real_confirm(vault, target)

    monkeypatch.setattr(fp, "confined_file_signature", raced_signature)
    monkeypatch.setattr(fp, "confirm_confined_absence", raced_absence)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["purge_io_error"]
    assert trash.read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0]
        intent_count = conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (state, intent_count) == ("pending", 1)

    monkeypatch.setattr(fp, "confined_file_signature", real_signature)
    monkeypatch.setattr(fp, "confirm_confined_absence", real_confirm)
    assert docs.recover_pending() == 1


def test_initial_staging_cleanup_failure_does_not_reopen_clean_projection(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "cleanup-warning.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "cleanup-warning.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
    real_cleanup = fp.cleanup_staged

    def cleanup_then_fail(staged):
        real_cleanup(staged)
        raise OSError("injected staged cleanup failure")

    monkeypatch.setattr(fp, "cleanup_staged", cleanup_then_fail)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 1
    assert report.issues == ()
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_initial_stage_failure_leaves_no_temp_file_and_recovers(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "stage-failure.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "stage-failure.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
    real_stage = fp.stage_text
    monkeypatch.setattr(
        fp,
        "stage_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stage failed")),
    )

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["io_error"]
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"
    monkeypatch.setattr(fp, "stage_text", real_stage)
    assert docs.recover_pending() == 1


def test_final_stage_failure_keeps_published_revision_pending_for_retry(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "final-stage-failure.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "final-stage-failure.md")
    _queue_absent_cleanup(ctx, doc_id, "retired.md")
    real_stage = fp.stage_text

    def fail_when_cleanup_has_finished(vault, target, body):
        with ctx.db.reader() as conn:
            cleanup_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                    (doc_id,),
                ).fetchone()[0]
            )
        if cleanup_count == 0:
            raise OSError("injected final stage failure")
        return real_stage(vault, target, body)

    monkeypatch.setattr(fp, "stage_text", fail_when_cleanup_has_finished)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["io_error"]
    assert (docs.vault / "final-stage-failure.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.version,d.file_state,r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.id=?",
            (doc_id,),
        ).fetchone()
    assert (row["version"], row["file_state"], row["body"]) == (
        created["version"],
        "pending",
        "canonical",
    )

    monkeypatch.setattr(fp, "stage_text", real_stage)
    assert docs.recover_pending() == 1


def test_final_staged_cleanup_warning_does_not_reopen_clean_projection(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "final-cleanup-warning.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "final-cleanup-warning.md")
    _queue_absent_cleanup(ctx, doc_id, "retired.md")
    real_cleanup = fp.cleanup_staged

    def cleanup_then_warn_when_projection_is_clean(staged):
        real_cleanup(staged)
        with ctx.db.reader() as conn:
            row = conn.execute(
                "SELECT file_state FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            cleanup_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                    (doc_id,),
                ).fetchone()[0]
            )
        if row["file_state"] == "clean" and cleanup_count == 0:
            raise OSError("injected final staged cleanup failure")

    monkeypatch.setattr(fp, "cleanup_staged", cleanup_then_warn_when_projection_is_clean)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 1
    assert report.issues == ()
    assert (docs.vault / "final-cleanup-warning.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["file_state"], cleanup_count) == ("clean", 0)


@pytest.mark.parametrize(
    ("race", "expected_reason"),
    [
        ("intent_removed_with_issue", "purge_intent_missing"),
        ("intent_removed_before_finish", "purge_intent_missing"),
        ("cleanup_reappeared", "purge_cleanup_pending"),
    ],
)
def test_purge_revalidates_intent_and_cleanup_between_writer_phases(
    ctx, principals, monkeypatch, race, expected_reason
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-phase.md", "canonical", embed=False)
    docs.delete(editor, "purge-phase.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-phase.md", monkeypatch)
    original_batch = docs._process_purge_cleanup_batch

    def race_batch(conn, intent, *, after_norm, batch_size=64):
        if race == "intent_removed_with_issue":
            conn.execute(
                "DELETE FROM document_purge_intents WHERE doc_id=?", (intent.doc_id,)
            )
            return None, (CleanupIssue("old.md", "purge_cleanup_io_error"),)
        if race == "intent_removed_before_finish":
            conn.execute(
                "DELETE FROM document_purge_intents WHERE doc_id=?", (intent.doc_id,)
            )
            return None, ()
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,?,?)",
            (
                intent.doc_id,
                "late-cleanup.md",
                path_norm("late-cleanup.md"),
                intent.version,
                now_iso(),
            ),
        )
        return None, ()

    monkeypatch.setattr(docs, "_process_purge_cleanup_batch", race_batch)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == [expected_reason]
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["is_deleted"], row["file_state"]) == (1, "pending")
    assert cleanup_count == int(race == "cleanup_reappeared")
    monkeypatch.setattr(docs, "_process_purge_cleanup_batch", original_batch)


def test_finish_purge_without_intent_reports_live_and_missing_documents(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "purge-intent-absent.md", "canonical", embed=False)
    with ctx.db.reader() as conn:
        doc_id = int(
            conn.execute(
                "SELECT id FROM documents WHERE path_norm=?",
                (path_norm("purge-intent-absent.md"),),
            ).fetchone()["id"]
        )

    live = docs._finish_purge(doc_id)
    missing = docs._finish_purge(doc_id + 1_000_000)

    assert (live.path, live.settled, live.reason) == (
        "purge-intent-absent.md",
        False,
        "purge_intent_missing",
    )
    assert (missing.path, missing.settled, missing.reason) == (None, True, "missing")


def test_purge_ignores_obsolete_cleanup_issue_when_no_cleanup_remains(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-obsolete-issue.md", "canonical", embed=False)
    docs.delete(editor, "purge-obsolete-issue.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-obsolete-issue.md", monkeypatch)

    monkeypatch.setattr(
        docs,
        "_process_purge_cleanup_batch",
        lambda *_args, **_kwargs: (
            None,
            (CleanupIssue("already-retired.md", "purge_cleanup_io_error"),),
        ),
    )
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 1
    assert report.issues == ()
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (doc_id,)
        ).fetchone() is None


def test_purge_tombstone_fence_detects_generation_change(ctx, principals, monkeypatch):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-generation.md", "canonical", embed=False)
    docs.delete(editor, "purge-generation.md")
    doc_id = _leave_purge_intent(ctx, admin, "purge-generation.md", monkeypatch)
    with ctx.db.reader() as conn:
        before_version = int(
            conn.execute(
                "SELECT version FROM documents WHERE id=?", (doc_id,)
            ).fetchone()[0]
        )

    real_unresolve = documents_module.graph.unresolve_incoming

    def change_generation(conn, current_id):
        real_unresolve(conn, current_id)
        conn.execute(
            "UPDATE documents SET version=version+1 WHERE id=?", (current_id,)
        )

    monkeypatch.setattr(documents_module.graph, "unresolve_incoming", change_generation)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [issue.reason for issue in report.issues] == ["recovery_error"]
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,file_state,version FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intent_count = conn.execute(
            "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["is_deleted"], row["file_state"], row["version"], intent_count) == (
        1,
        "pending",
        before_version,
        1,
    )


def test_recovery_stops_when_remaining_frontier_disappears(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    ids = []
    for rel in ("frontier-first.md", "frontier-gone.md"):
        docs.create(editor, rel, rel, embed=False)
        ids.append(_doc_id(ctx, rel))
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id IN (?,?)", ids)
    original_project = docs._project_current

    def settle_first_and_remove_frontier(current_id):
        result = original_project(current_id)
        with ctx.db.writer() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (ids[1],))
        return result

    monkeypatch.setattr(docs, "_project_current", settle_first_and_remove_frontier)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 1
    assert report.issues == ()
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (ids[0],)
        ).fetchone()[0] == "clean"
        assert conn.execute(
            "SELECT 1 FROM documents WHERE id=?", (ids[1],)
        ).fetchone() is None


def test_recovery_reports_error_even_when_error_path_lookup_fails(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "lookup-failure.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "lookup-failure.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
    projection_failed = threading.Event()
    real_reader = ctx.db.reader

    def fail_projection(_doc_id):
        projection_failed.set()
        raise RuntimeError("projection failed")

    @contextmanager
    def reader_fails_after_projection():
        if projection_failed.is_set():
            raise sqlite3.OperationalError("path lookup failed")
        with real_reader() as conn:
            yield conn

    monkeypatch.setattr(docs, "_project_current", fail_projection)
    monkeypatch.setattr(ctx.db, "reader", reader_fails_after_projection)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert len(report.issues) == 1
    assert (report.issues[0].doc_id, report.issues[0].path, report.issues[0].reason) == (
        doc_id,
        None,
        "recovery_error",
    )


def test_recovery_reports_none_path_when_failing_document_disappears(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "disappearing.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "disappearing.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    def disappear_then_fail(current_id):
        with ctx.db.writer() as conn:
            conn.execute("DELETE FROM documents WHERE id=?", (current_id,))
        raise RuntimeError("projection failed after removal")

    monkeypatch.setattr(docs, "_project_current", disappear_then_fail)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert len(report.issues) == 1
    assert (report.issues[0].doc_id, report.issues[0].path, report.issues[0].reason) == (
        doc_id,
        None,
        "recovery_error",
    )
