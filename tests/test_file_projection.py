from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp


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
