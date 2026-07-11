"""Adversarial boundaries for durable move cleanup projection."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.util import path_norm


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


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


def _queue_existing_cleanup(ctx, doc_id: int, rel: str) -> None:
    target = ctx.docs.vault / rel
    signature = fp.confined_file_signature(ctx.docs.vault, target)
    assert signature is not None
    with ctx.db.writer() as conn:
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id,
                rel,
                path_norm(rel),
                1,
                signature.dev,
                signature.ino,
                signature.size,
                signature.mtime_ns,
                signature.ctime_ns,
                1,
                "now",
            ),
        )


def test_move_intent_captures_the_database_path_casing(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    canonical_source = "Docs/MixedCase.md"
    caller_source = "docs/mixedcase.md"
    docs.create(editor, canonical_source, "canonical body", embed=False)
    doc_id = _doc_id(ctx, canonical_source)
    expected = fp.confined_file_signature(
        docs.vault, docs.vault / canonical_source
    )
    assert expected is not None

    original_require = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    docs.move(editor, caller_source, "Moved.md")

    with ctx.db.reader() as conn:
        intent = conn.execute(
            "SELECT path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns "
            "FROM file_projection_cleanup WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
    assert intent is not None
    assert (intent["path"], intent["path_norm"], intent["expected_exists"]) == (
        canonical_source,
        path_norm(canonical_source),
        1,
    )
    assert (
        intent["expected_dev"],
        intent["expected_ino"],
        intent["expected_size"],
        intent["expected_mtime_ns"],
        intent["expected_ctime_ns"],
    ) == (
        expected.dev,
        expected.ino,
        expected.size,
        expected.mtime_ns,
        expected.ctime_ns,
    )

    monkeypatch.setattr(docs, "_require_projection", original_require)
    result = original_require(doc_id)
    assert result.settled
    assert not (docs.vault / canonical_source).exists()
    assert (docs.vault / "Moved.md").read_text(encoding="utf-8") == "canonical body"


def test_first_conflict_does_not_starve_the_tail_of_193_cleanup_intents(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "current.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "current.md")
    conflict_rel = "old/000.md"
    conflict = docs.vault / conflict_rel
    conflict.parent.mkdir(parents=True)
    conflict.write_text("external generation", encoding="utf-8")

    intents = [
        (
            doc_id,
            f"old/{number:03}.md",
            path_norm(f"old/{number:03}.md"),
            1,
            "now",
        )
        for number in range(193)
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
    removals: list[int] = []

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
                removals.append(before - after)

    monkeypatch.setattr(ctx.db, "writer", recording_writer)
    result = docs._project_current(doc_id)

    assert not result.settled
    assert result.reason == "cleanup_changed"
    assert _cleanup_paths(ctx, doc_id) == [conflict_rel]
    assert conflict.read_text(encoding="utf-8") == "external generation"
    assert sum(removals) == 192
    assert len(removals) >= 4
    assert max(removals) <= 64
    assert (docs.vault / "current.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


@pytest.mark.parametrize("failure", ["unlink", "directory_fsync"])
def test_cleanup_commits_successful_intents_and_recovers_the_failed_one(
    ctx, principals, monkeypatch, failure
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "current.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "current.md")
    failed_rel = "old/000-failed/source.md"
    successful_rel = "old/001-success/source.md"
    failed = docs.vault / failed_rel
    successful = docs.vault / successful_rel
    failed.parent.mkdir(parents=True)
    successful.parent.mkdir(parents=True)
    failed.write_text("failed generation", encoding="utf-8")
    successful.write_text("successful generation", encoding="utf-8")
    _queue_existing_cleanup(ctx, doc_id, failed_rel)
    _queue_existing_cleanup(ctx, doc_id, successful_rel)
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    if failure == "unlink":
        real_unlink = fp.unlink_regular

        def failing_unlink(target, *args, **kwargs):
            if Path(target) == failed:
                raise OSError("injected cleanup unlink failure")
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(fp, "unlink_regular", failing_unlink)
    else:
        real_fsync = fp.os.fsync
        failed_parent_identity = (
            int(failed.parent.stat().st_dev),
            int(failed.parent.stat().st_ino),
        )
        injected = False

        def failing_fsync(fd):
            nonlocal injected
            opened = os.fstat(fd)
            opened_identity = (int(opened.st_dev), int(opened.st_ino))
            if (
                not injected
                and opened_identity == failed_parent_identity
                and not failed.exists()
            ):
                injected = True
                raise OSError("injected cleanup directory fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr(fp.os, "fsync", failing_fsync)

    result = docs._project_current(doc_id)

    assert not result.settled
    assert result.reason == "cleanup_io_error"
    assert _cleanup_paths(ctx, doc_id) == [failed_rel]
    assert not successful.exists()
    if failure == "unlink":
        assert failed.read_text(encoding="utf-8") == "failed generation"
        monkeypatch.setattr(fp, "unlink_regular", real_unlink)
    else:
        assert injected
        assert not failed.exists()
        monkeypatch.setattr(fp.os, "fsync", real_fsync)
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"

    assert docs.recover_pending() == 1
    assert _cleanup_paths(ctx, doc_id) == []
    assert not failed.exists()
    assert (docs.vault / "current.md").read_text(encoding="utf-8") == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"
