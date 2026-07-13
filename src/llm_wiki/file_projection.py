"""Crash-safe filesystem primitives for projecting canonical document revisions.

The helpers in this module deliberately use lexical paths and ``lstat``.  Resolving a
child path before mutating it would follow a symlink out of the vault, while ordinary
``Path.write_text``/``replace`` helpers do not give callers a generation token they can
revalidate after staging.  Higher-level DB fencing lives in ``DocumentService``; this
module owns only filesystem confinement, generation checks, and durability barriers.
"""
from __future__ import annotations

import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class FileProjectionError(RuntimeError):
    """Base class for a projection operation that cannot be performed safely."""


class UnsafeProjectionPath(FileProjectionError):
    """A lexical path component is unsafe, a symlink, or the wrong file type."""


class FileGenerationChanged(FileProjectionError):
    """A file no longer has the exact generation the caller previously observed."""


class CrossDeviceProjection(FileProjectionError):
    """The scratch directory and target are on different filesystems."""


class ProjectionPathMissing(FileProjectionError):
    """A lexical parent or final entry disappeared without becoming unsafe."""


class StableFileError(FileProjectionError):
    """A markdown file could not be adopted as one stable external generation."""

    def __init__(self, reason: str, detail: str | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FileSignature:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileSignature:
        return cls(
            dev=int(value.st_dev),
            ino=int(value.st_ino),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )


@dataclass(frozen=True)
class StagedText:
    vault: Path
    target: Path
    path: Path
    signature: FileSignature


@dataclass(frozen=True)
class StableMarkdown:
    vault: Path
    path: Path
    relative_path: str
    signature: FileSignature
    text: str


@dataclass(frozen=True)
class ProjectionResult:
    """Result shape shared by the higher-level document projector and recovery."""

    doc_id: int
    path: str | None
    settled: bool
    transitioned: bool
    reason: str | None = None
    attempts: int = 0
    is_deleted: bool | None = None
    detail: str | None = None
    current_installed: bool = False


def _regular_signature(value: os.stat_result, path: Path) -> FileSignature:
    if not stat.S_ISREG(value.st_mode):
        raise UnsafeProjectionPath(f"projection path is not a regular file: {path}")
    return FileSignature.from_stat(value)


def file_signature(path: Path | str, *, missing_ok: bool = False) -> FileSignature | None:
    """Return an ``lstat`` generation token for a regular file.

    ``missing_ok`` accepts only a genuinely absent final entry.  Permission errors,
    non-directory parents, symlinks, directories, and special files remain errors.
    """
    target = Path(path)
    try:
        value = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    return _regular_signature(value, target)


def confined_file_signature(
    vault: Path | str, path: Path | str, *, missing_ok: bool = False
) -> FileSignature | None:
    """Return a regular-file signature through a vault-anchored directory chain.

    Unlike a plain lexical ``lstat``, the directory descriptors used here cannot
    follow a parent symlink that is swapped in between validation and lookup.
    """
    target = Path(path)
    try:
        with _open_target_parent(vault, target, create=False) as (
            _root,
            confined,
            parent_fd,
            name,
            _parent,
        ):
            return _stat_at_signature(
                parent_fd, name, confined, missing_ok=missing_ok
            )
    except ProjectionPathMissing:
        if missing_ok:
            return None
        raise FileNotFoundError(target) from None


def confirm_confined_absence(vault: Path | str, path: Path | str) -> bool:
    """Confirm an absent target and durably fence its existing parent directory."""
    try:
        with _open_target_parent(vault, path, create=False) as (
            _root,
            confined,
            parent_fd,
            name,
            _parent,
        ):
            if _stat_at_signature(parent_fd, name, confined, missing_ok=True) is not None:
                return False
            _fsync_directory_fd(parent_fd)
            return True
    except ProjectionPathMissing:
        # A missing lexical ancestor also proves the final entry absent. Our file
        # primitives never remove parent directories, so there is no unflushed
        # target unlink for this helper to persist at that level.
        return True


def fsync_directory(path: Path | str) -> None:
    """Persist directory-entry changes without following a directory symlink."""
    directory = Path(path)
    try:
        value = directory.lstat()
    except OSError as exc:
        raise UnsafeProjectionPath(f"projection directory is unavailable: {directory}") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeProjectionPath(f"projection directory is not a real directory: {directory}")
    expected_identity = (int(value.st_dev), int(value.st_ino))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise UnsafeProjectionPath(
                f"projection directory changed while opening it: {directory}"
            )
        if (int(opened.st_dev), int(opened.st_ino)) != expected_identity:
            raise FileGenerationChanged(
                f"projection directory generation changed while opening it: {directory}"
            )
        os.fsync(fd)
    finally:
        os.close(fd)


def _vault_root(vault: Path | str) -> Path:
    try:
        root = Path(vault).resolve(strict=True)
        value = root.lstat()
    except OSError as exc:
        raise UnsafeProjectionPath(f"vault root is unavailable: {vault}") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeProjectionPath(f"vault root is not a directory: {root}")
    return root


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)


