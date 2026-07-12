"""Focused branch coverage for crash-safe document projection and recovery."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace

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


def test_recovery_retry_exhaustion_cleans_each_staged_generation(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "retry.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "retry.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    cleaned: list[object] = []
    real_cleanup = fp.cleanup_staged

    def clean_then_report(staged):
        real_cleanup(staged)
        cleaned.append(staged)

    monkeypatch.setattr(docs, "_projection_token_state", lambda *_a, **_kw: "changed")
    monkeypatch.setattr(fp, "cleanup_staged", clean_then_report)

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [(issue.reason, issue.attempts) for issue in report.issues] == [
        ("target_changed", 3)
    ]
    assert len(cleaned) == 3
    assert not any((docs.vault / ".tmp").iterdir())
    assert (docs.vault / "retry.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


@pytest.mark.parametrize("failure", ["stage", "cleanup"])
def test_recovery_final_publication_failure_keeps_latest_target_and_retries(
    ctx, principals, monkeypatch, failure
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "latest.md", "version one", embed=False)
    updated = docs.update(editor, "latest.md", 1, "version two", embed=False)
    doc_id = _doc_id(ctx, "latest.md")
    _queue_absent_cleanup(ctx, doc_id, "already-absent.md")

    real_stage = fp.stage_text
    real_cleanup = fp.cleanup_staged
    stage_count = 0
    cleanup_count = 0

    def fail_second_stage(vault, target, body):
        nonlocal stage_count
        stage_count += 1
        if failure == "stage" and stage_count == 2:
            raise OSError("injected final stage failure")
        return real_stage(vault, target, body)

    def cleanup_then_fail_second(staged):
        nonlocal cleanup_count
        cleanup_count += 1
        real_cleanup(staged)
        if failure == "cleanup" and cleanup_count == 2:
            raise OSError("injected final cleanup failure")

    monkeypatch.setattr(fp, "stage_text", fail_second_stage)
    monkeypatch.setattr(fp, "cleanup_staged", cleanup_then_fail_second)

    report = docs._recover_pending_report(page_size=1)

    if failure == "stage":
        assert report.recovered == 0
        assert [issue.reason for issue in report.issues] == ["io_error"]
        expected_state = "pending"
    else:
        assert report.recovered == 1
        assert report.issues == ()
        expected_state = "clean"
    assert stage_count == 2
    assert (docs.vault / "latest.md").read_text(encoding="utf-8") == "version two"
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.version,d.file_state,r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.id=?",
            (doc_id,),
        ).fetchone()
    assert (row["version"], row["file_state"], row["body"]) == (
        updated["version"],
        expected_state,
        "version two",
    )

    monkeypatch.setattr(fp, "stage_text", real_stage)
    monkeypatch.setattr(fp, "cleanup_staged", real_cleanup)
    assert docs.recover_pending() == int(failure == "stage")


def test_projector_argument_and_missing_document_boundaries(ctx):
    docs = ctx.docs
    with pytest.raises(ValueError, match="at least 1"):
        docs._project_current(99_999, max_attempts=0)
    result = docs._project_current(99_999)
    assert (result.settled, result.reason, result.path) == (True, "missing", None)


@pytest.mark.parametrize(
    ("token_state", "expected_reason", "settled"),
    [
        ("missing", "missing", True),
        ("settled", "already_settled", True),
        ("purge_pending", "purge_intent_missing", False),
        ("cleanup_pending", "cleanup_pending", False),
        ("invalid", "recovery_error", False),
    ],
)
def test_recovery_revalidates_staged_generation_before_publication(
    ctx, principals, monkeypatch, token_state, expected_reason, settled
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "staged-fence.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "staged-fence.md")
    projected = docs.vault / "staged-fence.md"
    projected.write_text("external generation", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    monkeypatch.setattr(
        docs, "_projection_token_state", lambda *_args, **_kwargs: token_state
    )
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    if settled:
        assert report.issues == ()
    else:
        assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
            (doc_id, expected_reason)
        ]
    assert projected.read_text(encoding="utf-8") == "external generation"
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.version,d.file_state,r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.id=?",
            (doc_id,),
        ).fetchone()
    assert (row["version"], row["file_state"], row["body"]) == (
        1,
        "pending",
        "canonical",
    )


@pytest.mark.parametrize(
    ("terminal", "expected_reason", "settled"),
    [
        ("missing", "missing", True),
        ("settled", "already_settled", True),
        ("purge_pending", "purge_intent_missing", False),
        ("cleanup_pending", "cleanup_pending", False),
        ("invalid", "recovery_error", False),
    ],
)
def test_cleanup_page_revalidates_generation_before_removing_history(
    ctx, principals, monkeypatch, terminal, expected_reason, settled
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "cleanup-fence.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "cleanup-fence.md")
    _queue_absent_cleanup(ctx, doc_id, "historical.md")
    states = iter(("current_cleanup", terminal))
    monkeypatch.setattr(
        docs, "_projection_token_state", lambda *_args, **_kwargs: next(states)
    )

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    if settled:
        assert report.issues == ()
    else:
        assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
            (doc_id, expected_reason)
        ]
    assert (docs.vault / "cleanup-fence.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0]
        intents = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (state, intents) == ("pending", 1)


@pytest.mark.parametrize(
    ("terminal", "expected_reason", "settled"),
    [
        ("missing", "missing", True),
        ("settled", "already_settled", True),
        ("purge_pending", "purge_intent_missing", False),
        ("cleanup_pending", "cleanup_pending", False),
        ("invalid", "recovery_error", False),
    ],
)
def test_final_publication_revalidates_generation_after_cleanup(
    ctx, principals, monkeypatch, terminal, expected_reason, settled
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "final-fence.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "final-fence.md")
    _queue_absent_cleanup(ctx, doc_id, "already-gone.md")
    states = iter(("current_cleanup", "current_cleanup", "current_cleanup", terminal))
    monkeypatch.setattr(
        docs, "_projection_token_state", lambda *_args, **_kwargs: next(states)
    )

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    if settled:
        assert report.issues == ()
    else:
        assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
            (doc_id, expected_reason)
        ]
    assert (docs.vault / "final-fence.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0]
        intents = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (state, intents) == ("pending", 0)


def test_recovery_finishes_purge_when_projector_discovers_intent(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "late-purge.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "late-purge.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    finished: list[int] = []
    monkeypatch.setattr(
        docs,
        "_project_current",
        lambda current_id: fp.ProjectionResult(
            current_id, "late-purge.md", False, False, "purge_pending", 1
        ),
    )

    def finish(current_id: int):
        finished.append(current_id)
        return fp.ProjectionResult(
            current_id, "late-purge.md", False, False, "purge_intent_missing", 1
        )

    monkeypatch.setattr(docs, "_finish_purge", finish)
    report = docs._recover_pending_report(page_size=1)

    assert finished == [doc_id]
    assert report.recovered == 0
    assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
        (doc_id, "purge_intent_missing")
    ]


@pytest.mark.parametrize("phase", ["cleanup", "final"])
def test_generation_change_retries_from_a_fresh_snapshot(
    ctx, principals, monkeypatch, phase
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "generation-retry.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "generation-retry.md")
    _queue_absent_cleanup(ctx, doc_id, "old-generation.md")
    if phase == "cleanup":
        states = iter(("current_cleanup", "changed", "settled"))
    else:
        states = iter(
            (
                "current_cleanup",
                "current_cleanup",
                "current_cleanup",
                "changed",
                "settled",
            )
        )
    monkeypatch.setattr(
        docs, "_projection_token_state", lambda *_args, **_kwargs: next(states)
    )

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert report.issues == ()
    assert (docs.vault / "generation-retry.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert not any((docs.vault / ".tmp").iterdir())
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


def test_clean_cleanup_reopen_uses_exact_snapshot_fence(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "reopen-fence.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "reopen-fence.md")
    _queue_absent_cleanup(ctx, doc_id, "historical.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='clean' WHERE id=?", (doc_id,))
    with ctx.db.reader() as conn:
        actual = docs._projection_snapshot(conn, doc_id)
    assert actual is not None
    stale = replace(actual, path="stale.md")
    monkeypatch.setattr(docs, "_projection_snapshot", lambda *_args: stale)

    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 0
    assert [(issue.reason, issue.attempts) for issue in report.issues] == [
        ("target_changed", 3)
    ]
    assert (docs.vault / "reopen-fence.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT path,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intents = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    assert (row["path"], row["file_state"], intents) == ("reopen-fence.md", "clean", 1)


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
