"""Crash-safe, generation-aware cleanup of paths left behind by document moves."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from llm_wiki.util import path_norm


class _PostCommitInterruption(RuntimeError):
    """Simulate process loss after the move transaction has committed."""


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _defer_move_projection(docs, monkeypatch):
    """Commit moves without running the common projector."""
    original_require = docs._require_projection
    delayed: list[int] = []

    def defer(doc_id: int):
        delayed.append(int(doc_id))
        return None

    monkeypatch.setattr(docs, "_require_projection", defer)
    return original_require, delayed


def _cleanup_paths(ctx, doc_id: int) -> list[str]:
    with ctx.db.reader() as conn:
        return [
            str(row["path"])
            for row in conn.execute(
                "SELECT path FROM file_projection_cleanup WHERE doc_id=? "
                "ORDER BY path_norm",
                (doc_id,),
            )
        ]


def test_move_commit_interruption_recovers_new_path_and_removes_old_path(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "old.md", "canonical body", embed=False)
    doc_id = _doc_id(ctx, "old.md")
    original_require = docs._require_projection

    def interrupt(*_args, **_kwargs):
        raise _PostCommitInterruption

    monkeypatch.setattr(docs, "_require_projection", interrupt)
    with pytest.raises(_PostCommitInterruption):
        docs.move(editor, "old.md", "new.md")

    monkeypatch.setattr(docs, "_require_projection", original_require)
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT path,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert row is not None
    assert (row["path"], row["file_state"]) == ("new.md", "pending")
    assert _cleanup_paths(ctx, doc_id) == ["old.md"]

    assert docs.recover_pending() == 1
    assert not (docs.vault / "old.md").exists()
    assert (docs.vault / "new.md").read_text(encoding="utf-8") == "canonical body"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_delayed_a_to_b_to_c_projectors_converge_only_on_c(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "a.md", "latest canonical body", embed=False)
    doc_id = _doc_id(ctx, "a.md")
    original_require, delayed = _defer_move_projection(docs, monkeypatch)

    docs.move(editor, "a.md", "b.md")
    docs.move(editor, "b.md", "c.md")

    assert delayed == [doc_id, doc_id]
    assert _cleanup_paths(ctx, doc_id) == ["a.md", "b.md"]
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT path,version,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert row is not None
    assert (row["path"], row["version"], row["file_state"]) == (
        "c.md",
        3,
        "pending",
    )

    # Old workers carry only doc_id. Even when their completions arrive in reverse,
    # each must re-read the latest C generation rather than publish A or B state.
    monkeypatch.setattr(docs, "_require_projection", original_require)
    for queued_doc_id in reversed(delayed):
        original_require(queued_doc_id)

    assert not (docs.vault / "a.md").exists()
    assert not (docs.vault / "b.md").exists()
    assert (docs.vault / "c.md").read_text(encoding="utf-8") == "latest canonical body"
    assert _cleanup_paths(ctx, doc_id) == []
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_move_back_to_a_cancels_a_cleanup_intent(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "a.md", "body to preserve", embed=False)
    doc_id = _doc_id(ctx, "a.md")
    original_require, delayed = _defer_move_projection(docs, monkeypatch)

    docs.move(editor, "a.md", "b.md")
    docs.move(editor, "b.md", "a.md")

    assert delayed == [doc_id, doc_id]
    # Returning to A must cancel its old deletion authority. B remains as a missing
    # generation cleanup, which is safe and still has to be durably completed.
    assert _cleanup_paths(ctx, doc_id) == ["b.md"]
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"

    monkeypatch.setattr(docs, "_require_projection", original_require)
    original_require(doc_id)
    assert (docs.vault / "a.md").read_text(encoding="utf-8") == "body to preserve"
    assert not (docs.vault / "b.md").exists()
    assert _cleanup_paths(ctx, doc_id) == []


def test_late_cleanup_never_deletes_an_old_path_reused_by_another_document(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "shared.md", "moved document", embed=False)
    moved_id = _doc_id(ctx, "shared.md")
    original_require, delayed = _defer_move_projection(docs, monkeypatch)

    docs.move(editor, "shared.md", "moved.md")
    assert delayed == [moved_id]
    assert _cleanup_paths(ctx, moved_id) == ["shared.md"]

    monkeypatch.setattr(docs, "_require_projection", original_require)
    docs.create(editor, "shared.md", "new path owner", embed=False)
    new_owner_id = _doc_id(ctx, "shared.md")
    assert new_owner_id != moved_id
    assert (docs.vault / "shared.md").read_text(encoding="utf-8") == "new path owner"

    original_require(moved_id)

    assert (docs.vault / "shared.md").read_text(encoding="utf-8") == "new path owner"
    assert (docs.vault / "moved.md").read_text(encoding="utf-8") == "moved document"
    assert _cleanup_paths(ctx, moved_id) == []
    with ctx.db.reader() as conn:
        states = {
            int(row["id"]): str(row["file_state"])
            for row in conn.execute(
                "SELECT id,file_state FROM documents WHERE id IN (?,?)",
                (moved_id, new_owner_id),
            )
        }
    assert states == {moved_id: "clean", new_owner_id: "clean"}


def test_changed_old_path_signature_is_preserved_and_keeps_move_pending(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "old.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "old.md")
    original_require, delayed = _defer_move_projection(docs, monkeypatch)
    docs.move(editor, "old.md", "new.md")
    assert delayed == [doc_id]
    assert _cleanup_paths(ctx, doc_id) == ["old.md"]

    replacement = docs.vault.parent / "external-generation.md"
    replacement.write_text("external replacement", encoding="utf-8")
    os.replace(replacement, docs.vault / "old.md")

    monkeypatch.setattr(docs, "_require_projection", original_require)
    result = docs._project_current(doc_id)

    assert not result.settled
    assert result.reason == "cleanup_changed"
    assert (docs.vault / "old.md").read_text(encoding="utf-8") == "external replacement"
    assert (docs.vault / "new.md").read_text(encoding="utf-8") == "canonical"
    assert _cleanup_paths(ctx, doc_id) == ["old.md"]
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


def test_sixty_five_cleanup_intents_are_split_across_writer_transactions(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "current.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "current.md")
    intents = [
        (
            doc_id,
            f"old/{number:03}.md",
            path_norm(f"old/{number:03}.md"),
            1,
            "now",
        )
        for number in range(65)
    ]
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )
        conn.executemany(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,?,?)",
            intents,
        )

    real_writer = ctx.db.writer
    removed_per_transaction: list[int] = []

    @contextmanager
    def recording_writer():
        with real_writer() as conn:
            before = int(
                conn.execute(
                    "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                    (doc_id,),
                ).fetchone()[0]
            )
            yield conn
            after = int(
                conn.execute(
                    "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                    (doc_id,),
                ).fetchone()[0]
            )
            if before > after:
                removed_per_transaction.append(before - after)

    monkeypatch.setattr(ctx.db, "writer", recording_writer)
    result = docs._project_current(doc_id)

    assert result.settled and result.transitioned
    assert sum(removed_per_transaction) == 65
    assert len(removed_per_transaction) >= 2
    assert max(removed_per_transaction) <= 64
    assert _cleanup_paths(ctx, doc_id) == []
    assert (docs.vault / "current.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_recovery_reopens_inconsistent_clean_row_with_cleanup_intent(
    ctx, principals
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "current.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "current.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,1,'now')",
            (doc_id, "old/missing.md", path_norm("old/missing.md")),
        )

    assert docs.recover_pending() == 1
    assert _cleanup_paths(ctx, doc_id) == []
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"