def _fsync_directory_fd(fd: int) -> None:
    value = os.fstat(fd)
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeProjectionPath("projection directory descriptor is not a directory")
    os.fsync(fd)


def _open_root_fd(root: Path) -> int:
    before = root.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise UnsafeProjectionPath(f"vault root is not a real directory: {root}")
    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (int(opened.st_dev), int(opened.st_ino))
            != (int(before.st_dev), int(before.st_ino))
        ):
            raise FileGenerationChanged(f"vault root changed while opening it: {root}")
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _open_directory_chain(
    root: Path, parts: tuple[str, ...], *, create: bool
) -> Iterator[tuple[int, Path]]:
    """Open a directory chain from an anchored vault fd without following symlinks."""
    current_fd = _open_root_fd(root)
    current_path = root
    try:
        for part in parts:
            next_fd = -1
            try:
                try:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise ProjectionPathMissing(
                            f"projection parent is missing: {current_path / part}"
                        ) from None
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    _fsync_directory_fd(next_fd)
                    _fsync_directory_fd(current_fd)
                opened = os.fstat(next_fd)
                if not stat.S_ISDIR(opened.st_mode):
                    raise UnsafeProjectionPath(
                        f"projection parent is not a directory: {current_path / part}"
                    )
            except ProjectionPathMissing:
                if next_fd >= 0:
                    os.close(next_fd)
                raise
            except OSError as exc:
                if next_fd >= 0:
                    os.close(next_fd)
                raise UnsafeProjectionPath(
                    f"projection parent is unsafe: {current_path / part}"
                ) from exc
            except Exception:
                if next_fd >= 0:
                    os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            current_path = current_path / part
        yield current_fd, current_path
    finally:
        os.close(current_fd)


def _target_components(vault: Path | str, target: Path | str) -> tuple[Path, Path, tuple[str, ...]]:
    root = _vault_root(vault)
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeProjectionPath("projection target escapes the vault") from exc
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafeProjectionPath("projection target contains an unsafe component")
    relative_text = relative.as_posix()
    if relative_text != relative_text.strip() or any(
        "\\" in part
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in part)
        for part in parts
    ):
        raise UnsafeProjectionPath("projection target contains a non-canonical component")
    return root, candidate, parts


def _stat_at_signature(
    directory_fd: int, name: str, path: Path, *, missing_ok: bool = False
) -> FileSignature | None:
    try:
        value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    return _regular_signature(value, path)


@contextmanager
def _open_target_parent(
    vault: Path | str, target: Path | str, *, create: bool
) -> Iterator[tuple[Path, Path, int, str, Path]]:
    root, candidate, parts = _target_components(vault, target)
    with _open_directory_chain(root, parts[:-1], create=create) as (
        parent_fd,
        parent_path,
    ):
        _stat_at_signature(parent_fd, parts[-1], candidate, missing_ok=True)
        yield root, candidate, parent_fd, parts[-1], parent_path


