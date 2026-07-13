from __future__ import annotations

import errno
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp

_REAL_FSTAT = os.fstat


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _assert_closed(fds: list[int]) -> None:
    for fd in fds:
        with pytest.raises(OSError) as raised:
            _REAL_FSTAT(fd)
        assert raised.value.errno == errno.EBADF


def test_missing_paths_and_absence_have_distinct_contracts(tmp_path):
    vault = _vault(tmp_path)
    missing = vault / "missing" / "note.md"

    with pytest.raises(FileNotFoundError):
        fp.file_signature(vault / "gone.md")
    with pytest.raises(FileNotFoundError):
        fp.confined_file_signature(vault, missing)
    assert fp.confirm_confined_absence(vault, missing)


def test_directory_fsync_rejects_missing_files_and_bad_descriptors(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.fsync_directory(missing)

    ordinary = tmp_path / "ordinary"
    ordinary.write_text("sentinel", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.fsync_directory(ordinary)

    fd = os.open(ordinary, os.O_RDONLY)
    try:
        with pytest.raises(fp.UnsafeProjectionPath):
            fp._fsync_directory_fd(fd)
    finally:
        os.close(fd)

    directory = tmp_path / "directory"
    directory.mkdir()
    real_open = fp.os.open
    real_fstat = fp.os.fstat
    opened: list[int] = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def file_fstat(fd):
        value = real_fstat(fd)
        if fd in opened:
            return os.stat_result((stat.S_IFREG | 0o600, *value[1:]))
        return value

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(fp.os, "fstat", file_fstat)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.fsync_directory(directory)
    _assert_closed(opened)


def test_vault_and_root_open_failures_close_descriptors(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._vault_root(missing)

    file_root = tmp_path / "file-root"
    file_root.write_text("outside", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._vault_root(file_root)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._open_root_fd(file_root)

    vault = _vault(tmp_path)
    real_open = fp.os.open
    real_fstat = fp.os.fstat
    opened: list[int] = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def changed_fstat(fd):
        value = real_fstat(fd)
        if fd in opened:
            values = list(value)
            values[1] += 1
            return os.stat_result(values)
        return value

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(fp.os, "fstat", changed_fstat)
    with pytest.raises(fp.FileGenerationChanged):
        fp._open_root_fd(vault)
    _assert_closed(opened)


def test_directory_chain_reports_missing_unsafe_and_closes_every_fd(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    with pytest.raises(fp.ProjectionPathMissing):
        with fp._open_directory_chain(vault, ("missing",), create=False):
            pass

    (vault / "file").write_text("external", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        with fp._open_directory_chain(vault, ("file",), create=False):
            pass

    created = vault / "created"
    real_mkdir = fp.os.mkdir

    def race_mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        raise FileExistsError(path)

    monkeypatch.setattr(fp.os, "mkdir", race_mkdir)
    with fp._open_directory_chain(vault, ("created",), create=True) as (_, path):
        assert path == created
    assert created.is_dir()


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        ("nondirectory", fp.UnsafeProjectionPath),
        ("missing", fp.ProjectionPathMissing),
        ("oserror", fp.UnsafeProjectionPath),
        ("runtime", RuntimeError),
    ],
)
def test_directory_chain_closes_open_child_for_every_validation_failure(
    tmp_path, monkeypatch, failure, raised
):
    vault = _vault(tmp_path)
    child = vault / "child"
    child.mkdir()
    real_open = fp.os.open
    real_fstat = fp.os.fstat
    opened: list[int] = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_child_fstat(fd):
        value = real_fstat(fd)
        if len(opened) >= 2 and fd == opened[-1]:
            if failure == "nondirectory":
                values = list(value)
                values[0] = stat.S_IFREG | 0o600
                return os.stat_result(values)
            if failure == "missing":
                raise fp.ProjectionPathMissing("injected disappearance")
            if failure == "oserror":
                raise OSError("injected fstat failure")
            raise RuntimeError("injected validation failure")
        return value

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(fp.os, "fstat", fail_child_fstat)
    with pytest.raises(raised):
        with fp._open_directory_chain(vault, ("child",), create=False):
            pass
    _assert_closed(opened)


def test_directory_chain_closes_root_when_child_open_raises_non_os_error(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    (vault / "child").mkdir()
    real_open = fp.os.open
    opened: list[int] = []

    def fail_before_child_fd(*args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            raise RuntimeError("injected pre-open failure")
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(fp.os, "open", fail_before_child_fd)
    with pytest.raises(RuntimeError, match="pre-open"):
        with fp._open_directory_chain(vault, ("child",), create=False):
            pass
    _assert_closed(opened)


@pytest.mark.parametrize(
    "target",
    ["", "../escape.md", " bad.md", "bad.md ", "bad\\name.md", "bad\nname.md"],
)
def test_target_components_reject_noncanonical_paths(tmp_path, target):
    vault = _vault(tmp_path)
    candidate = vault if target == "" else target
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._target_components(vault, candidate)


def test_target_components_reject_absolute_escape(tmp_path):
    vault = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("external", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._target_components(vault, outside)
    assert outside.read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("rel", [None, "", "bad\\name.md", "a//b", "bad\x7fname.md"])
def test_relative_parts_reject_invalid_paths(rel):
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._relative_parts(rel)  # type: ignore[arg-type]


def test_legacy_directory_validation_covers_creation_races_and_io_errors(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    parent = vault / "parent"
    assert fp._ensure_directory(parent, vault, create=False) is False
    assert fp._ensure_directory(parent, vault, create=True) is True
    assert parent.is_dir()

    ordinary = vault / "ordinary"
    ordinary.write_text("sentinel", encoding="utf-8")
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._ensure_directory(ordinary, vault, create=False)

    raced = vault / "raced"

    def file_race(*args, **kwargs):
        raced.write_text("external", encoding="utf-8")
        raise FileExistsError(raced)

    monkeypatch.setattr(Path, "mkdir", file_race)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._ensure_directory(raced, vault, create=True)

    directory_race = vault / "directory-race"

    def directory_wins(*args, **kwargs):
        os.mkdir(directory_race)
        raise FileExistsError(directory_race)

    monkeypatch.setattr(Path, "mkdir", directory_wins)
    assert fp._ensure_directory(directory_race, vault, create=True)

    unavailable = vault / "unavailable"
    real_lstat = Path.lstat

    def denied(path):
        if path == unavailable:
            raise PermissionError("injected denial")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._ensure_directory(unavailable, vault, create=False)


def test_absolute_validation_handles_relative_missing_and_unavailable_targets(
    tmp_path, monkeypatch
):
    vault = _vault(tmp_path)
    root, target = fp._validate_absolute_target(vault, "nested/note.md", create_parents=False)
    assert root == vault.resolve()
    assert target == root / "nested/note.md"
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._validate_absolute_target(vault, tmp_path / "outside.md", create_parents=False)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._validate_absolute_target(vault, vault, create_parents=False)

    denied = vault / "denied.md"
    real_lstat = Path.lstat

    def fail_final(path):
        if path == denied:
            raise PermissionError("injected denial")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_final)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp._validate_absolute_target(vault, denied, create_parents=False)


def test_managed_path_creates_trash_parents_and_same_device_check_fails(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = fp.managed_path(vault, "nested/note.md", namespace="trash", create_parents=True)
    assert target == vault / ".trash" / "nested" / "note.md"
    assert target.parent.is_dir()

    values = iter([type("S", (), {"st_dev": 1})(), type("S", (), {"st_dev": 2})()])
    monkeypatch.setattr(fp.os, "fstat", lambda _fd: next(values))
    with pytest.raises(fp.CrossDeviceProjection):
        fp._require_same_device(10, 11, target)


def test_stage_generation_swap_preserves_attacker_file_and_closes_fds(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    real_open = fp.os.open
    real_stat_at = fp._stat_at_signature
    opened: list[int] = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def replace_before_anchor(directory_fd, name, path, *, missing_ok=False):
        if path.parent == vault / ".tmp" and path.exists():
            attacker = path.with_suffix(".attacker")
            attacker.write_text("attacker", encoding="utf-8")
            os.replace(attacker, path)
        return real_stat_at(directory_fd, name, path, missing_ok=missing_ok)

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(fp, "_stat_at_signature", replace_before_anchor)
    with pytest.raises(fp.FileGenerationChanged):
        fp.stage_text(vault, target, "secret")
    assert [p.read_text(encoding="utf-8") for p in (vault / ".tmp").iterdir()] == ["attacker"]
    _assert_closed(opened)


def test_stage_open_and_cleanup_failures_close_fds(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    real_open = fp.os.open
    real_fstat = fp.os.fstat
    opened: list[int] = []
    temp_fds: set[int] = set()

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        if len(args) >= 2 and args[1] & os.O_CREAT:
            temp_fds.add(fd)
        return fd

    def fail_temp_fstat(fd):
        if fd in temp_fds:
            raise OSError("injected temp fstat failure")
        return real_fstat(fd)

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(fp.os, "fstat", fail_temp_fstat)
    with pytest.raises(OSError, match="temp fstat"):
        fp.stage_text(vault, target, "secret")
    unknown_generation = list((vault / ".tmp").iterdir())
    assert len(unknown_generation) == 1
    assert unknown_generation[0].read_bytes() == b""
    _assert_closed(opened)

    monkeypatch.setattr(fp.os, "fstat", real_fstat)
    real_stat = fp.os.stat
    cleanup_started = False

    def fail_cleanup_stat(*args, **kwargs):
        if cleanup_started and kwargs.get("dir_fd") is not None:
            raise PermissionError("injected cleanup race")
        return real_stat(*args, **kwargs)

    def fail_fdopen(*_args, **_kwargs):
        nonlocal cleanup_started
        cleanup_started = True
        raise OSError("fdopen")

    monkeypatch.setattr(fp.os, "stat", fail_cleanup_stat)
    monkeypatch.setattr(fp.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="fdopen"):
        fp.stage_text(vault, target, "secret")
    scratch_files = list((vault / ".tmp").iterdir())
    assert len(scratch_files) == 2
    assert set(unknown_generation) < set(scratch_files)
    assert all(path.read_bytes() == b"" for path in scratch_files)
    assert not target.exists()
    monkeypatch.setattr(fp.os, "stat", real_stat)
    _assert_closed(opened)


def test_stage_fdopen_failure_removes_owned_generation_and_closes_fds(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    real_open = fp.os.open
    opened: list[int] = []

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(fp.os, "open", track_open)
    monkeypatch.setattr(
        fp.os,
        "fdopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected fdopen failure")),
    )
    with pytest.raises(OSError, match="fdopen failure"):
        fp.stage_text(vault, target, "secret")
    assert list((vault / ".tmp").iterdir()) == []
    assert not target.exists()
    _assert_closed(opened)


@pytest.mark.parametrize("attribute", ["target", "scratch", "tmp_namespace", "trash_case"])
def test_install_rejects_invalid_staged_metadata(tmp_path, attribute):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    staged = fp.stage_text(vault, target, "canonical")
    if attribute == "target":
        install_target = vault / "other.md"
        candidate = staged
    elif attribute == "scratch":
        install_target = target
        candidate = replace(staged, path=vault / "elsewhere.tmp")
    elif attribute == "tmp_namespace":
        install_target = vault / ".tmp" / "note.md"
        candidate = replace(staged, target=install_target)
    else:
        install_target = vault / ".Trash" / "note.md"
        candidate = replace(staged, target=install_target)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.install_staged(candidate, install_target)
    assert staged.path.read_text(encoding="utf-8") == "canonical"


def test_install_rejects_cross_device_and_post_install_generation_change(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    staged = fp.stage_text(vault, target, "canonical")
    monkeypatch.setattr(
        fp,
        "_require_same_device",
        lambda *_args: (_ for _ in ()).throw(fp.CrossDeviceProjection("injected")),
    )
    with pytest.raises(fp.CrossDeviceProjection):
        fp.install_staged(staged, target)
    assert not target.exists()
    assert staged.path.read_text(encoding="utf-8") == "canonical"

    monkeypatch.undo()
    real_stat_at = fp._stat_at_signature
    calls = 0

    def changed_after_install(directory_fd, name, path, *, missing_ok=False):
        nonlocal calls
        result = real_stat_at(directory_fd, name, path, missing_ok=missing_ok)
        if path == target and result is not None:
            calls += 1
            if calls == 3:
                return replace(result, ctime_ns=result.ctime_ns + 1)
        return result

    monkeypatch.setattr(fp, "_stat_at_signature", changed_after_install)
    with pytest.raises(fp.FileGenerationChanged, match="changed after installation"):
        fp.install_staged(staged, target)
    assert target.read_text(encoding="utf-8") == "canonical"


def test_install_rejects_generation_changed_before_post_open(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    staged = fp.stage_text(vault, target, "canonical")
    real_stat_at = fp._stat_at_signature
    installed_checks = 0

    def changed_installed_target(directory_fd, name, path, *, missing_ok=False):
        nonlocal installed_checks
        result = real_stat_at(directory_fd, name, path, missing_ok=missing_ok)
        if path == target and not missing_ok and result is not None:
            installed_checks += 1
            if installed_checks == 1:
                return replace(result, ctime_ns=result.ctime_ns + 1)
        return result

    monkeypatch.setattr(fp, "_stat_at_signature", changed_installed_target)
    with pytest.raises(fp.FileGenerationChanged, match="before verification"):
        fp.install_staged(staged, target)
    assert target.read_text(encoding="utf-8") == "canonical"
    assert not staged.path.exists()


def test_cleanup_staged_rejects_wrong_location_missing_scratch_and_symlink(tmp_path):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    staged = fp.stage_text(vault, target, "canonical")
    assert not fp.cleanup_staged(replace(staged, path=vault / "wrong.tmp"))
    os.replace(vault / ".tmp", vault / "moved-tmp")
    assert not fp.cleanup_staged(staged)
    (vault / ".tmp").symlink_to(tmp_path, target_is_directory=True)
    assert not fp.cleanup_staged(staged)
    assert (vault / "moved-tmp" / staged.path.name).read_text(encoding="utf-8") == "canonical"


def test_confined_unlink_checks_lexical_path_generation_and_absence(tmp_path):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    target.write_text("current", encoding="utf-8")
    signature = fp.file_signature(target)
    with pytest.raises(fp.UnsafeProjectionPath):
        fp.unlink_regular("note.md", vault=vault)
    assert target.read_text(encoding="utf-8") == "current"
    assert not fp.unlink_regular(
        target, expected=replace(signature, ctime_ns=signature.ctime_ns + 1), vault=vault
    )
    assert fp.unlink_regular(target, expected=signature, vault=vault)
    assert not fp.unlink_regular(target, vault=vault)


def test_stable_read_reports_platform_generation_read_and_post_read_failures(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    target.write_text("body", encoding="utf-8")

    monkeypatch.delattr(fp.os, "O_NOFOLLOW")
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_unreadable"
    monkeypatch.undo()

    real_stat_at = fp._stat_at_signature
    calls = 0

    def changed_while_opening(directory_fd, name, path, *, missing_ok=False):
        nonlocal calls
        result = real_stat_at(directory_fd, name, path, missing_ok=missing_ok)
        if not missing_ok:
            calls += 1
        if calls == 1 and not missing_ok and result is not None:
            return replace(result, ctime_ns=result.ctime_ns + 1)
        return result

    monkeypatch.setattr(fp, "_stat_at_signature", changed_while_opening)
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_changed"

    monkeypatch.undo()
    real_read = fp.os.read
    interrupted = False

    def interrupt_once(fd, size):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise InterruptedError
        return real_read(fd, size)

    monkeypatch.setattr(fp.os, "read", interrupt_once)
    stable = fp.read_stable_markdown(vault, target)
    assert interrupted and stable.text == "body"


def test_stable_read_rejects_invalid_utf8_bytes(tmp_path):
    vault = _vault(tmp_path)
    target = vault / "invalid.md"
    target.write_bytes(b"before\xffafter")

    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "invalid_encoding"
    assert target.read_bytes() == b"before\xffafter"


def test_stable_read_detects_size_and_post_read_generation_changes(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    target.write_text("body", encoding="utf-8")
    real_read = fp.os.read
    shortened = False

    def short_read(fd, size):
        nonlocal shortened
        chunk = real_read(fd, size)
        if chunk and not shortened:
            shortened = True
            return chunk[:-1]
        return chunk

    monkeypatch.setattr(fp.os, "read", short_read)
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_changed"


def test_stable_read_reports_post_close_disappearance_and_generation_change(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    target.write_text("body", encoding="utf-8")
    real_open_parent = fp._open_target_parent
    entries = 0

    def disappear_on_post_open(*args, **kwargs):
        nonlocal entries
        manager = real_open_parent(*args, **kwargs)

        class Wrapper:
            def __enter__(self):
                nonlocal entries
                entries += 1
                if entries == 2:
                    raise fp.ProjectionPathMissing("injected post-close disappearance")
                return manager.__enter__()

            def __exit__(self, *exc):
                return manager.__exit__(*exc)

        return Wrapper()

    monkeypatch.setattr(fp, "_open_target_parent", disappear_on_post_open)
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_disappeared"

    monkeypatch.undo()
    real_stat_at = fp._stat_at_signature
    generation_checks = 0

    def change_post_signature(directory_fd, name, path, *, missing_ok=False):
        nonlocal generation_checks
        result = real_stat_at(directory_fd, name, path, missing_ok=missing_ok)
        if path == target and not missing_ok and result is not None:
            generation_checks += 1
            if generation_checks == 3:
                return replace(result, ctime_ns=result.ctime_ns + 1)
        return result

    monkeypatch.setattr(fp, "_stat_at_signature", change_post_signature)
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_changed"
    assert target.read_text(encoding="utf-8") == "body"

    monkeypatch.undo()
    real_open_parent = fp._open_target_parent
    entries = 0

    def fail_post_open(*args, **kwargs):
        nonlocal entries
        manager = real_open_parent(*args, **kwargs)

        class Wrapper:
            def __enter__(self):
                nonlocal entries
                entries += 1
                if entries == 2:
                    raise fp.UnsafeProjectionPath("injected post-read swap")
                return manager.__enter__()

            def __exit__(self, *exc):
                return manager.__exit__(*exc)

        return Wrapper()

    monkeypatch.setattr(fp, "_open_target_parent", fail_post_open)
    with pytest.raises(fp.StableFileError) as raised:
        fp.read_stable_markdown(vault, target)
    assert raised.value.reason == "file_changed"


def test_stable_current_returns_false_for_missing_and_unsafe_paths(tmp_path):
    vault = _vault(tmp_path)
    target = vault / "note.md"
    target.write_text("body", encoding="utf-8")
    stable = fp.read_stable_markdown(vault, target)
    target.unlink()
    assert not fp.stable_markdown_is_current(stable)
    (vault / "note.md").symlink_to(tmp_path / "outside.md")
    assert not fp.stable_markdown_is_current(stable)
