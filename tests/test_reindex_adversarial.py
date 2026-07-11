"""Adversarial contracts for generation-safe external reconciliation."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki import indexing
from llm_wiki.util import path_norm


def _write(vault: Path, rel: str, body: str) -> Path:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _replace(target: Path, body: str) -> None:
    replacement = target.with_name(f".{target.name}.reindex-swap")
    replacement.write_text(body, encoding="utf-8")
    os.replace(replacement, target)


def _doc_row(ctx, rel: str):
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id,path,version,content_hash,file_state,is_deleted "
            "FROM documents WHERE path_norm=?",
            (path_norm(rel),),
        ).fetchone()
    assert row is not None
    return row


def _defer_move_projection(docs, monkeypatch) -> object:
    original = docs._require_projection
    monkeypatch.setattr(docs, "_require_projection", lambda _doc_id: None)
    return original


def test_casefold_collision_skips_both_paths_without_database_side_effects(ctx):
    vault = ctx.settings.vault_path
    _write(vault, "A.md", "# Upper\n\nupper generation")
    _write(vault, "a.md", "# Lower\n\nlower generation")

    report = ctx.docs.reindex_all()

    assert report["created"] == 0
    assert report["updated"] == 0
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == [
        {"path": "A.md", "reason": "path_collision", "attempts": 1},
        {"path": "a.md", "reason": "path_collision", "attempts": 1},
    ]
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM documents WHERE path_norm=?",
                (path_norm("a.md"),),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='doc_reconcile'").fetchone()[
                0
            ]
            == 0
        )


def test_symlink_alias_is_unreadable_without_colliding_with_regular_path(ctx):
    vault = ctx.settings.vault_path
    _write(vault, "A.md", "# Canonical\n\nregular generation")
    (vault / "a.md").symlink_to(vault / "A.md")

    report = ctx.docs.reindex_all()

    assert report["created"] == 1
    assert report["skipped_conflicts"] == [
        {"path": "a.md", "reason": "file_unreadable", "attempts": 1}
    ]
    assert ctx.docs.get("A.md")["content"] == "# Canonical\n\nregular generation"


def test_case_only_external_rename_preserves_identity_and_adds_rename_revision(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    vault = ctx.settings.vault_path
    docs.create(editor, "Case.md", "# Case\n\ncanonical", embed=False)
    before = _doc_row(ctx, "Case.md")
    os.replace(vault / "Case.md", vault / "case.md")

    report = docs.reindex_all()

    after = _doc_row(ctx, "case.md")
    assert report["renamed"] == 1
    assert report["created"] == 0
    assert report["renames"] == ["Case.md -> case.md"]
    assert report["skipped_conflicts"] == []
    assert (after["id"], after["path"], after["version"]) == (
        before["id"],
        "case.md",
        2,
    )
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version,op FROM revisions WHERE doc_id=? ORDER BY version",
            (after["id"],),
        ).fetchall()
    assert [(row["version"], row["op"]) for row in revisions] == [
        (1, "create"),
        (2, "rename"),
    ]


def test_exact_cleanup_generation_is_removed_before_reindex_can_adopt_it(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "a-old.md", "canonical owner body", embed=False)
    owner_id = int(_doc_row(ctx, "a-old.md")["id"])
    original_projection = _defer_move_projection(docs, monkeypatch)
    docs.move(editor, "a-old.md", "z-current.md")
    monkeypatch.setattr(docs, "_require_projection", original_projection)

    reads: list[str] = []
    real_read = fp.read_stable_markdown

    def record_read(vault: Path, path: Path):
        reads.append(path.relative_to(vault).as_posix())
        return real_read(vault, path)

    monkeypatch.setattr(fp, "read_stable_markdown", record_read)
    report = docs.reindex_all()

    assert report["recovered_pending"] == 1
    assert report["created"] == 0
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == []
    assert reads == ["z-current.md"]
    assert not (docs.vault / "a-old.md").exists()
    assert (docs.vault / "z-current.md").read_text(encoding="utf-8") == ("canonical owner body")
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT file_state FROM documents WHERE id=?", (owner_id,)).fetchone()[0]
            == "clean"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                (owner_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM revisions WHERE doc_id=?", (owner_id,)).fetchone()[0]
            == 2
        )


def test_mismatched_cleanup_generation_is_adopted_and_owner_becomes_clean(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "a-old.md", "canonical owner body", embed=False)
    owner_id = int(_doc_row(ctx, "a-old.md")["id"])
    original_projection = _defer_move_projection(docs, monkeypatch)
    docs.move(editor, "a-old.md", "z-current.md")
    monkeypatch.setattr(docs, "_require_projection", original_projection)
    _replace(docs.vault / "a-old.md", "# External\n\nnew generation")

    report = docs.reindex_all()

    adopted = _doc_row(ctx, "a-old.md")
    owner = _doc_row(ctx, "z-current.md")
    assert report["created"] == 1
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == []
    assert adopted["id"] != owner_id
    assert owner["id"] == owner_id
    assert owner["file_state"] == "clean"
    assert docs.get("a-old.md")["content"] == "# External\n\nnew generation"
    assert docs.get("z-current.md")["content"] == "canonical owner body"
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                (owner_id,),
            ).fetchone()[0]
            == 0
        )


def test_file_change_during_prepared_publish_rolls_back_stale_revision_and_index(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "publish-race.md"
    docs.create(editor, "publish-race.md", "# Managed\n\nbase", embed=False)
    _replace(target, "# External two\n\nstale candidate")
    real_publish = indexing.publish_prepared
    calls = 0

    def change_during_publish(conn, doc_id, title, folder, prepared):
        nonlocal calls
        calls += 1
        real_publish(conn, doc_id, title, folder, prepared)
        if calls == 1:
            _replace(target, "# External three\n\nwinning generation")

    monkeypatch.setattr(indexing, "publish_prepared", change_during_publish)
    report = docs.reindex_all()

    row = _doc_row(ctx, "publish-race.md")
    assert calls == 2
    assert report["updated"] == 1
    assert report["retried"] == 1
    assert report["skipped_conflicts"] == []
    assert row["version"] == 2
    assert docs.get("publish-race.md")["content"] == ("# External three\n\nwinning generation")
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version,body FROM revisions WHERE doc_id=? ORDER BY version",
            (row["id"],),
        ).fetchall()
        indexed = conn.execute(
            "SELECT body FROM documents_fts WHERE rowid=?", (row["id"],)
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='doc_reconcile' AND target='publish-race.md'"
        ).fetchone()[0]
    assert [(revision["version"], revision["body"]) for revision in revisions] == [
        (1, "# Managed\n\nbase"),
        (2, "# External three\n\nwinning generation"),
    ]
    assert "winning generation" in indexed
    assert "stale candidate" not in indexed
    assert audit_count == 1


def test_file_change_after_commit_converges_with_one_revision_per_generation(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "post-commit-race.md"
    docs.create(editor, "post-commit-race.md", "# Managed\n\nbase", embed=False)
    _replace(target, "# External two\n\ncommitted generation")
    real_writer = ctx.db.writer
    changed = False

    @contextmanager
    def change_after_first_commit():
        nonlocal changed
        with real_writer() as conn:
            yield conn
        if not changed:
            changed = True
            _replace(target, "# External three\n\nlatest generation")

    monkeypatch.setattr(ctx.db, "writer", change_after_first_commit)
    report = docs.reindex_all()

    row = _doc_row(ctx, "post-commit-race.md")
    assert changed
    assert report["updated"] == 1
    assert report["retried"] == 1
    assert report["skipped_conflicts"] == []
    assert row["version"] == 3
    assert docs.get("post-commit-race.md")["content"] == ("# External three\n\nlatest generation")
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version,body,op FROM revisions WHERE doc_id=? ORDER BY version",
            (row["id"],),
        ).fetchall()
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='doc_reconcile' AND target='post-commit-race.md'"
        ).fetchone()[0]
    assert [(revision["version"], revision["op"]) for revision in revisions] == [
        (1, "create"),
        (2, "external-reconcile"),
        (3, "external-reconcile"),
    ]
    assert revisions[1]["body"] == "# External two\n\ncommitted generation"
    assert revisions[2]["body"] == "# External three\n\nlatest generation"
    assert audit_count == 2


def test_tombstone_restored_after_stable_read_retries_without_skip_audit(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "restore-race.md"
    docs.create(editor, "restore-race.md", "canonical managed body", embed=False)
    docs.delete(editor, "restore-race.md")
    _write(docs.vault, "restore-race.md", "external stale body")
    real_read = fp.read_stable_markdown
    restored = False

    def restore_after_first_read(vault: Path, path: Path):
        nonlocal restored
        stable = real_read(vault, path)
        if not restored:
            restored = True
            docs.restore(editor, "restore-race.md")
        return stable

    monkeypatch.setattr(fp, "read_stable_markdown", restore_after_first_read)
    report = docs.reindex_all()

    row = _doc_row(ctx, "restore-race.md")
    assert restored
    assert report["retried"] == 1
    assert report["unchanged"] == 1
    assert report["skipped_deleted"] == []
    assert report["skipped_conflicts"] == []
    assert (row["is_deleted"], row["file_state"]) == (0, "clean")
    assert target.read_text(encoding="utf-8") == "canonical managed body"
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action='doc_reconcile_skip' AND target='restore-race.md'"
            ).fetchone()[0]
            == 0
        )


def test_source_disappearing_after_scan_is_adopted_as_external_rename(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Same generation\n\nrename identity"
    docs.create(editor, "source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "source.md")["id"])
    target = _write(docs.vault, "target.md", body)
    source = docs.vault / "source.md"
    real_read = fp.read_stable_markdown
    removed = False

    def remove_source_after_scan(vault: Path, path: Path):
        nonlocal removed
        stable = real_read(vault, path)
        if path == target and not removed:
            removed = True
            source.unlink()
        return stable

    monkeypatch.setattr(fp, "read_stable_markdown", remove_source_after_scan)

    report = docs.reindex_all()

    assert removed
    assert report["renamed"] == 1
    assert report["created"] == 0
    assert report["skipped_conflicts"] == []
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT id,path FROM documents WHERE is_deleted=0 ORDER BY id"
        ).fetchall()
    assert [(int(row["id"]), str(row["path"])) for row in rows] == [(source_id, "target.md")]


def test_source_reappearing_after_external_rename_is_requeued_in_same_run(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nexternal rename"
    docs.create(editor, "old.md", body, embed=False)
    original_id = int(_doc_row(ctx, "old.md")["id"])
    os.replace(docs.vault / "old.md", docs.vault / "new.md")
    real_is_current = fp.stable_markdown_is_current
    checks = 0

    def recreate_source_after_commit(stable):
        nonlocal checks
        current = real_is_current(stable)
        if stable.relative_path == "new.md":
            checks += 1
            if checks == 3:
                _write(docs.vault, "old.md", body)
        return current

    monkeypatch.setattr(fp, "stable_markdown_is_current", recreate_source_after_commit)

    report = docs.reindex_all()

    assert checks >= 3
    assert report["renamed"] == 1
    assert report["created"] == 1
    assert report["skipped_conflicts"] == []
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT id,path FROM documents WHERE is_deleted=0 ORDER BY path"
        ).fetchall()
    assert [str(row["path"]) for row in rows] == ["new.md", "old.md"]
    assert next(int(row["id"]) for row in rows if row["path"] == "new.md") == original_id


def test_failed_case_only_reconcile_reports_exact_database_path_missing(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "Case.md", "case body", embed=False)
    os.replace(docs.vault / "Case.md", docs.vault / "case.md")

    def never_stable(_vault: Path, _path: Path):
        raise fp.StableFileError("file_changed")

    monkeypatch.setattr(fp, "read_stable_markdown", never_stable)

    report = docs.reindex_all()

    assert report["skipped_conflicts"] == [
        {"path": "case.md", "reason": "file_changed", "attempts": 3}
    ]
    assert report["missing_files"] == ["Case.md"]


def test_purge_recovery_removing_scanned_target_is_not_a_reindex_conflict(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "purged.md", "canonical", embed=False)
    docs.delete(editor, "purged.md")
    target = _write(docs.vault, "purged.md", "external stale generation")
    doc_id = int(_doc_row(ctx, "purged.md")["id"])
    real_signature = fp.confined_file_signature
    intent_added = False

    def request_purge_during_scan(vault, path, *, missing_ok=False):
        nonlocal intent_added
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == target and not intent_added:
            intent_added = True
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
                        "cli",
                        "now",
                    ),
                )
                conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
        return signature

    real_finish_purge = docs._finish_purge

    def purge_and_remove_scanned_file(target_id: int):
        result = real_finish_purge(target_id)
        if result.settled and target.exists():
            target.unlink()
        return result

    monkeypatch.setattr(fp, "confined_file_signature", request_purge_during_scan)
    monkeypatch.setattr(docs, "_finish_purge", purge_and_remove_scanned_file)

    report = docs.reindex_all()

    assert intent_added
    assert report["recovered_pending"] == 1
    assert report["skipped_conflicts"] == []
    assert report["missing_files"] == []
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == 0
        )


def test_final_integrity_retry_still_counts_an_earlier_committed_generation(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "integrity-race.md"
    docs.create(editor, "integrity-race.md", "managed", embed=False)
    _replace(target, "external committed")
    real_writer = ctx.db.writer
    writer_calls = 0

    @contextmanager
    def fail_after_first_reindex_commit():
        nonlocal writer_calls
        writer_calls += 1
        if writer_calls in (2, 3):
            raise sqlite3.IntegrityError("deterministic race")
        with real_writer() as conn:
            yield conn
        if writer_calls == 1:
            _replace(target, "external uncommitted")

    monkeypatch.setattr(ctx.db, "writer", fail_after_first_reindex_commit)

    report = docs.reindex_all()

    assert report["updated"] == 1
    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [
        {"path": "integrity-race.md", "reason": "target_changed", "attempts": 3}
    ]
    assert docs.get("integrity-race.md")["content"] == "external committed"


def test_later_unreadable_generation_still_counts_an_earlier_commit(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "unreadable-race.md"
    docs.create(editor, "unreadable-race.md", "managed", embed=False)
    _replace(target, "external committed")
    real_writer = ctx.db.writer
    real_read = fp.read_stable_markdown
    reads = 0

    @contextmanager
    def change_after_first_commit():
        with real_writer() as conn:
            yield conn
        _replace(target, "external next")

    def unreadable_on_second_read(vault: Path, path: Path):
        nonlocal reads
        reads += 1
        if reads >= 2:
            raise fp.StableFileError("file_unreadable")
        return real_read(vault, path)

    monkeypatch.setattr(ctx.db, "writer", change_after_first_commit)
    monkeypatch.setattr(fp, "read_stable_markdown", unreadable_on_second_read)

    report = docs.reindex_all()

    assert report["updated"] == 1
    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [
        {"path": "unreadable-race.md", "reason": "file_unreadable", "attempts": 3}
    ]
    assert docs.get("unreadable-race.md")["content"] == "external committed"


def test_later_pending_recovery_still_counts_an_earlier_commit(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "pending-after-commit.md"
    docs.create(editor, "pending-after-commit.md", "managed", embed=False)
    doc_id = int(_doc_row(ctx, "pending-after-commit.md")["id"])
    _replace(target, "external committed")
    real_writer = ctx.db.writer
    writer_calls = 0

    @contextmanager
    def make_pending_after_first_commit():
        nonlocal writer_calls
        writer_calls += 1
        with real_writer() as conn:
            yield conn
        if writer_calls == 1:
            _replace(target, "external next")
            with real_writer() as conn:
                conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))

    def never_settle(target_id: int, *, max_attempts: int = 3):
        assert target_id == doc_id
        return fp.ProjectionResult(
            doc_id,
            "pending-after-commit.md",
            False,
            False,
            "install_failed",
            max_attempts,
        )

    monkeypatch.setattr(ctx.db, "writer", make_pending_after_first_commit)
    monkeypatch.setattr(docs, "_project_current", never_settle)

    report = docs.reindex_all()

    assert report["updated"] == 1
    assert report["retried"] == 1
    assert report["skipped_conflicts"] == [
        {
            "path": "pending-after-commit.md",
            "reason": "pending_projection",
            "attempts": 3,
        }
    ]
    assert report["missing_files"] == []


def test_unsafe_case_rename_is_confined_to_one_path_and_batch_continues(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "Case.md", "case body", embed=False)
    os.replace(docs.vault / "Case.md", docs.vault / "case.md")
    _write(docs.vault, "z-ok.md", "independent body")
    real_absence = fp.confirm_confined_absence

    def reject_old_path(vault: Path, path: Path):
        if Path(path).name == "Case.md":
            raise fp.UnsafeProjectionPath("unsafe source")
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", reject_old_path)

    report = docs.reindex_all()

    assert report["created"] == 1
    assert report["skipped_conflicts"] == [
        {"path": "case.md", "reason": "file_unreadable", "attempts": 1}
    ]
    assert docs.get("z-ok.md")["content"] == "independent body"


def test_unsafe_exact_path_is_not_reported_as_missing(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    target = docs.vault / "unsafe.md"
    outside = docs.vault.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    docs.create(editor, "unsafe.md", "managed", embed=False)
    target.unlink()
    target.symlink_to(outside)

    report = docs.reindex_all()

    assert report["missing_files"] == []
    assert report["skipped_conflicts"] == [
        {"path": "unsafe.md", "reason": "file_unreadable", "attempts": 3}
    ]


def test_unsettled_pending_document_is_not_reported_as_missing(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "pending-missing.md", "managed", embed=False)
    doc_id = int(_doc_row(ctx, "pending-missing.md")["id"])
    (docs.vault / "pending-missing.md").unlink()
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))

    def never_settle(target_id: int, *, max_attempts: int = 3):
        return fp.ProjectionResult(
            target_id,
            "pending-missing.md",
            False,
            False,
            "install_failed",
            max_attempts,
        )

    monkeypatch.setattr(docs, "_project_current", never_settle)

    report = docs.reindex_all()

    assert report["missing_files"] == []
    assert report["skipped_conflicts"] == [
        {"path": "pending-missing.md", "reason": "pending_projection", "attempts": 3}
    ]


def test_same_hash_pending_source_blocks_target_insert_until_projection_settles(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nunsettled source"
    docs.create(editor, "source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "source.md")["id"])
    (docs.vault / "source.md").unlink()
    _write(docs.vault, "target.md", body)
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?",
            (source_id,),
        )

    def never_settle(target_id: int, *, max_attempts: int = 3):
        assert target_id == source_id
        return fp.ProjectionResult(
            target_id,
            "source.md",
            False,
            False,
            "install_failed",
            max_attempts,
        )

    monkeypatch.setattr(docs, "_project_current", never_settle)

    report = docs.reindex_all()

    assert report["created"] == 0
    assert {(conflict["path"], conflict["reason"]) for conflict in report["skipped_conflicts"]} == {
        ("source.md", "pending_projection"),
        ("target.md", "pending_projection"),
    }
    with ctx.db.reader() as conn:
        rows = conn.execute("SELECT id,path,file_state FROM documents ORDER BY id").fetchall()
    assert [(int(row["id"]), str(row["path"]), str(row["file_state"])) for row in rows] == [
        (source_id, "source.md", "pending")
    ]


def test_unsafe_same_hash_source_does_not_block_safe_target_creation(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nunsafe source alias"
    docs.create(editor, "source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "source.md")["id"])
    outside = docs.vault.parent / "outside-source.md"
    outside.write_text(body, encoding="utf-8")
    (docs.vault / "source.md").unlink()
    (docs.vault / "source.md").symlink_to(outside)
    _write(docs.vault, "target.md", body)

    report = docs.reindex_all()

    assert report["created"] == 1
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == [
        {"path": "source.md", "reason": "file_unreadable", "attempts": 3}
    ]
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT id,path FROM documents WHERE is_deleted=0 ORDER BY path"
        ).fetchall()
    assert [str(row["path"]) for row in rows] == ["source.md", "target.md"]
    assert next(int(row["id"]) for row in rows if row["path"] == "source.md") == source_id
    assert docs.get("target.md")["content"] == body


def test_processed_source_conflict_is_replaced_by_single_requeued_outcome(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nrequeue exactly once"
    source = docs.vault / "a-source.md"
    target = docs.vault / "z-target.md"
    docs.create(editor, "a-source.md", body, embed=False)
    original_id = int(_doc_row(ctx, "a-source.md")["id"])
    _write(docs.vault, "z-target.md", body)
    real_read = fp.read_stable_markdown
    source_failures = 0

    def fail_then_remove_processed_source(vault: Path, path: Path):
        nonlocal source_failures
        if path == source and source_failures < 3:
            source_failures += 1
            if source_failures == 3:
                source.unlink()
            raise fp.StableFileError("file_changed")
        return real_read(vault, path)

    real_is_current = fp.stable_markdown_is_current
    target_checks = 0

    def recreate_source_after_rename(stable):
        nonlocal target_checks
        current = real_is_current(stable)
        if stable.path == target:
            target_checks += 1
            if target_checks == 3:
                _write(docs.vault, "a-source.md", body)
        return current

    monkeypatch.setattr(fp, "read_stable_markdown", fail_then_remove_processed_source)
    monkeypatch.setattr(fp, "stable_markdown_is_current", recreate_source_after_rename)

    report = docs.reindex_all()

    assert source_failures == 3
    assert target_checks >= 3
    assert report["created"] == 1
    assert report["renamed"] == 1
    assert report["updated"] == 0
    assert report["unchanged"] == 0
    assert report["skipped_conflicts"] == []
    assert report["renames"] == ["a-source.md -> z-target.md"]
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT id,path FROM documents WHERE is_deleted=0 ORDER BY path"
        ).fetchall()
    assert [str(row["path"]) for row in rows] == ["a-source.md", "z-target.md"]
    assert next(int(row["id"]) for row in rows if row["path"] == "z-target.md") == original_id


def test_final_missing_check_revalidates_rows_raced_by_managed_move_and_delete(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "delete-old.md", "delete body", embed=False)
    docs.create(editor, "move-old.md", "move body", embed=False)
    (docs.vault / "delete-old.md").unlink()
    (docs.vault / "move-old.md").unlink()
    raced: set[str] = set()
    real_signature = fp.confined_file_signature

    def race_final_missing_stat(vault, path, *, missing_ok=False):
        rel = Path(path).relative_to(docs.vault).as_posix()
        if missing_ok and rel in {"delete-old.md", "move-old.md"} and rel not in raced:
            raced.add(rel)
            if rel == "delete-old.md":
                docs.delete(editor, rel)
            else:
                docs.move(editor, rel, "move-new.md")
        return real_signature(vault, path, missing_ok=missing_ok)

    monkeypatch.setattr(fp, "confined_file_signature", race_final_missing_stat)

    report = docs.reindex_all()

    assert raced == {"delete-old.md", "move-old.md"}
    assert report["missing_files"] == []
    assert report["skipped_conflicts"] == []
    assert docs.get("move-new.md")["content"] == "move body"
    with ctx.db.reader() as conn:
        deleted = conn.execute(
            "SELECT is_deleted FROM documents WHERE path_norm=?",
            (path_norm("delete-old.md"),),
        ).fetchone()
    assert deleted is not None and deleted["is_deleted"] == 1


def test_cleanup_owner_recovery_exception_keeps_owner_pending_conflict(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "cleanup-old.md", "canonical owner", embed=False)
    owner_id = int(_doc_row(ctx, "cleanup-old.md")["id"])
    _defer_move_projection(docs, monkeypatch)
    docs.move(editor, "cleanup-old.md", "cleanup-owner.md")
    recovery_calls = 0

    def recovery_raises(target_id: int, *, max_attempts: int = 3):
        nonlocal recovery_calls
        assert target_id == owner_id
        recovery_calls += 1
        raise RuntimeError("deterministic cleanup owner failure")

    monkeypatch.setattr(docs, "_project_current", recovery_raises)

    report = docs.reindex_all()

    assert recovery_calls >= 3
    assert report["created"] == 0
    assert report["renamed"] == 0
    assert report["missing_files"] == []
    assert report["skipped_conflicts"] == [
        {
            "path": "cleanup-owner.md",
            "reason": "pending_projection",
            "attempts": 1,
        }
    ]
    with ctx.db.reader() as conn:
        owner = conn.execute(
            "SELECT path,file_state FROM documents WHERE id=?", (owner_id,)
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
            (owner_id,),
        ).fetchone()[0]
    assert owner is not None
    assert (owner["path"], owner["file_state"]) == ("cleanup-owner.md", "pending")
    assert cleanup_count == 1


def test_peer_disappearing_between_presence_passes_retries_to_ambiguous_insert(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nambiguous after retry"
    docs.create(editor, "a-absent.md", body, embed=False)
    first_id = int(_doc_row(ctx, "a-absent.md")["id"])
    (docs.vault / "a-absent.md").unlink()
    docs.create(editor, "b-present.md", body, embed=False)
    second_id = int(_doc_row(ctx, "b-present.md")["id"])
    present = docs.vault / "b-present.md"
    _write(docs.vault, "z-target.md", body)
    real_absence = fp.confirm_confined_absence
    presence_checks = 0

    def disappear_on_second_presence_pass(vault: Path, path: Path):
        nonlocal presence_checks
        if Path(path) == present:
            presence_checks += 1
            if presence_checks == 2:
                present.unlink()
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", disappear_on_second_presence_pass)

    report = docs.reindex_all()

    assert presence_checks >= 4
    assert report["retried"] == 1
    assert report["created"] == 1
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == []
    target = _doc_row(ctx, "z-target.md")
    assert int(target["id"]) not in {first_id, second_id}
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT id,path FROM documents WHERE is_deleted=0 ORDER BY id"
        ).fetchall()
    assert [(int(row["id"]), str(row["path"])) for row in rows] == [
        (first_id, "a-absent.md"),
        (second_id, "b-present.md"),
        (int(target["id"]), "z-target.md"),
    ]


def test_peer_disappearing_after_rename_commit_reports_source_change(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\npost-commit peer change"
    docs.create(editor, "a-absent.md", body, embed=False)
    renamed_id = int(_doc_row(ctx, "a-absent.md")["id"])
    (docs.vault / "a-absent.md").unlink()
    docs.create(editor, "b-present.md", body, embed=False)
    present = docs.vault / "b-present.md"
    _write(docs.vault, "z-target.md", body)
    real_writer = ctx.db.writer
    disappeared = False

    @contextmanager
    def disappear_after_rename_commit():
        nonlocal disappeared
        with real_writer() as conn:
            yield conn
        if disappeared:
            return
        with ctx.db.reader() as conn:
            renamed = conn.execute(
                "SELECT path FROM documents WHERE id=?", (renamed_id,)
            ).fetchone()
        if renamed is not None and renamed["path"] == "z-target.md":
            present.unlink()
            disappeared = True

    monkeypatch.setattr(ctx.db, "writer", disappear_after_rename_commit)

    report = docs.reindex_all()

    assert disappeared
    assert report["created"] == 0
    assert report["renamed"] == 1
    assert report["renames"] == ["a-absent.md -> z-target.md"]
    assert report["skipped_conflicts"] == [
        {
            "path": "z-target.md",
            "reason": "rename_source_changed",
            "attempts": 1,
        }
    ]
    assert int(_doc_row(ctx, "z-target.md")["id"]) == renamed_id


def test_reappearing_rename_source_stops_after_bounded_requeues(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Oscillating generation\n\nbounded source requeue"
    left = docs.vault / "left.md"
    right = docs.vault / "right.md"
    docs.create(editor, "left.md", body, embed=False)
    original_id = int(_doc_row(ctx, "left.md")["id"])
    os.replace(left, right)
    real_is_current = fp.stable_markdown_is_current
    checks: dict[str, int] = {"left.md": 0, "right.md": 0}
    postcommit_mutations = 0

    def swap_after_each_rename_commit(stable):
        nonlocal postcommit_mutations
        current = real_is_current(stable)
        rel = stable.relative_path
        checks[rel] += 1
        if current and checks[rel] % 3 == 0:
            other = right if rel == "left.md" else left
            _write(docs.vault, other.name, body)
            Path(stable.path).unlink()
            postcommit_mutations += 1
        return current

    monkeypatch.setattr(fp, "stable_markdown_is_current", swap_after_each_rename_commit)

    report = docs.reindex_all()

    assert postcommit_mutations == 7
    assert report["created"] == 0
    assert report["renamed"] == 2
    assert report["skipped_conflicts"] == [
        {
            "path": "left.md",
            "reason": "rename_source_reappeared",
            "attempts": 3,
        }
    ]
    assert int(_doc_row(ctx, "right.md")["id"]) == original_id
    assert not right.exists()
    assert left.read_text(encoding="utf-8") == body


def test_peer_change_survives_target_retry_that_finishes_as_existing_update(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    original = "# Shared generation\n\nrename candidate"
    replacement = "# Changed target\n\nsecond attempt wins"
    docs.create(editor, "a-absent.md", original, embed=False)
    renamed_id = int(_doc_row(ctx, "a-absent.md")["id"])
    (docs.vault / "a-absent.md").unlink()
    docs.create(editor, "b-present.md", original, embed=False)
    peer = docs.vault / "b-present.md"
    target = _write(docs.vault, "z-target.md", original)
    real_writer = ctx.db.writer
    raced = False

    @contextmanager
    def change_peer_and_target_after_rename_commit():
        nonlocal raced
        with real_writer() as conn:
            yield conn
        if raced:
            return
        with ctx.db.reader() as conn:
            renamed = conn.execute(
                "SELECT path FROM documents WHERE id=?", (renamed_id,)
            ).fetchone()
        if renamed is not None and renamed["path"] == "z-target.md":
            peer.unlink()
            _replace(target, replacement)
            raced = True

    monkeypatch.setattr(ctx.db, "writer", change_peer_and_target_after_rename_commit)

    report = docs.reindex_all()

    assert raced
    assert report["retried"] == 1
    assert report["created"] == 0
    assert report["renamed"] == 1
    assert report["updated"] == 0
    assert report["renames"] == ["a-absent.md -> z-target.md"]
    assert {(conflict["path"], conflict["reason"]) for conflict in report["skipped_conflicts"]} == {
        ("z-target.md", "rename_source_changed")
    }
    current = _doc_row(ctx, "z-target.md")
    assert int(current["id"]) == renamed_id
    assert current["version"] == 3
    assert docs.get("z-target.md")["content"] == replacement


def test_transient_initial_scan_disappearance_is_still_adopted(ctx, monkeypatch):
    target = _write(ctx.docs.vault, "transient-scan.md", "stable external body")
    real_signature = fp.confined_file_signature
    failed = False

    def disappear_once(vault, path, *, missing_ok=False):
        nonlocal failed
        if Path(path) == target and not missing_ok and not failed:
            failed = True
            raise FileNotFoundError(target)
        return real_signature(vault, path, missing_ok=missing_ok)

    monkeypatch.setattr(fp, "confined_file_signature", disappear_once)

    report = ctx.docs.reindex_all()

    assert failed
    assert report["created"] == 1
    assert report["skipped_conflicts"] == []
    assert ctx.docs.get("transient-scan.md")["content"] == "stable external body"


def test_transient_source_inspection_error_is_cleared_after_rename_retry(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Stable source\n\ntransient inspection"
    docs.create(editor, "source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "source.md")["id"])
    os.replace(docs.vault / "source.md", docs.vault / "target.md")
    source = docs.vault / "source.md"
    real_absence = fp.confirm_confined_absence
    failed = False

    def fail_source_once(vault: Path, path: Path):
        nonlocal failed
        if Path(path) == source and not failed:
            failed = True
            raise OSError("transient source stat")
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", fail_source_once)

    report = docs.reindex_all()

    assert failed
    assert report["retried"] == 1
    assert report["renamed"] == 1
    assert report["skipped_conflicts"] == []
    assert int(_doc_row(ctx, "target.md")["id"]) == source_id


def test_recoverable_pending_source_is_projected_then_target_retried(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared body\n\nrecover pending dependency"
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "a-source.md")["id"])
    source = docs.vault / "a-source.md"
    target = _write(docs.vault, "z-target.md", body)
    real_read = fp.read_stable_markdown
    injected = False

    def make_source_pending_after_target_read(vault: Path, path: Path):
        nonlocal injected
        stable = real_read(vault, path)
        if Path(path) == target and not injected:
            injected = True
            source.unlink()
            with ctx.db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET file_state='pending' WHERE id=?",
                    (source_id,),
                )
        return stable

    monkeypatch.setattr(fp, "read_stable_markdown", make_source_pending_after_target_read)

    report = docs.reindex_all()

    assert injected
    assert report["recovered_pending"] == 1
    assert report["retried"] == 1
    assert report["created"] == 1
    assert report["skipped_conflicts"] == []
    assert source.read_text(encoding="utf-8") == body
    assert docs.get("z-target.md")["content"] == body


def test_requeued_source_disappearance_after_create_is_not_superseded(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nrequeued then removed"
    old = docs.vault / "old.md"
    new = docs.vault / "new.md"
    docs.create(editor, "old.md", body, embed=False)
    os.replace(old, new)
    real_is_current = fp.stable_markdown_is_current
    checks = {"new.md": 0, "old.md": 0}

    def reappear_then_remove(stable):
        rel = stable.relative_path
        checks[rel] += 1
        if rel == "new.md" and checks[rel] == 3:
            _write(docs.vault, "old.md", body)
        elif rel == "old.md" and checks[rel] == 3:
            old.unlink()
        return real_is_current(stable)

    monkeypatch.setattr(fp, "stable_markdown_is_current", reappear_then_remove)

    report = docs.reindex_all()

    assert report["renamed"] == 1
    assert report["created"] == 1
    assert report["missing_files"] == ["old.md"]
    assert report["skipped_conflicts"] == [
        {"path": "old.md", "reason": "file_disappeared", "attempts": 3}
    ]


def test_successful_rename_clears_prior_source_scan_conflict(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\nsuperseded source conflict"
    source = docs.vault / "a-source.md"
    target = _write(docs.vault, "z-target.md", body)
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "a-source.md")["id"])
    real_read = fp.read_stable_markdown
    failures = 0

    def fail_source_then_remove(vault: Path, path: Path):
        nonlocal failures
        if Path(path) == source and failures < 3:
            failures += 1
            if failures == 3:
                source.unlink()
            raise fp.StableFileError("file_changed")
        return real_read(vault, path)

    monkeypatch.setattr(fp, "read_stable_markdown", fail_source_then_remove)

    report = docs.reindex_all()

    assert failures == 3
    assert report["renamed"] == 1
    assert report["skipped_conflicts"] == []
    assert report["missing_files"] == []
    assert int(_doc_row(ctx, "z-target.md")["id"]) == source_id
    assert target.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("invalid_rel", ["bad\nname.md", " bad.md", "bad\\name.md"])
def test_noncanonical_external_path_is_rejected_without_database_insert(
    ctx, invalid_rel
):
    _write(ctx.docs.vault, invalid_rel, "external body")

    report = ctx.docs.reindex_all()

    assert report["created"] == 0
    assert report["skipped_conflicts"] == [
        {"path": repr(invalid_rel), "reason": "file_unreadable", "attempts": 1}
    ]
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_transient_pending_dependency_recovery_retries_until_target_is_adopted(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared body\n\ntransient pending recovery"
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "a-source.md")["id"])
    source = docs.vault / "a-source.md"
    target = _write(docs.vault, "z-target.md", body)
    real_read = fp.read_stable_markdown
    real_project = docs._project_current
    injected = False
    recovery_calls = 0

    def make_source_pending_after_target_read(vault: Path, path: Path):
        nonlocal injected
        stable = real_read(vault, path)
        if Path(path) == target and not injected:
            injected = True
            source.unlink()
            with ctx.db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET file_state='pending' WHERE id=?",
                    (source_id,),
                )
        return stable

    def transient_recovery(target_id: int, *, max_attempts: int = 3):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            return fp.ProjectionResult(
                target_id,
                "a-source.md",
                False,
                False,
                "io_error",
                1,
            )
        return real_project(target_id, max_attempts=max_attempts)

    monkeypatch.setattr(fp, "read_stable_markdown", make_source_pending_after_target_read)
    monkeypatch.setattr(docs, "_project_current", transient_recovery)

    report = docs.reindex_all()

    assert injected
    assert recovery_calls == 2
    assert report["recovered_pending"] == 1
    assert report["retried"] == 2
    assert report["created"] == 1
    assert report["skipped_conflicts"] == []
    assert source.read_text(encoding="utf-8") == body
    assert docs.get("z-target.md")["content"] == body


def test_transient_case_rename_source_check_retries(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "Case.md", "case body", embed=False)
    original_id = int(_doc_row(ctx, "Case.md")["id"])
    os.replace(docs.vault / "Case.md", docs.vault / "case.md")
    old = docs.vault / "Case.md"
    real_absence = fp.confirm_confined_absence
    failed = False

    def fail_old_once(vault: Path, path: Path):
        nonlocal failed
        if Path(path) == old and not failed:
            failed = True
            raise OSError("transient absence check")
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", fail_old_once)

    report = docs.reindex_all()

    assert failed
    assert report["retried"] == 1
    assert report["renamed"] == 1
    assert report["skipped_conflicts"] == []
    assert int(_doc_row(ctx, "case.md")["id"]) == original_id


def test_transient_postcommit_source_check_does_not_leave_conflict(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# External rename\n\npostcommit source check"
    docs.create(editor, "old.md", body, embed=False)
    original_id = int(_doc_row(ctx, "old.md")["id"])
    old = docs.vault / "old.md"
    os.replace(old, docs.vault / "new.md")
    real_signature = fp.confined_file_signature
    failed = False

    def fail_old_once(vault, path, *, missing_ok=False):
        nonlocal failed
        if Path(path) == old and missing_ok and not failed:
            failed = True
            raise OSError("transient postcommit stat")
        return real_signature(vault, path, missing_ok=missing_ok)

    monkeypatch.setattr(fp, "confined_file_signature", fail_old_once)

    report = docs.reindex_all()

    assert failed
    assert report["renamed"] == 1
    assert report["skipped_conflicts"] == []
    assert int(_doc_row(ctx, "new.md")["id"]) == original_id


def test_present_same_hash_pending_source_does_not_block_external_target_create(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\npresent pending source"
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = int(_doc_row(ctx, "a-source.md")["id"])
    _write(docs.vault, "unrelated.cleanup", "unrelated generation")
    _write(docs.vault, "z-target.md", body)
    with ctx.db.writer() as conn:
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,queued_version,created_at) "
            "VALUES(?,?,?,0,1,'now')",
            (source_id, "unrelated.cleanup", path_norm("unrelated.cleanup")),
        )
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?",
            (source_id,),
        )

    report = docs.reindex_all()

    assert report["created"] == 1
    assert report["renamed"] == 0
    assert {(conflict["path"], conflict["reason"]) for conflict in report["skipped_conflicts"]} == {
        ("a-source.md", "pending_projection")
    }
    assert all(conflict["path"] != "z-target.md" for conflict in report["skipped_conflicts"])
    target = _doc_row(ctx, "z-target.md")
    assert int(target["id"]) != source_id
    assert docs.get("a-source.md")["content"] == body
    assert docs.get("z-target.md")["content"] == body
    with ctx.db.reader() as conn:
        source = conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (source_id,)
        ).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
            (source_id,),
        ).fetchone()[0]
    assert source is not None and source["file_state"] == "pending"
    assert cleanup_count == 1


def test_same_hash_new_inode_at_move_source_is_adopted_and_owner_settles(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Canonical generation\n\nsame hash replacement"
    old = docs.vault / "a-old.md"
    docs.create(editor, "a-old.md", body, embed=False)
    owner_id = int(_doc_row(ctx, "a-old.md")["id"])
    original_signature = fp.confined_file_signature(docs.vault, old)
    assert original_signature is not None
    original_projection = _defer_move_projection(docs, monkeypatch)
    docs.move(editor, "a-old.md", "z-owner.md")
    monkeypatch.setattr(docs, "_require_projection", original_projection)

    _replace(old, body)
    replacement_signature = fp.confined_file_signature(docs.vault, old)
    assert replacement_signature is not None
    assert replacement_signature.ino != original_signature.ino

    report = docs.reindex_all()

    assert report["created"] == 1
    assert report["renamed"] == 0
    assert report["skipped_conflicts"] == []
    assert report["missing_files"] == []
    adopted = _doc_row(ctx, "a-old.md")
    owner = _doc_row(ctx, "z-owner.md")
    assert int(adopted["id"]) != owner_id
    assert int(owner["id"]) == owner_id
    assert owner["file_state"] == "clean"
    assert docs.get("a-old.md")["content"] == body
    assert docs.get("z-owner.md")["content"] == body
    with ctx.db.reader() as conn:
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
            (owner_id,),
        ).fetchone()[0]
    assert cleanup_count == 0
