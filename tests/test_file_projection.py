from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.db import Database
from llm_wiki.util import path_norm, sha256_hex


def test_file_signature_detects_same_length_rewrite_and_atomic_replace(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("one", encoding="utf-8")
    first = fp.file_signature(target)

    target.write_text("two", encoding="utf-8")
    os.utime(target, ns=(first.mtime_ns + 1_000_000, first.mtime_ns + 1_000_000))
    rewritten = fp.file_signature(target)
    assert rewritten != first
    assert rewritten.size == first.size

    replacement = tmp_path / "replacement"
    replacement.write_text("two", encoding="utf-8")
    os.replace(replacement, target)
    replaced = fp.file_signature(target)
    assert replaced.ino != rewritten.ino


def test_stage_install_and_cleanup_are_fsynced_and_generation_safe(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "nested/note.md")
    fsync_modes: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_modes.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(fp.os, "fsync", recording_fsync)
    staged = fp.stage_text(vault, target, "# 제목\n\n본문")
    assert staged.path.parent == vault / ".tmp"
    assert staged.path.is_file()
    assert any(stat.S_ISREG(mode) for mode in fsync_modes)

    installed = fp.install_staged(staged, target)
    assert target.read_text(encoding="utf-8") == "# 제목\n\n본문"
    assert installed.dev == target.lstat().st_dev
    assert installed.ino == target.lstat().st_ino
    assert any(stat.S_ISDIR(mode) for mode in fsync_modes)
    assert fp.cleanup_staged(staged) is False

    second = fp.stage_text(vault, target, "next")
    assert fp.cleanup_staged(second) is True
    assert not second.path.exists()


def test_stage_and_install_fsync_every_affected_directory(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "one/two/note.md")
    synced: set[tuple[int, int]] = set()
    real_fsync = fp.os.fsync

    def recording_fsync(fd: int) -> None:
        value = os.fstat(fd)
        if stat.S_ISDIR(value.st_mode):
            synced.add((int(value.st_dev), int(value.st_ino)))
        real_fsync(fd)

    monkeypatch.setattr(fp.os, "fsync", recording_fsync)
    staged = fp.stage_text(vault, target, "body")
    fp.install_staged(staged, target)
    expected = {
        (int(value.st_dev), int(value.st_ino))
        for value in (
            vault.lstat(),
            (vault / "one").lstat(),
            (vault / "one" / "two").lstat(),
            (vault / ".tmp").lstat(),
        )
    }
    assert expected <= synced


def test_fsync_directory_rejects_generation_swap(tmp_path, monkeypatch):
    directory = tmp_path / "directory"
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"
    directory.mkdir()
    replacement.mkdir()
    real_open = fp.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == directory and not swapped:
            swapped = True
            os.replace(directory, moved)
            os.replace(replacement, directory)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fp.os, "open", swapping_open)
    with pytest.raises(fp.FileGenerationChanged):
        fp.fsync_directory(directory)


def test_install_rejects_replaced_staged_generation(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "note.md")
    staged = fp.stage_text(vault, target, "canonical")
    attacker = staged.path.with_name("replacement.tmp")
    attacker.write_text("tampered!", encoding="utf-8")
    os.replace(attacker, staged.path)

    with pytest.raises(fp.FileGenerationChanged):
        fp.install_staged(staged, target)
    assert not target.exists()
    assert fp.cleanup_staged(staged) is False
    assert staged.path.read_text(encoding="utf-8") == "tampered!"


def test_install_rejects_in_place_staged_mutation(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "note.md")
    staged = fp.stage_text(vault, target, "canonical")
    staged.path.write_text("tampered!", encoding="utf-8")
    os.utime(
        staged.path,
        ns=(staged.signature.mtime_ns, staged.signature.mtime_ns),
    )
    with pytest.raises(fp.FileGenerationChanged):
        fp.install_staged(staged, target)


