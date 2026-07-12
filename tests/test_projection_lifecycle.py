"""Crash-safe document lifecycle projection and two-phase purge contracts."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.services.auth import Principal
from llm_wiki.services.documents import ProjectionPendingError
from llm_wiki.services.errors import ConflictError
from llm_wiki.util import path_norm


class _PostCommitInterruption(RuntimeError):
    """Simulate process loss after the canonical DB transaction commits."""


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _defer_lifecycle_projection(docs, monkeypatch):
    """Queue common-projector completions after their durable DB commits."""
    original_require = docs._require_projection
    delayed: list[int] = []

    def defer(doc_id: int):
        delayed.append(int(doc_id))
        return None

    monkeypatch.setattr(docs, "_require_projection", defer)
    return original_require, delayed


def _replay_latest_in_reverse(docs, monkeypatch, original_require, delayed) -> None:
    monkeypatch.setattr(docs, "_require_projection", original_require)
    for doc_id in reversed(delayed):
        original_require(doc_id)


def _purge_rows(ctx, rel: str) -> tuple[int, int, int]:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
        doc_count = int(row is not None)
        if row is None:
            return 0, 0, 0
        doc_id = int(row["id"])
        revisions = int(
            conn.execute(
                "SELECT COUNT(*) FROM revisions WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]
        )
        intents = int(
            conn.execute(
                "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?",
                (doc_id,),
            ).fetchone()[0]
        )
    return doc_count, revisions, intents


def test_delayed_update_then_delete_converges_to_latest_deleted_revision(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "race.md", "version one", embed=False)
    doc_id = _doc_id(ctx, "race.md")
    original_require, delayed = _defer_lifecycle_projection(docs, monkeypatch)

    docs.update(editor, "race.md", 1, "version two", embed=False)
    docs.delete(editor, "race.md", base_version=2)

    assert delayed == [doc_id, doc_id]
    _replay_latest_in_reverse(docs, monkeypatch, original_require, delayed)
    assert not (docs.vault / "race.md").exists()
    assert (docs.vault / ".trash" / "race.md").read_text(
        encoding="utf-8"
    ) == "version two"


def test_delayed_delete_then_restore_converges_to_live_only(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "restore-race.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "restore-race.md")
    monkeypatch.setattr(docs, "_embed", lambda _doc_id: None)
    original_require, delayed = _defer_lifecycle_projection(docs, monkeypatch)

    docs.delete(editor, "restore-race.md")
    docs.restore(editor, "restore-race.md")

    assert delayed == [doc_id, doc_id]
    _replay_latest_in_reverse(docs, monkeypatch, original_require, delayed)
    assert (docs.vault / "restore-race.md").read_text(
        encoding="utf-8"
    ) == "canonical"
    assert not (docs.vault / ".trash" / "restore-race.md").exists()


def test_delayed_restore_then_delete_converges_to_trash_only(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "delete-race.md", "canonical", embed=False)
    docs.delete(editor, "delete-race.md")
    doc_id = _doc_id(ctx, "delete-race.md")
    monkeypatch.setattr(docs, "_embed", lambda _doc_id: None)
    original_require, delayed = _defer_lifecycle_projection(docs, monkeypatch)

    docs.restore(editor, "delete-race.md")
    docs.delete(editor, "delete-race.md")

    assert delayed == [doc_id, doc_id]
    _replay_latest_in_reverse(docs, monkeypatch, original_require, delayed)
    assert not (docs.vault / "delete-race.md").exists()
    assert (docs.vault / ".trash" / "delete-race.md").read_text(
        encoding="utf-8"
    ) == "canonical"


def test_delete_projects_canonical_revision_instead_of_stale_live_file(
    ctx, principals
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "canonical-delete.md", "canonical body", embed=False)
    (docs.vault / "canonical-delete.md").write_text(
        "stale external body", encoding="utf-8"
    )

    docs.delete(editor, "canonical-delete.md")

    assert not (docs.vault / "canonical-delete.md").exists()
    assert (docs.vault / ".trash" / "canonical-delete.md").read_text(
        encoding="utf-8"
    ) == "canonical body"


def test_restore_removes_a_stale_trash_generation(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "restore.md", "canonical body", embed=False)
    docs.delete(editor, "restore.md")
    trash = docs.vault / ".trash" / "restore.md"
    trash.write_text("stale trash generation", encoding="utf-8")
    monkeypatch.setattr(docs, "_embed", lambda _doc_id: None)

    docs.restore(editor, "restore.md")

    assert (docs.vault / "restore.md").read_text(
        encoding="utf-8"
    ) == "canonical body"
    assert not trash.exists()


def test_recovery_finishes_delete_together_with_pending_move_cleanup(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "old.md", "canonical body", embed=False)
    doc_id = _doc_id(ctx, "old.md")
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, "old.md", "new.md")

    def interrupt(*_args, **_kwargs):
        raise _PostCommitInterruption

    monkeypatch.setattr(docs, "_require_projection", interrupt)
    with pytest.raises(_PostCommitInterruption):
        docs.delete(editor, "new.md")

    monkeypatch.setattr(docs, "_require_projection", original_require)
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        cleanup_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                (doc_id,),
            ).fetchone()[0]
        )
    assert (row["is_deleted"], row["file_state"], cleanup_count) == (1, "pending", 1)

    assert docs.recover_pending() == 1
    assert not (docs.vault / "old.md").exists()
    assert not (docs.vault / "new.md").exists()
    assert (docs.vault / ".trash" / "new.md").read_text(
        encoding="utf-8"
    ) == "canonical body"


def test_purge_intent_survives_post_commit_interruption_and_recovery_audits_once(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    base_admin = principals["admin"]
    admin = Principal(base_admin.user_id, base_admin.username, base_admin.role, via="mcp")
    docs.create(editor, "purge-crash.md", "canonical", embed=False)
    docs.delete(editor, "purge-crash.md")
    doc_id = _doc_id(ctx, "purge-crash.md")
    missing = object()
    original_finish = getattr(docs, "_finish_purge", missing)

    def interrupt(*_args, **_kwargs):
        raise _PostCommitInterruption

    monkeypatch.setattr(docs, "_finish_purge", interrupt, raising=False)
    with pytest.raises(_PostCommitInterruption):
        docs.purge(admin, "purge-crash.md")

    with ctx.db.reader() as conn:
        intent = conn.execute(
            "SELECT version,actor,via FROM document_purge_intents WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT is_deleted,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        purge_audits = int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='doc_purge' AND target=?",
                ("purge-crash.md",),
            ).fetchone()[0]
        )
    assert intent is not None
    assert (intent["actor"], intent["via"]) == ("admin", "mcp")
    assert (row["is_deleted"], row["file_state"], purge_audits) == (1, "pending", 0)

    if original_finish is missing:
        monkeypatch.delattr(docs, "_finish_purge")
    else:
        monkeypatch.setattr(docs, "_finish_purge", original_finish)
    assert docs.recover_pending() == 1
    assert _purge_rows(ctx, "purge-crash.md") == (0, 0, 0)
    assert not (docs.vault / ".trash" / "purge-crash.md").exists()
    with ctx.db.reader() as conn:
        audits = conn.execute(
            "SELECT actor,via FROM audit_log WHERE action='doc_purge' AND target=?",
            ("purge-crash.md",),
        ).fetchall()
    assert [(row["actor"], row["via"]) for row in audits] == [("admin", "mcp")]


def test_purge_trash_unlink_failure_is_durable_and_retried(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-retry.md", "canonical", embed=False)
    docs.delete(editor, "purge-retry.md")
    trash = docs.vault / ".trash" / "purge-retry.md"
    real_unlink = fp.unlink_regular
    failed = False

    def fail_once(target, *args, **kwargs):
        nonlocal failed
        if Path(target) == trash and not failed:
            failed = True
            raise OSError("injected purge trash unlink failure")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(fp, "unlink_regular", fail_once)
    try:
        docs.purge(admin, "purge-retry.md")
    except (OSError, ProjectionPendingError):
        pass

    assert failed
    assert _purge_rows(ctx, "purge-retry.md")[0:3] == (1, 2, 1)
    assert trash.exists()

    monkeypatch.setattr(fp, "unlink_regular", real_unlink)
    assert docs.recover_pending() == 1
    assert _purge_rows(ctx, "purge-retry.md") == (0, 0, 0)
    assert not trash.exists()


def test_pending_delete_with_cleanup_conflict_can_enter_durable_purge(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "old.md", "canonical", embed=False)
    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, "old.md", "new.md")
    monkeypatch.setattr(docs, "_require_projection", original_require)

    replacement = docs.vault.parent / "external-old.md"
    replacement.write_text("external generation", encoding="utf-8")
    replacement.replace(docs.vault / "old.md")

    with pytest.raises(ProjectionPendingError) as deleted:
        docs.delete(editor, "new.md")
    assert deleted.value.result.reason == "cleanup_changed"
    assert deleted.value.result.current_installed

    result = docs.purge(admin, "new.md")

    assert result["purged"] is True
    assert (docs.vault / "old.md").read_text(
        encoding="utf-8"
    ) == "external generation"
    assert _purge_rows(ctx, "new.md") == (0, 0, 0)


def test_purge_commit_failure_after_trash_unlink_recovers_from_absence(
    ctx, principals, monkeypatch
):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge-commit.md", "canonical", embed=False)
    docs.delete(editor, "purge-commit.md")
    doc_id = _doc_id(ctx, "purge-commit.md")
    original_finish = docs._finish_purge

    def interrupt(_doc_id):
        raise _PostCommitInterruption

    monkeypatch.setattr(docs, "_finish_purge", interrupt)
    with pytest.raises(_PostCommitInterruption):
        docs.purge(admin, "purge-commit.md")
    monkeypatch.setattr(docs, "_finish_purge", original_finish)

    real_writer = ctx.db.writer
    failed = False

    @contextmanager
    def fail_final_commit_once():
        nonlocal failed
        with real_writer() as conn:
            yield conn
            if not failed and conn.execute(
                "SELECT 1 FROM documents WHERE id=?", (doc_id,)
            ).fetchone() is None:
                failed = True
                raise sqlite3.OperationalError("injected purge commit failure")

    monkeypatch.setattr(ctx.db, "writer", fail_final_commit_once)
    with pytest.raises(sqlite3.OperationalError):
        original_finish(doc_id)
    assert failed
    assert not (docs.vault / ".trash" / "purge-commit.md").exists()
    assert _purge_rows(ctx, "purge-commit.md") == (1, 2, 1)

    monkeypatch.setattr(ctx.db, "writer", real_writer)
    assert docs.recover_pending() == 1
    assert _purge_rows(ctx, "purge-commit.md") == (0, 0, 0)
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='doc_purge' AND target='purge-commit.md'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["restore", "revive"])
def test_existing_purge_intent_fences_restore_and_tombstone_revive(
    ctx, principals, operation
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "purge-fence.md", "original", embed=False)
    docs.delete(editor, "purge-fence.md")
    doc_id = _doc_id(ctx, "purge-fence.md")
    with ctx.db.writer() as conn:
        row = conn.execute(
            "SELECT path,path_norm,version FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO document_purge_intents("
            "doc_id,path,path_norm,version,actor,via,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                doc_id,
                row["path"],
                row["path_norm"],
                row["version"],
                "admin",
                "web",
                "now",
            ),
        )
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    with pytest.raises(ConflictError):
        if operation == "restore":
            docs.restore(editor, "purge-fence.md")
        else:
            docs.create(editor, "purge-fence.md", "replacement", embed=False)

    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT is_deleted,version,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        intent_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM document_purge_intents WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]
        )
    assert (row["is_deleted"], row["version"], row["file_state"], intent_count) == (
        1,
        2,
        "pending",
        1,
    )