def read_confined_bytes(
    vault: Path | str,
    relative_path: str,
    *,
    max_bytes: int | None = None,
) -> tuple[Path, bytes]:
    """Read one regular file through a vault-anchored, no-follow descriptor chain."""
    root, target, parts = _target_components(vault, relative_path)
    with _open_directory_chain(root, parts[:-1], create=False) as (parent_fd, _parent):
        before = _stat_at_signature(parent_fd, parts[-1], target)
        if before is None:  # missing_ok=False above; narrows the helper's optional type
            raise FileNotFoundError(target)
        if max_bytes is not None and before.size > max_bytes:
            raise UnsafeProjectionPath(f"confined file exceeds size limit: {target}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        try:
            opened = _regular_signature(os.fstat(fd), target)
            if opened != before:
                raise FileGenerationChanged(f"file changed while opening: {target}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise UnsafeProjectionPath(f"confined file exceeds size limit: {target}")
                chunks.append(chunk)
            after = _regular_signature(os.fstat(fd), target)
        finally:
            os.close(fd)
        anchored_after = _stat_at_signature(parent_fd, parts[-1], target)
    data = b"".join(chunks)
    if after != opened or anchored_after != opened or len(data) != opened.size:
        raise FileGenerationChanged(f"file changed while reading: {target}")
    return target, data


def write_confined_bytes(vault: Path | str, relative_path: str, data: bytes) -> Path:
    """Create a regular file atomically below ``vault`` without following symlinks.

    Existing regular files are retained (callers use content-addressed names).  An
    existing symlink, directory, or special file is always rejected.
    """
    root, target, parts = _target_components(vault, relative_path)
    with _open_directory_chain(root, parts[:-1], create=True) as (parent_fd, _parent):
        current = _stat_at_signature(parent_fd, parts[-1], target, missing_ok=True)
        if current is not None:
            return target
        temp_name = f".{parts[-1]}.{uuid.uuid4().hex[:16]}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                try:
                    written = os.write(fd, view[offset:])
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError("short write while storing confined file")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            try:
                os.link(
                    temp_name,
                    parts[-1],
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _stat_at_signature(parent_fd, parts[-1], target)
            _fsync_directory_fd(parent_fd)
        finally:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    return target


def list_confined_names(vault: Path | str, relative_directory: str) -> tuple[str, ...]:
    """List a real directory below ``vault`` without following any parent symlink."""
    root = _vault_root(vault)
    parts = _relative_parts(relative_directory)
    with _open_directory_chain(root, parts, create=False) as (directory_fd, _path):
        return tuple(os.listdir(directory_fd))


def _relative_parts(rel: str) -> tuple[str, ...]:
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        raise UnsafeProjectionPath("projection path is empty or invalid")
    if rel.startswith(("/", "\\")) or "\\" in rel:
        raise UnsafeProjectionPath("projection path must be vault-relative POSIX")
    parts = tuple(rel.split("/"))
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafeProjectionPath("projection path contains an unsafe component")
    if any(
        any(ord(char) < 0x20 or ord(char) == 0x7F for char in part)
        for part in parts
    ):
        raise UnsafeProjectionPath("projection path contains a control character")
    return parts


def _ensure_directory(path: Path, parent: Path, *, create: bool) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode):
                raise UnsafeProjectionPath(
                    f"projection parent is not a real directory: {path}"
                ) from None
        else:
            # Persist both the new directory itself and its entry in the parent.
            fsync_directory(path)
            fsync_directory(parent)
            return True
    except OSError as exc:
        raise UnsafeProjectionPath(f"projection parent is unavailable: {path}") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeProjectionPath(f"projection parent is not a real directory: {path}")
    return True


def _validate_absolute_target(
    vault: Path | str, target: Path | str, *, create_parents: bool
) -> tuple[Path, Path]:
    root = _vault_root(vault)
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeProjectionPath("projection target escapes the vault") from exc
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafeProjectionPath("projection target contains an unsafe component")

    parent = root
    parents_exist = True
    for part in parts[:-1]:
        next_parent = parent / part
        if parents_exist:
            parents_exist = _ensure_directory(
                next_parent, parent, create=create_parents
            )
        parent = next_parent

    if parents_exist:
        final = parent / parts[-1]
        try:
            value = final.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise UnsafeProjectionPath(f"projection target is unavailable: {final}") from exc
        else:
            if not stat.S_ISREG(value.st_mode):
                raise UnsafeProjectionPath(
                    f"projection target is not a regular file: {final}"
                )
    return root, candidate


def managed_path(
    vault: Path | str,
    rel: str,
    *,
    namespace: str = "live",
    create_parents: bool = False,
) -> Path:
    """Build a confined lexical live/trash path without resolving a child symlink."""
    root = _vault_root(vault)
    parts = _relative_parts(rel)
    if namespace == "live":
        if parts[0].casefold() in (".tmp", ".trash"):
            raise UnsafeProjectionPath(
                f"live documents cannot use the internal namespace: {parts[0]}"
            )
        target = root.joinpath(*parts)
    elif namespace == "trash":
        target = root.joinpath(".trash", *parts)
    else:
        raise ValueError(f"unknown projection namespace: {namespace}")
    if create_parents:
        with _open_target_parent(root, target, create=True):
            pass
    else:
        _validate_absolute_target(root, target, create_parents=False)
    return target


def _require_same_device(scratch_fd: int, target_fd: int, target: Path) -> None:
    if int(os.fstat(scratch_fd).st_dev) != int(os.fstat(target_fd).st_dev):
        raise CrossDeviceProjection(
            f"scratch and projection target are on different filesystems: {target}"
        )


def stage_text(vault: Path | str, target: Path | str, body: str) -> StagedText:
    """Write and fsync canonical UTF-8 text in the vault scratch directory."""
    root, target_path, target_parts = _target_components(vault, target)
    if target_parts[0].casefold() == ".tmp":
        raise UnsafeProjectionPath("a projection target cannot use the scratch namespace")
    if target_parts[0].casefold() == ".trash" and target_parts[0] != ".trash":
        raise UnsafeProjectionPath("trash namespace spelling must be canonical")
    scratch = root / ".tmp"
    with _open_target_parent(root, target_path, create=True) as (
        _root,
        _target,
        target_parent_fd,
        _target_name,
        _target_parent,
    ), _open_directory_chain(root, (".tmp",), create=True) as (
        scratch_fd,
        _scratch_path,
    ):
        _require_same_device(scratch_fd, target_parent_fd, target_path)
        temp_name = f"{uuid.uuid4().hex}.tmp"
        temp = scratch / temp_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_name, flags, 0o600, dir_fd=scratch_fd)
        opened_identity: tuple[int, int] | None = None
        try:
            opened = os.fstat(fd)
            opened_identity = (int(opened.st_dev), int(opened.st_ino))
            stream = os.fdopen(fd, "wb", closefd=True)
            fd = -1  # stream owns the descriptor from here
            with stream:
                stream.write((body or "").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
                signature = _regular_signature(os.fstat(stream.fileno()), temp)
                anchored = _stat_at_signature(scratch_fd, temp_name, temp)
                if anchored != signature:
                    raise FileGenerationChanged(
                        f"staged file generation changed while open: {temp}"
                    )
            _fsync_directory_fd(scratch_fd)
            return StagedText(root, target_path, temp, signature)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                current = os.stat(temp_name, dir_fd=scratch_fd, follow_symlinks=False)
                if opened_identity is not None and (
                    int(current.st_dev),
                    int(current.st_ino),
                ) == opened_identity:
                    os.unlink(temp_name, dir_fd=scratch_fd)
                    _fsync_directory_fd(scratch_fd)
            except OSError:
                pass
            raise


def install_staged(staged: StagedText, target: Path | str) -> FileSignature:
    """Revalidate and atomically install a staged generation, then fsync its parent."""
    root, target_path, target_parts = _target_components(staged.vault, target)
    if target_parts[0].casefold() == ".tmp":
        raise UnsafeProjectionPath("a projection target cannot use the scratch namespace")
    if target_parts[0].casefold() == ".trash" and target_parts[0] != ".trash":
        raise UnsafeProjectionPath("trash namespace spelling must be canonical")
    if target_path != staged.target:
        raise UnsafeProjectionPath("staged text belongs to a different target")
    scratch = root / ".tmp"
    if staged.path.parent != scratch:
        raise UnsafeProjectionPath("staged text is not in the vault scratch directory")
    with _open_target_parent(root, target_path, create=False) as (
        _root,
        _target,
        target_parent_fd,
        target_name,
        _target_parent,
    ), _open_directory_chain(root, (".tmp",), create=False) as (
        scratch_fd,
        _scratch_path,
    ):
        _require_same_device(scratch_fd, target_parent_fd, target_path)
        temp_name = staged.path.name
        temp_fd = os.open(
            temp_name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=scratch_fd,
        )
        try:
            before = _regular_signature(os.fstat(temp_fd), staged.path)
            if before != staged.signature:
                raise FileGenerationChanged(
                    f"staged file generation changed: {staged.path}"
                )
            lexical = file_signature(staged.path)
            anchored = _stat_at_signature(scratch_fd, temp_name, staged.path)
            after = _regular_signature(os.fstat(temp_fd), staged.path)
            if lexical != staged.signature or anchored != staged.signature or after != before:
                raise FileGenerationChanged(
                    f"staged file generation changed during verification: {staged.path}"
                )

            os.replace(
                temp_name,
                target_name,
                src_dir_fd=scratch_fd,
                dst_dir_fd=target_parent_fd,
            )
            _fsync_directory_fd(target_parent_fd)
            _fsync_directory_fd(scratch_fd)
            installed_fd = _regular_signature(os.fstat(temp_fd), target_path)
            installed_target = _stat_at_signature(
                target_parent_fd, target_name, target_path
            )
            if installed_target != installed_fd:
                raise FileGenerationChanged(
                    f"installed file generation changed before verification: {target_path}"
                )
        finally:
            os.close(temp_fd)

    # Re-open the lexical chain after publication.  The target must still name the
    # exact installed generation; a parent rename/replacement is not a successful
    # projection even though the anchored rename itself stayed confined.
    try:
        with _open_target_parent(root, target_path, create=False) as (
            _root,
            _target,
            post_parent_fd,
            post_name,
            _post_parent,
        ):
            post_target = _stat_at_signature(
                post_parent_fd, post_name, target_path
            )
    except ProjectionPathMissing as exc:
        raise FileGenerationChanged(
            f"projection target disappeared after installation: {target_path}"
        ) from exc
    if post_target != installed_target:
        raise FileGenerationChanged(
            f"projection target changed after installation: {target_path}"
        )
    return installed_target


def cleanup_staged(staged: StagedText) -> bool:
    """Remove only the exact temp generation created by ``stage_text``."""
    try:
        root = _vault_root(staged.vault)
        scratch = root / ".tmp"
        if staged.path.parent != scratch:
            return False
        with _open_directory_chain(root, (".tmp",), create=False) as (
            scratch_fd,
            _scratch_path,
        ):
            current = _stat_at_signature(
                scratch_fd, staged.path.name, staged.path, missing_ok=True
            )
            if current is None or current != staged.signature:
                return False
            os.unlink(staged.path.name, dir_fd=scratch_fd)
            _fsync_directory_fd(scratch_fd)
            return True
    except (FileNotFoundError, ProjectionPathMissing, UnsafeProjectionPath):
        return False


def unlink_regular(
    path: Path | str,
    *,
    expected: FileSignature | None = None,
    vault: Path | str | None = None,
) -> bool:
    """Unlink a regular file only if its current generation is the expected one."""
    target = Path(path)
    if vault is not None:
        try:
            with _open_target_parent(vault, target, create=False) as (
                _root,
                confined,
                parent_fd,
                name,
                _parent,
            ):
                if confined != target:
                    raise UnsafeProjectionPath(
                        "unlink target is not the confined lexical path"
                    )
                current = _stat_at_signature(
                    parent_fd, name, target, missing_ok=True
                )
                if current is None or (expected is not None and current != expected):
                    return False
                os.unlink(name, dir_fd=parent_fd)
                _fsync_directory_fd(parent_fd)
                return True
        except ProjectionPathMissing:
            return False
    current = file_signature(target, missing_ok=True)
    if current is None or (expected is not None and current != expected):
        return False
    target.unlink()
    fsync_directory(target.parent)
    return True


def read_stable_markdown(vault: Path | str, path: Path | str) -> StableMarkdown:
    """Read one no-follow regular-file generation and verify it before and after."""
    try:
        root, target, parts = _target_components(vault, path)
        with _open_target_parent(root, target, create=False) as (
            _root,
            confined,
            parent_fd,
            name,
            _parent,
        ):
            anchored_before = _stat_at_signature(parent_fd, name, confined)
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise StableFileError(
                    "file_unreadable",
                    "this platform cannot open external files without following symlinks",
                )
            fd = os.open(
                name,
                os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
            try:
                opened_before = _regular_signature(os.fstat(fd), confined)
                if opened_before != anchored_before:
                    raise StableFileError("file_changed", f"file changed while opening: {confined}")
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = os.read(fd, 1024 * 1024)
                    except InterruptedError:
                        continue
                    if not chunk:
                        break
                    chunks.append(chunk)
                opened_after = _regular_signature(os.fstat(fd), confined)
                anchored_after = _stat_at_signature(parent_fd, name, confined)
            finally:
                os.close(fd)
        data = b"".join(chunks)
        if (
            opened_after != opened_before
            or anchored_after != opened_before
            or len(data) != opened_before.size
        ):
            raise StableFileError("file_changed", f"file changed while reading: {target}")
    except StableFileError:
        raise
    except (FileNotFoundError, ProjectionPathMissing) as exc:
        raise StableFileError("file_disappeared", str(exc)) from exc
    except (OSError, FileProjectionError) as exc:
        raise StableFileError("file_unreadable", str(exc)) from exc

    # Re-open the lexical path after closing the original descriptors. A directory
    # rename/symlink swap must not turn an anchored read into an adopted alias.
    try:
        with _open_target_parent(root, target, create=False) as (
            _root,
            post_target,
            post_parent_fd,
            post_name,
            _post_parent,
        ):
            post = _stat_at_signature(post_parent_fd, post_name, post_target)
    except (FileNotFoundError, ProjectionPathMissing) as exc:
        raise StableFileError("file_disappeared", str(exc)) from exc
    except (OSError, FileProjectionError) as exc:
        raise StableFileError("file_changed", str(exc)) from exc
    if post != opened_before:
        raise StableFileError("file_changed", f"file changed after reading: {target}")

    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise StableFileError(
            "invalid_encoding",
            f"markdown must be valid UTF-8: {target}",
        ) from exc

    return StableMarkdown(root, target, "/".join(parts), opened_before, text)


def stable_markdown_is_current(stable: StableMarkdown) -> bool:
    """Whether the same lexical path still names the adopted file generation."""
    try:
        return (
            confined_file_signature(stable.vault, stable.path, missing_ok=True) == stable.signature
        )
    except (OSError, FileProjectionError):
        return False