def test_install_rejects_mutation_between_staged_checks(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "note.md")
    staged = fp.stage_text(vault, target, "canonical")
    real_signature = fp.file_signature
    injected = False

    def mutate_after_lexical_check(path, *, missing_ok=False):
        nonlocal injected
        signature = real_signature(path, missing_ok=missing_ok)
        if Path(path) == staged.path and not injected:
            injected = True
            staged.path.write_text("tampered!", encoding="utf-8")
            os.utime(
                staged.path,
                ns=(staged.signature.mtime_ns, staged.signature.mtime_ns),
            )
        return signature

    monkeypatch.setattr(fp, "file_signature", mutate_after_lexical_check)
    with pytest.raises(fp.FileGenerationChanged):
        fp.install_staged(staged, target)
    assert not target.exists()


def test_install_parent_symlink_swap_never_writes_outside_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    parent = vault / "parent"
    moved = vault / "parent-moved"
    vault.mkdir()
    outside.mkdir()
    parent.mkdir()
    outside_target = outside / "note.md"
    outside_target.write_text("outside", encoding="utf-8")
    target = fp.managed_path(vault, "parent/note.md")
    staged = fp.stage_text(vault, target, "canonical")
    real_replace = fp.os.replace
    swapped = False

    def swap_parent_before_replace(src, dst, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            real_replace(parent, moved)
            parent.symlink_to(outside, target_is_directory=True)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(fp.os, "replace", swap_parent_before_replace)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.install_staged(staged, target)
    assert outside_target.read_text(encoding="utf-8") == "outside"


def test_install_rejects_parent_moved_away_after_anchoring(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    parent = vault / "parent"
    moved = vault / "parent-moved"
    vault.mkdir()
    parent.mkdir()
    target = fp.managed_path(vault, "parent/note.md")
    staged = fp.stage_text(vault, target, "canonical")
    real_replace = fp.os.replace
    swapped = False

    def move_parent_before_replace(src, dst, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            real_replace(parent, moved)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(fp.os, "replace", move_parent_before_replace)
    with pytest.raises(fp.FileGenerationChanged):
        fp.install_staged(staged, target)
    assert not target.exists()
    assert (moved / "note.md").read_text(encoding="utf-8") == "canonical"


def test_managed_path_rejects_parent_and_final_symlinks(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, "link/note.md")

    final = vault / "note.md"
    final.symlink_to(outside / "note.md")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, "note.md")

    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, "../escape.md")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, "/absolute.md")
    with pytest.raises(ValueError):
        fp.managed_path(vault, "note.md", namespace="unknown")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, ".tmp/note.md")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, ".trash/note.md")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, ".TMP/note.md")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, ".Trash/note.md")


def test_stage_text_cannot_target_the_scratch_namespace_directly(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.stage_text(vault, vault / ".tmp" / "note.md", "body")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.stage_text(vault, vault / ".Trash" / "note.md", "body")


def test_stage_rejects_symlinked_scratch_directory(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    (vault / ".tmp").symlink_to(outside, target_is_directory=True)
    target = fp.managed_path(vault, "note.md")

    with pytest.raises(fp.UnsafeProjectionPath):
        fp.stage_text(vault, target, "secret")
    assert list(outside.iterdir()) == []


def test_stage_rejects_non_directory_scratch_and_cross_device(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = fp.managed_path(vault, "note.md")
    (vault / ".tmp").write_text("not a directory", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.stage_text(vault, target, "secret")

    (vault / ".tmp").unlink()
    def reject_cross_device(_scratch_fd, _target_fd, _target):
        raise fp.CrossDeviceProjection("different devices")

    monkeypatch.setattr(fp, "_require_same_device", reject_cross_device)
    with pytest.raises(fp.CrossDeviceProjection):
        fp.stage_text(vault, target, "body")


def test_unlink_regular_requires_the_expected_generation(tmp_path):
    target = tmp_path / "old.md"
    target.write_text("old", encoding="utf-8")
    expected = fp.file_signature(target)
    replacement = tmp_path / "new.md"
    replacement.write_text("new", encoding="utf-8")
    os.replace(replacement, target)

    assert fp.unlink_regular(target, expected=expected) is False
    assert target.read_text(encoding="utf-8") == "new"
    assert fp.unlink_regular(target, expected=fp.file_signature(target)) is True
    assert not target.exists()


def test_unlink_regular_treats_a_missing_parent_as_absent(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert fp.unlink_regular(vault / "gone" / "old.md", vault=vault) is False


def test_directory_chain_closes_fds_when_creation_fsync_fails(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    real_open = fp.os.open
    real_close = fp.os.close
    opened: set[int] = set()

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def tracking_close(fd: int) -> None:
        opened.discard(fd)
        real_close(fd)

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(fp.os, "open", tracking_open)
    monkeypatch.setattr(fp.os, "close", tracking_close)
    monkeypatch.setattr(fp, "_fsync_directory_fd", fail_fsync)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.managed_path(vault, "new/note.md", create_parents=True)
    assert opened == set()


def test_file_signature_missing_ok_only_accepts_enoent(tmp_path):
    assert fp.file_signature(tmp_path / "missing", missing_ok=True) is None
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.file_signature(directory, missing_ok=True)


def test_confined_file_signature_is_anchored_to_the_vault(tmp_path):
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    target = vault / "note.md"
    target.write_text("body", encoding="utf-8")

    assert fp.confined_file_signature(vault, target) == fp.file_signature(target)
    assert fp.confined_file_signature(
        vault, vault / "missing" / "note.md", missing_ok=True
    ) is None

    (vault / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.confined_file_signature(
            vault, vault / "link" / "note.md", missing_ok=True
        )


def test_confirm_confined_absence_fsyncs_the_existing_parent(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    parent = vault / "old"
    parent.mkdir(parents=True)
    synced: list[tuple[int, int]] = []
    real_fsync = fp.os.fsync

    def recording_fsync(fd):
        value = os.fstat(fd)
        if stat.S_ISDIR(value.st_mode):
            synced.append((int(value.st_dev), int(value.st_ino)))
        real_fsync(fd)

    monkeypatch.setattr(fp.os, "fsync", recording_fsync)
    assert fp.confirm_confined_absence(vault, parent / "gone.md")
    assert (int(parent.stat().st_dev), int(parent.stat().st_ino)) in synced

    (parent / "present.md").write_text("body", encoding="utf-8")
    assert not fp.confirm_confined_absence(vault, parent / "present.md")


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        return int(
            conn.execute(
                "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
            ).fetchone()[0]
        )


def test_projector_retries_latest_revision_instead_of_installing_stale_body(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "race.md", "v1")
    doc_id = _doc_id(ctx, "race.md")
    external_db = Database(ctx.db.path)
    real_stage = fp.stage_text
    injected = False

    def stage_then_commit_v3(vault, target, body):
        nonlocal injected
        staged = real_stage(vault, target, body)
        if body == "v2" and not injected:
            injected = True
            # A separate Database instance has a separate process-local lock. The
            # stale stage must therefore be fenced by SQLite's cross-instance writer
            # transaction, not by ctx.db's in-process RLock.
            with external_db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET version=3,content_hash=?,file_state='pending' "
                    "WHERE id=? AND version=2",
                    (sha256_hex("v3"), doc_id),
                )
                conn.execute(
                    "INSERT INTO revisions(doc_id,version,body,title,content_hash,"
                    "author_id,op,via,created_at) VALUES(?,3,'v3','race',?,NULL,'edit','web','now')",
                    (doc_id, sha256_hex("v3")),
                )
        return staged

    monkeypatch.setattr(fp, "stage_text", stage_then_commit_v3)
    try:
        docs.update(editor, "race.md", 1, "v2")
    finally:
        external_db.close()

    assert (docs.vault / "race.md").read_text(encoding="utf-8") == "v3"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT version,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert (row["version"], row["file_state"]) == (3, "clean")


def test_projector_leaves_corrupt_exact_revision_pending(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "corrupt.md", "canonical")
    doc_id = _doc_id(ctx, "corrupt.md")
    projected = docs.vault / "corrupt.md"
    projected.write_text("sentinel", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
        conn.execute(
            "UPDATE revisions SET body='tampered' WHERE doc_id=? AND version=1",
            (doc_id,),
        )

    result = docs._project_current(doc_id)
    assert result.reason == "projection_corrupt" and not result.settled
    assert projected.read_text(encoding="utf-8") == "sentinel"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


def test_purge_intent_created_after_stage_fences_stale_projector(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "purge-race.md", "canonical")
    doc_id = _doc_id(ctx, "purge-race.md")
    live = docs.vault / "purge-race.md"
    live.write_text("external live", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET is_deleted=1,file_state='pending' WHERE id=?",
            (doc_id,),
        )

    real_stage = fp.stage_text
    injected = False

    def stage_then_request_purge(vault, target, body):
        nonlocal injected
        staged = real_stage(vault, target, body)
        if not injected:
            injected = True
            with ctx.db.writer() as conn:
                conn.execute(
                    "INSERT INTO document_purge_intents("
                    "doc_id,path,path_norm,version,actor,via,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (doc_id, "purge-race.md", "purge-race.md", 1, "admin", "web", "now"),
                )
        return staged

    monkeypatch.setattr(fp, "stage_text", stage_then_request_purge)
    result = docs._project_current(doc_id)
    assert result.reason == "purge_pending" and not result.settled
    assert live.read_text(encoding="utf-8") == "external live"
    assert not (docs.vault / ".trash" / "purge-race.md").exists()


def test_deleted_projector_uses_revision_not_stale_live_file(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "deleted.md", "canonical body")
    doc_id = _doc_id(ctx, "deleted.md")
    live = docs.vault / "deleted.md"
    live.write_text("stale disk body", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET is_deleted=1,file_state='pending' WHERE id=?",
            (doc_id,),
        )

    result = docs._project_current(doc_id)
    assert result.transitioned and result.settled
    assert not live.exists()
    assert (docs.vault / ".trash" / "deleted.md").read_text(
        encoding="utf-8"
    ) == "canonical body"


def test_projection_failure_after_replace_stays_pending_and_recovers(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "retry.md", "canonical")
    doc_id = _doc_id(ctx, "retry.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
    real_install = fp.install_staged
    failed = False

    def install_then_fail(staged, target):
        nonlocal failed
        installed = real_install(staged, target)
        if not failed:
            failed = True
            raise OSError("injected post-replace failure")
        return installed

    monkeypatch.setattr(fp, "install_staged", install_then_fail)
    result = docs._project_current(doc_id)
    assert result.reason == "io_error" and not result.settled
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"
    assert docs.recover_pending() == 1


def test_target_directory_fsync_failure_stays_pending_then_converges(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "fsync-retry.md", "canonical after retry")
    doc_id = _doc_id(ctx, "fsync-retry.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))

    real_replace = fp.os.replace
    real_fsync_directory_fd = fp._fsync_directory_fd
    target_replaced = False
    failed = False

    def record_target_replace(*args, **kwargs):
        nonlocal target_replaced
        result = real_replace(*args, **kwargs)
        target_replaced = True
        return result

    def fail_first_directory_fsync_after_replace(fd):
        nonlocal failed
        if target_replaced and not failed:
            failed = True
            raise OSError("injected target directory fsync failure")
        return real_fsync_directory_fd(fd)

    monkeypatch.setattr(fp.os, "replace", record_target_replace)
    monkeypatch.setattr(fp, "_fsync_directory_fd", fail_first_directory_fsync_after_replace)
    result = docs._project_current(doc_id)

    assert result.reason == "io_error" and not result.settled
    assert failed
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"

    monkeypatch.setattr(fp.os, "replace", real_replace)
    monkeypatch.setattr(fp, "_fsync_directory_fd", real_fsync_directory_fd)
    assert docs.recover_pending() == 1
    assert (docs.vault / "fsync-retry.md").read_text(
        encoding="utf-8"
    ) == "canonical after retry"
    assert list((docs.vault / ".tmp").iterdir()) == []
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_bounded_recovery_continues_after_one_document_conflict(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    ids: list[int] = []
    for number in range(3):
        rel = f"pending-{number}.md"
        docs.create(editor, rel, f"body {number}")
        ids.append(_doc_id(ctx, rel))
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id IN (?,?,?)", ids)
    original = docs._project_current

    def conflict_first(doc_id, *, max_attempts=3):
        if doc_id == ids[0]:
            return fp.ProjectionResult(
                doc_id, "pending-0.md", False, False, "target_changed", max_attempts
            )
        return original(doc_id, max_attempts=max_attempts)

    monkeypatch.setattr(docs, "_project_current", conflict_first)
    report = docs._recover_pending_report(page_size=2)
    assert report.recovered == 2
    assert [issue.doc_id for issue in report.issues] == [ids[0]]
    with ctx.db.reader() as conn:
        states = {
            int(row["id"]): row["file_state"]
            for row in conn.execute(
                "SELECT id,file_state FROM documents WHERE id IN (?,?,?)", ids
            )
        }
    assert states == {ids[0]: "pending", ids[1]: "clean", ids[2]: "clean"}


def test_bounded_recovery_continues_after_real_stage_io_error(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    ids: list[int] = []
    for number in range(3):
        rel = f"io-pending-{number}.md"
        docs.create(editor, rel, f"body {number}")
        ids.append(_doc_id(ctx, rel))
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id IN (?,?,?)", ids)

    real_stage = fp.stage_text

    def fail_first_stage(vault, target, body):
        if Path(target).name == "io-pending-0.md":
            raise OSError("injected stage failure")
        return real_stage(vault, target, body)

    monkeypatch.setattr(fp, "stage_text", fail_first_stage)
    report = docs._recover_pending_report(page_size=1)

    assert report.recovered == 2
    assert [(issue.doc_id, issue.reason) for issue in report.issues] == [
        (ids[0], "io_error")
    ]
    with ctx.db.reader() as conn:
        states = {
            int(row["id"]): row["file_state"]
            for row in conn.execute(
                "SELECT id,file_state FROM documents WHERE id IN (?,?,?)", ids
            )
        }
    assert states == {ids[0]: "pending", ids[1]: "clean", ids[2]: "clean"}


def test_recovery_uses_latest_state_after_page_fetch_and_keeps_max_id_frontier(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    ids: list[int] = []
    for number in range(3):
        rel = f"frontier-{number}.md"
        docs.create(editor, rel, f"body {number}")
        ids.append(_doc_id(ctx, rel))
    docs.create(editor, "frontier-late.md", "late body")
    late_id = _doc_id(ctx, "frontier-late.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id IN (?,?,?)", ids)

    external_db = Database(ctx.db.path)
    original = docs._project_current
    injected = False

    def mutate_second_after_first(doc_id, *, max_attempts=3):
        nonlocal injected
        result = original(doc_id, max_attempts=max_attempts)
        if doc_id == ids[0] and not injected:
            injected = True
            latest = "body 1 changed after page fetch"
            with external_db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET version=2,content_hash=?,file_state='pending' "
                    "WHERE id=? AND version=1",
                    (sha256_hex(latest), ids[1]),
                )
                conn.execute(
                    "INSERT INTO revisions(doc_id,version,body,title,content_hash,"
                    "author_id,op,via,created_at) VALUES(?,2,?,'frontier-1',?,NULL,"
                    "'edit','web','now')",
                    (ids[1], latest, sha256_hex(latest)),
                )
                # This ID is beyond the recovery run's initial pending MAX(id), so a
                # newly-pending document is deliberately left for the next sweep.
                conn.execute(
                    "UPDATE documents SET file_state='pending' WHERE id=?", (late_id,)
                )
        return result

    monkeypatch.setattr(docs, "_project_current", mutate_second_after_first)
    try:
        report = docs._recover_pending_report(page_size=2)
    finally:
        external_db.close()

    assert report.recovered == 3
    assert report.issues == ()
    assert (docs.vault / "frontier-1.md").read_text(
        encoding="utf-8"
    ) == "body 1 changed after page fetch"
    with ctx.db.reader() as conn:
        states = {
            int(row["id"]): row["file_state"]
            for row in conn.execute(
                "SELECT id,file_state FROM documents WHERE id IN (?,?,?,?)",
                (*ids, late_id),
            )
        }
    assert states == {
        ids[0]: "clean",
        ids[1]: "clean",
        ids[2]: "clean",
        late_id: "pending",
    }


def test_create_rejects_internal_namespace_before_db_commit(ctx, principals):
    with pytest.raises(fp.UnsafeProjectionPath):
        ctx.docs.create(principals["editor"], ".Trash/hidden.md", "body")
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?", (".trash/hidden.md",)
        ).fetchone() is None
