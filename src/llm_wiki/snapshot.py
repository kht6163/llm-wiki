"""Generation-consistent database and vault snapshots."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from filelock import FileLock

from .db import SCHEMA_VERSION, Database, get_meta
from .process_lock import ProjectLock
from .util import now_iso

SNAPSHOT_FORMAT = "llm-wiki-snapshot"
_ATTACHMENT_ATTEMPTS = 3
_COPY_CHUNK_SIZE = 1024 * 1024
MAX_SNAPSHOT_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
MAX_SNAPSHOT_MEMBERS = 100_000
MAX_SNAPSHOT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_MEMBER_BYTES = 8 * 1024 * 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
_RESTORE_JOURNAL_FORMAT = "llm-wiki-restore-journal"
_RESTORE_JOURNAL_VERSION = 2


@dataclass(frozen=True)
class SnapshotReport:
    schema_version: int | None
    doc_count: int
    file_count: int
    embedding_model: str | None


@dataclass
class RestoreReport:
    schema_version: int | None
    doc_count: int
    embedding_model: str | None = None
    backup_cleanup_warnings: tuple[Path, ...] = ()
    _journal: _RestoreJournal | None = None

    def finalize(self) -> tuple[Path, ...]:
        """Commit a pending restore by removing the retained original targets."""
        if self._journal is not None:
            self.backup_cleanup_warnings = self._journal.finalize()
            self._journal = None
        return self.backup_cleanup_warnings

    def rollback(self, cause: BaseException) -> None:
        """Restore every original target, then re-raise ``cause``."""
        if self._journal is None:
            raise RuntimeError("restore is no longer pending")
        journal, self._journal = self._journal, None
        journal.rollback(cause)


class RestoreRollbackError(ValueError):
    """Publication failed and one or more original targets could not be restored."""

    def __init__(
        self,
        publish_error: BaseException,
        rollback_errors: Sequence[BaseException],
        backup_paths: tuple[Path, ...],
    ) -> None:
        self.publish_error = publish_error
        self.rollback_errors = tuple(rollback_errors)
        self.backup_paths = backup_paths
        self.staging_cleanup_errors: tuple[OSError, ...] = ()
        locations = ", ".join(str(path) for path in backup_paths) or "unknown locations"
        super().__init__(
            "restore publication failed and rollback could not complete; "
            f"backups preserved at {locations}"
        )


class RestorePreparationError(ValueError):
    """Restore validation failed and one or more staging paths could not be removed."""

    def __init__(
        self,
        primary_error: BaseException,
        cleanup_errors: list[OSError],
        staging_paths: tuple[Path, ...],
    ) -> None:
        self.primary_error = primary_error
        self.cleanup_errors = tuple(cleanup_errors)
        self.staging_paths = staging_paths
        locations = ", ".join(str(path) for path in staging_paths) or "unknown locations"
        super().__init__(
            f"{primary_error}; staging cleanup also failed; remnants preserved at {locations}"
        )


class SnapshotArchiveReadError(ValueError):
    """Archive processing failed and descriptor finalization also failed."""

    def __init__(
        self, primary_error: BaseException, cleanup_errors: list[BaseException]
    ) -> None:
        self.primary_error = primary_error
        self.cleanup_errors = tuple(cleanup_errors)
        super().__init__(
            f"{primary_error}; snapshot archive descriptor cleanup also failed: "
            f"{cleanup_errors[0]}"
        )


@dataclass(frozen=True)
class _ArchiveFile:
    path: str
    kind: str
    source: Path
    size: int
    sha256: str

    @property
    def manifest_entry(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
        }


def _normalized_archive_path(rel: str) -> tuple[str, str]:
    """Return a safe POSIX archive path and its case-insensitive identity."""
    if "\\" in rel or any(ord(char) < 0x20 or ord(char) == 0x7F for char in rel):
        raise ValueError(f"unsafe vault path: {rel!r}")
    path = PurePosixPath(rel)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe vault path: {rel!r}")
    archive_path = (PurePosixPath("vault") / path).as_posix()
    identity = unicodedata.normalize("NFC", archive_path).casefold()
    return archive_path, identity


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _stage_attachment_once(path: Path, root: Path, staged: Path) -> tuple[int, str] | None:
    """Stage one stable regular-file generation without following a final symlink."""
    fd = -1
    try:
        visible = os.lstat(path)
        if not stat.S_ISREG(visible.st_mode):
            return None
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        before = os.fstat(fd)
        resolved = path.resolve()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(visible.st_mode)
            or (before.st_dev, before.st_ino) != (visible.st_dev, visible.st_ino)
            or resolved == root
            or root not in resolved.parents
        ):
            return None

        digest = hashlib.sha256()
        size = 0
        with staged.open("wb") as target:
            while chunk := os.read(fd, _COPY_CHUNK_SIZE):
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        after = os.fstat(fd)
        final_visible = os.lstat(path)
        if (
            _stat_identity(before) != _stat_identity(after)
            or size != before.st_size
            or (before.st_dev, before.st_ino)
            != (final_visible.st_dev, final_visible.st_ino)
        ):
            return None
        return size, digest.hexdigest()
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _stage_stable_attachment(path: Path, root: Path, staged: Path) -> tuple[int, str]:
    for _ in range(_ATTACHMENT_ATTEMPTS):
        staged.unlink(missing_ok=True)
        metadata = _stage_attachment_once(path, root, staged)
        if metadata is not None:
            return metadata
    staged.unlink(missing_ok=True)
    raise RuntimeError(f"attachment changed while snapshotting: {path.name}")


def _add_bytes(tar: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _add_staged_file(tar: tarfile.TarFile, file: _ArchiveFile) -> None:
    info = tarfile.TarInfo(file.path)
    info.size = file.size
    with file.source.open("rb") as source:
        tar.addfile(info, source)


def _file_metadata(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _verify_archive(path: Path, files: list[_ArchiveFile]) -> None:
    """Verify the bytes actually persisted by tar before publishing the archive."""
    with tarfile.open(path, "r") as tar:
        for file in files:
            archived = tar.extractfile(file.path)
            if archived is None:
                raise RuntimeError(f"snapshot file verification failed: {file.path}")
            file_digest = hashlib.sha256()
            size = 0
            while chunk := archived.read(_COPY_CHUNK_SIZE):
                file_digest.update(chunk)
                size += len(chunk)
            if size != file.size or file_digest.hexdigest() != file.sha256:
                raise RuntimeError(f"snapshot file verification failed: {file.path}")


def _stage_managed_files(
    conn: sqlite3.Connection, staging: Path
) -> tuple[list[_ArchiveFile], set[str]]:
    rows = conn.execute(
        "SELECT d.path, r.body FROM documents d "
        "JOIN revisions r ON r.doc_id=d.id AND r.version=d.version "
        "WHERE d.is_deleted=0 ORDER BY d.path_norm"
    )
    expected = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
    ).fetchone()[0]

    files: list[_ArchiveFile] = []
    normalized: set[str] = set()
    for index, row in enumerate(rows):
        archive_path, identity = _normalized_archive_path(str(row["path"]))
        if identity in normalized:
            raise ValueError(f"duplicate normalized snapshot path: {archive_path}")
        normalized.add(identity)
        source = staging / f"managed-{index}"
        digest = hashlib.sha256()
        size = 0
        body = str(row["body"])
        with source.open("wb") as target:
            for offset in range(0, len(body), _COPY_CHUNK_SIZE // 4):
                chunk = body[offset:offset + _COPY_CHUNK_SIZE // 4].encode("utf-8")
                target.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        files.append(_ArchiveFile(archive_path, "managed", source, size, digest.hexdigest()))
    if len(files) != expected:
        raise RuntimeError("active document has no revision at its current version")
    return files, normalized


def _stage_attachment_files(
    vault: Path, normalized: set[str], staging: Path
) -> list[_ArchiveFile]:
    if not vault.exists():
        return []
    root = vault.resolve()
    files: list[_ArchiveFile] = []
    for path in sorted(vault.rglob("*"), key=lambda item: item.as_posix()):
        rel_path = path.relative_to(vault)
        if rel_path.parts and rel_path.parts[0] == ".tmp":
            continue
        visible = os.lstat(path)
        if stat.S_ISDIR(visible.st_mode):
            continue
        if stat.S_ISLNK(visible.st_mode):
            raise ValueError(f"unsafe vault path: {rel_path.as_posix()!r}")
        if not stat.S_ISREG(visible.st_mode):
            raise ValueError(
                f"vault entry must be a regular file: {rel_path.as_posix()!r}"
            )
        if path.suffix.lower() == ".md":
            continue
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError(f"unsafe vault path: {rel_path.as_posix()!r}")
        archive_path, identity = _normalized_archive_path(rel_path.as_posix())
        if identity in normalized:
            raise ValueError(f"duplicate normalized snapshot path: {archive_path}")
        normalized.add(identity)
        source = staging / f"attachment-{len(files)}"
        size, digest = _stage_stable_attachment(path, root, source)
        files.append(_ArchiveFile(archive_path, "attachment", source, size, digest))
    return files


def write_snapshot(
    db: Database,
    vault: Path,
    out: Path,
    *,
    force: bool,
) -> SnapshotReport:
    """Atomically write a DB-consistent snapshot without trusting managed projections."""
    out = Path(out)
    vault = Path(vault)
    out.parent.mkdir(parents=True, exist_ok=True)
    output_lock = FileLock(out.with_name(f".{out.name}.snapshot.lock"))
    with output_lock:
        if out.exists() and not force:
            raise FileExistsError(out)
        temporary_fd, temporary_raw = tempfile.mkstemp(
            prefix=f".{out.name}.snapshot-tmp-", dir=out.parent
        )
        os.close(temporary_fd)
        temporary_out = Path(temporary_raw)

        try:
            with tempfile.TemporaryDirectory() as directory:
                staging = Path(directory)
                cloned_db = staging / "wiki.db"
                with db.reader() as conn:
                    conn.execute("VACUUM INTO ?", (str(cloned_db),))

                clone = sqlite3.connect(cloned_db)
                clone.row_factory = sqlite3.Row
                try:
                    managed, normalized = _stage_managed_files(clone, staging)
                    schema = get_meta(clone, "schema_version")
                    embedding_model = get_meta(clone, "embedding_model")
                    embedding_dim = get_meta(clone, "embedding_dim")
                finally:
                    clone.close()

                attachments = _stage_attachment_files(vault, normalized, staging)
                archive_files = managed + attachments
                database_size, database_digest = _file_metadata(cloned_db)
                schema_version = int(schema) if schema else None
                manifest = {
                    "format": SNAPSHOT_FORMAT,
                    "format_version": 2,
                    "schema_version": schema_version,
                    "embedding_model": embedding_model,
                    "embedding_dim": int(embedding_dim) if embedding_dim else None,
                    "doc_count": len(managed),
                    "created_at": now_iso(),
                    "database": {
                        "size": database_size,
                        "sha256": database_digest,
                    },
                    "files": [file.manifest_entry for file in archive_files],
                }
                manifest_data = json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ).encode("utf-8")
                if len(archive_files) + 2 > MAX_SNAPSHOT_MEMBERS:
                    raise ValueError("snapshot member count limit exceeded")
                if len(manifest_data) > MAX_SNAPSHOT_MANIFEST_BYTES:
                    raise ValueError("snapshot manifest size limit exceeded")
                member_sizes = [
                    database_size,
                    len(manifest_data),
                    *(file.size for file in archive_files),
                ]
                if any(size > MAX_SNAPSHOT_MEMBER_BYTES for size in member_sizes):
                    raise ValueError("snapshot member size limit exceeded")
                if sum(member_sizes) > MAX_SNAPSHOT_TOTAL_BYTES:
                    raise ValueError("snapshot total size limit exceeded")
                with tarfile.open(temporary_out, "w") as tar:
                    tar.add(cloned_db, arcname="wiki.db")
                    for file in archive_files:
                        _add_staged_file(tar, file)
                    _add_bytes(tar, "manifest.json", manifest_data)
                _verify_archive(
                    temporary_out,
                    [
                        _ArchiveFile(
                            "wiki.db",
                            "database",
                            cloned_db,
                            database_size,
                            database_digest,
                        ),
                        *archive_files,
                    ],
                )
                if temporary_out.stat().st_size > MAX_SNAPSHOT_ARCHIVE_BYTES:
                    raise ValueError("snapshot archive size limit exceeded")
            if force:
                os.replace(temporary_out, out)
            else:
                os.link(temporary_out, out)
                temporary_out.unlink()
        except BaseException:
            temporary_out.unlink(missing_ok=True)
            raise

    return SnapshotReport(
        schema_version=schema_version,
        doc_count=len(managed),
        file_count=len(archive_files),
        embedding_model=embedding_model,
    )


def _safe_member_name(name: str) -> tuple[str, str]:
    if "\\" in name or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise ValueError("unsafe archive member")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ValueError("unsafe archive member")
    normalized = path.as_posix()
    return normalized, unicodedata.normalize("NFC", normalized).casefold()


def _copy_archive_file(source: IO[bytes], target: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        while chunk := source.read(_COPY_CHUNK_SIZE):
            if size + len(chunk) > MAX_SNAPSHOT_MEMBER_BYTES:
                raise ValueError("snapshot member size limit exceeded")
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


@contextmanager
def _open_stable_snapshot_archive(path: Path) -> Iterator[IO[bytes]]:
    fd = -1
    source: IO[bytes] | None = None
    primary_error: BaseException | None = None
    try:
        visible = os.lstat(path)
        if not stat.S_ISREG(visible.st_mode) or stat.S_ISLNK(visible.st_mode):
            raise ValueError("snapshot archive must be a regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _stat_identity(visible) != _stat_identity(
            before
        ):
            raise ValueError("snapshot archive must be a stable regular file")
        if before.st_size > MAX_SNAPSHOT_ARCHIVE_BYTES:
            raise ValueError("snapshot archive size limit exceeded")
        source = os.fdopen(fd, "rb")
        fd = -1
        try:
            yield source
        except BaseException as exc:
            primary_error = exc
        cleanup_errors: list[BaseException] = []
        try:
            after = os.fstat(source.fileno())
            if _stat_identity(before) != _stat_identity(after):
                cleanup_errors.append(
                    ValueError("snapshot archive changed while reading")
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
        current_source, source = source, None
        try:
            current_source.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if primary_error is not None:
            if cleanup_errors:
                raise SnapshotArchiveReadError(
                    primary_error, cleanup_errors
                ) from primary_error
            raise primary_error
        if cleanup_errors:
            raise cleanup_errors[0]
    except OSError as exc:
        if primary_error is not None:
            raise
        raise ValueError("snapshot archive must be a stable regular file") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _read_restore_manifest(
    archive: tarfile.TarFile,
) -> tuple[dict[str, object], dict[str, tarfile.TarInfo]]:
    members: dict[str, tarfile.TarInfo] = {}
    identities: set[str] = set()
    total_size = 0
    for index, member in enumerate(archive, start=1):
        if index > MAX_SNAPSHOT_MEMBERS:
            raise ValueError("snapshot member count limit exceeded")
        name, identity = _safe_member_name(member.name)
        if identity in identities:
            raise ValueError("duplicate archive member")
        identities.add(identity)
        if not member.isfile():
            raise ValueError("archive members must be regular files")
        if member.size < 0 or member.size > MAX_SNAPSHOT_MEMBER_BYTES:
            raise ValueError("snapshot member size limit exceeded")
        total_size += member.size
        if total_size > MAX_SNAPSHOT_TOTAL_BYTES:
            raise ValueError("snapshot total size limit exceeded")
        members[name] = member

    manifest_member = members.get("manifest.json")
    if manifest_member is None:
        raise ValueError("missing manifest.json")
    if manifest_member.size > MAX_SNAPSHOT_MANIFEST_BYTES:
        raise ValueError("snapshot manifest size limit exceeded")
    manifest_file = archive.extractfile(manifest_member)
    if manifest_file is None:
        raise ValueError("missing manifest.json")
    try:
        manifest_data = manifest_file.read(MAX_SNAPSHOT_MANIFEST_BYTES + 1)
        if len(manifest_data) > MAX_SNAPSHOT_MANIFEST_BYTES:
            raise ValueError("snapshot manifest size limit exceeded")
        manifest = json.loads(manifest_data)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("invalid manifest.json")
    format_version = manifest.get("format_version")
    if format_version not in (1, 2):
        raise ValueError("unsupported snapshot format version")
    manifest_schema = manifest.get("schema_version")
    if (
        not isinstance(manifest_schema, int)
        or isinstance(manifest_schema, bool)
        or manifest_schema > SCHEMA_VERSION
    ):
        raise ValueError("unsupported snapshot schema version")
    if "wiki.db" not in members:
        raise ValueError("missing wiki.db")
    if format_version == 2:
        database = manifest.get("database")
        if not isinstance(database, dict):
            raise ValueError("invalid manifest database")
        size, digest = database.get("size"), database.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("invalid manifest database size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("invalid manifest database hash")
    return manifest, members


def _manifest_files(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("invalid manifest files")
    files: dict[str, dict[str, object]] = {}
    identities: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ValueError("invalid manifest entry")
        path = raw.get("path")
        kind = raw.get("kind")
        size = raw.get("size")
        digest = raw.get("sha256")
        if not isinstance(path, str) or not path.startswith("vault/"):
            raise ValueError("invalid manifest path")
        normalized, identity = _safe_member_name(path)
        if normalized != path or identity in identities:
            raise ValueError("duplicate or unsafe manifest path")
        if kind not in ("managed", "attachment"):
            raise ValueError("invalid manifest kind")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("invalid manifest size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("invalid manifest hash")
        identities.add(identity)
        files[path] = raw
    return files


def _validate_staged_database(
    db_path: Path,
    vault: Path,
    manifest: dict[str, object],
    files: dict[str, dict[str, object]],
) -> tuple[int | None, int]:
    try:
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True
        )
        conn.row_factory = sqlite3.Row
        try:
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
            except sqlite3.Error as exc:
                raise ValueError("snapshot database integrity check failed") from exc
            if len(integrity) != 1 or integrity[0][0] != "ok":
                raise ValueError("snapshot database integrity check failed")
            schema_raw = get_meta(conn, "schema_version")
            schema_version = int(schema_raw) if schema_raw is not None else None
            if schema_version is None or schema_version > SCHEMA_VERSION:
                raise ValueError("unsupported snapshot schema version")
            rows = list(
                conn.execute(
                    "SELECT d.path, d.content_hash, r.body, "
                    "r.content_hash AS revision_hash FROM documents d "
                    "JOIN revisions r ON r.doc_id=d.id AND r.version=d.version "
                    "WHERE d.is_deleted=0 ORDER BY d.path_norm"
                )
            )
            expected = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
            ).fetchone()[0]
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("invalid snapshot database") from exc

    if len(rows) != expected:
        raise ValueError("active document has no current revision")
    if manifest.get("schema_version") != schema_version:
        raise ValueError("manifest schema version mismatch")
    doc_count = manifest.get("doc_count")
    if not isinstance(doc_count, int) or isinstance(doc_count, bool) or doc_count != len(rows):
        raise ValueError("manifest document count mismatch")

    managed_paths = {path for path, entry in files.items() if entry["kind"] == "managed"}
    expected_managed: set[str] = set()
    for row in rows:
        archive_path, _ = _normalized_archive_path(str(row["path"]))
        expected_managed.add(archive_path)
        entry = files.get(archive_path)
        if entry is None or entry["kind"] != "managed":
            raise ValueError("manifest is missing a managed document")
        body = str(row["body"])
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if row["content_hash"] != digest or row["revision_hash"] != digest:
            raise ValueError("snapshot database document hash mismatch")
        try:
            staged_body = (vault / archive_path.removeprefix("vault/")).read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            raise ValueError("invalid staged managed document") from exc
        if staged_body != body:
            raise ValueError("staged managed document mismatch")
    if managed_paths != expected_managed:
        raise ValueError("manifest contains an undeclared managed document")
    return schema_version, len(rows)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _backup_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.restore-backup-{uuid.uuid4().hex}")


def restore_journal_path(db_path: Path) -> Path:
    db_path = Path(db_path)
    return db_path.with_name(f".{db_path.name}.restore-journal.json")


def validate_restore_layout(db_path: Path, vault: Path) -> None:
    """Reject layouts where replacing the vault can consume database machinery."""
    db_path, vault = Path(db_path), Path(vault)
    resolved_vault = vault.resolve(strict=False)
    database_paths = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        restore_journal_path(db_path),
        db_path.parent / ".llm-wiki.lock",
    )
    for candidate in database_paths:
        resolved = candidate.resolve(strict=False)
        if (
            resolved == resolved_vault
            or resolved_vault in resolved.parents
            or resolved in resolved_vault.parents
        ):
            raise ValueError(
                "database and vault paths overlap; choose separate sibling locations"
            )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_staging(staged_db: Path, staged_vault: Path) -> None:
    _fsync_file(staged_db)
    directories = [staged_vault]
    for path in staged_vault.rglob("*"):
        if path.is_dir():
            directories.append(path)
        else:
            _fsync_file(path)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _path_fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    """Return a stable identity/content proof, rejecting symlinks and special files."""
    visible = os.lstat(path)
    kind = stat.S_IFMT(visible.st_mode)
    if stat.S_ISLNK(visible.st_mode):
        raise ValueError(f"restore target must not be a symlink: {path}")
    if stat.S_ISREG(visible.st_mode):
        fd = -1
        try:
            fd = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            before = os.fstat(fd)
            if _stat_identity(visible) != _stat_identity(before):
                raise ValueError(f"restore target identity changed: {path}")
            file_digest = hashlib.sha256()
            size = 0
            while chunk := os.read(fd, _COPY_CHUNK_SIZE):
                file_digest.update(chunk)
                size += len(chunk)
            after = os.fstat(fd)
            if _stat_identity(before) != _stat_identity(after) or size != before.st_size:
                raise ValueError(f"restore target changed while hashing: {path}")
            return before.st_dev, before.st_ino, kind, size, file_digest.hexdigest()
        finally:
            if fd >= 0:
                os.close(fd)
    if not stat.S_ISDIR(visible.st_mode):
        raise ValueError(f"restore target must be a regular file or directory: {path}")
    entries: list[tuple[object, ...]] = []
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = child.relative_to(path).as_posix()
        child_visible = os.lstat(child)
        if stat.S_ISDIR(child_visible.st_mode):
            entries.append(
                (
                    relative,
                    "directory",
                    child_visible.st_dev,
                    child_visible.st_ino,
                )
            )
        elif stat.S_ISREG(child_visible.st_mode):
            fingerprint = _path_fingerprint(child)
            entries.append((relative, "file", *fingerprint))
        else:
            raise ValueError(f"restore vault contains a symlink or special file: {child}")
    after = os.lstat(path)
    if _stat_identity(visible) != _stat_identity(after):
        raise ValueError(f"restore target changed while hashing: {path}")
    tree_digest = hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return visible.st_dev, visible.st_ino, kind, len(entries), tree_digest


@dataclass
class _RestoreTarget:
    target: Path
    backup: Path
    had_original: bool
    backup_identity: tuple[int, int, int] | None = None
    target_identity: tuple[int, int, int] | None = None
    original_fingerprint: tuple[int, int, int, int, str] | None = None

    @property
    def serialized(self) -> dict[str, object]:
        return {
            "target": str(self.target),
            "backup": str(self.backup),
            "had_original": self.had_original,
            "backup_identity": list(self.backup_identity)
            if self.backup_identity is not None
            else None,
            "target_identity": list(self.target_identity)
            if self.target_identity is not None
            else None,
            "original_fingerprint": list(self.original_fingerprint)
            if self.original_fingerprint is not None
            else None,
        }


@dataclass
class _RestoreJournal:
    targets: tuple[_RestoreTarget, ...]
    replacement_targets: tuple[Path, ...]
    process_lock: ProjectLock
    path: Path
    state: str = "prepared"

    @property
    def original_targets(self) -> list[tuple[Path, Path]]:
        return [
            (item.target, item.backup) for item in self.targets if item.had_original
        ]

    def persist(self, state: str) -> None:
        self.state = state
        data = json.dumps(
            {
                "format": _RESTORE_JOURNAL_FORMAT,
                "version": _RESTORE_JOURNAL_VERSION,
                "state": state,
                "replacement_targets": [str(path) for path in self.replacement_targets],
                "targets": [item.serialized for item in self.targets],
            },
            sort_keys=True,
        ).encode("utf-8")
        fd, temporary_raw = tempfile.mkstemp(
            prefix=f".{self.path.name}.tmp-", dir=self.path.parent
        )
        temporary = Path(temporary_raw)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _finish_journal(self) -> None:
        self.path.unlink(missing_ok=True)
        _fsync_directory(self.path.parent)

    @staticmethod
    def _visible(path: Path) -> os.stat_result | None:
        try:
            return os.lstat(path)
        except FileNotFoundError:
            return None

    def _validate_paths(self) -> None:
        vault_target = self.replacement_targets[-1]
        for item in self.targets:
            visible_target = self._visible(item.target)
            if visible_target is not None and stat.S_ISLNK(visible_target.st_mode):
                raise ValueError(f"restore live target must not be a symlink: {item.target}")
            if (
                visible_target is not None
                and self.state in {"publishing", "pending", "finalizing"}
                and item.target_identity is not None
                and (
                    visible_target.st_dev,
                    visible_target.st_ino,
                    stat.S_IFMT(visible_target.st_mode),
                )
                != item.target_identity
            ):
                raise ValueError(f"restore live target identity changed: {item.target}")
            visible_backup = self._visible(item.backup)
            if item.had_original and item.original_fingerprint is None:
                raise ValueError(f"restore original fingerprint is missing: {item.target}")
            if self.state == "prepared" and item.had_original:
                if visible_backup is not None and visible_target is not None:
                    raise ValueError(
                        f"restore prepared target and backup both exist: {item.target}"
                    )
                original_path = item.backup if visible_backup is not None else item.target
                if self._visible(original_path) is None:
                    raise ValueError(f"restore original target is missing: {item.target}")
                if _path_fingerprint(original_path) != item.original_fingerprint:
                    raise ValueError(
                        f"restore original fingerprint changed: {original_path}"
                    )
            if (
                self.state == "rolling_back"
                and item.had_original
                and visible_backup is None
                and visible_target is not None
                and _path_fingerprint(item.target) != item.original_fingerprint
            ):
                raise ValueError(
                    f"restore original fingerprint changed: {item.target}"
                )
            if visible_backup is None:
                continue
            if stat.S_ISLNK(visible_backup.st_mode):
                raise ValueError(f"restore backup must not be a symlink: {item.backup}")
            expected_type = stat.S_ISDIR if item.target == vault_target else stat.S_ISREG
            if not expected_type(visible_backup.st_mode):
                raise ValueError(f"restore backup has an invalid type: {item.backup}")
            identity = (
                visible_backup.st_dev,
                visible_backup.st_ino,
                stat.S_IFMT(visible_backup.st_mode),
            )
            if item.backup_identity is not None and identity != item.backup_identity:
                raise ValueError(f"restore backup identity changed: {item.backup}")
            if (
                item.original_fingerprint is not None
                and _path_fingerprint(item.backup) != item.original_fingerprint
            ):
                raise ValueError(f"restore backup fingerprint changed: {item.backup}")

    def _rollback_files(self) -> list[OSError]:
        rollback_errors: list[OSError] = []
        prior_state = self.state
        self._validate_paths()
        if prior_state in {"publishing", "pending"}:
            missing = [
                item.backup
                for item in self.targets
                if item.had_original and not item.backup.exists()
            ]
            if missing:
                return [OSError(f"restore backup is missing: {missing[0]}")]
        if prior_state == "rolling_back":
            missing_both = [
                item.target
                for item in self.targets
                if item.had_original
                and self._visible(item.target) is None
                and self._visible(item.backup) is None
            ]
            if missing_both:
                return [
                    OSError(
                        "restore target and backup are both missing: "
                        f"{missing_both[0]}"
                    )
                ]
        try:
            self.persist("rolling_back")
        except OSError as exc:
            return [exc]

        for item in reversed(self.targets):
            should_remove = (
                prior_state != "prepared" and not item.had_original
            ) or item.backup.exists()
            if should_remove:
                try:
                    _remove_path(item.target)
                except OSError as exc:
                    rollback_errors.append(exc)
        for item in reversed(self.targets):
            if item.had_original and item.backup.exists():
                try:
                    os.replace(item.backup, item.target)
                except OSError as exc:
                    rollback_errors.append(exc)
        try:
            for parent in {item.target.parent for item in self.targets}:
                _fsync_directory(parent)
        except OSError as exc:
            rollback_errors.append(exc)
        if not rollback_errors:
            try:
                self._finish_journal()
            except OSError as exc:
                rollback_errors.append(exc)
        return rollback_errors

    def rollback(self, cause: BaseException) -> None:
        try:
            try:
                file_errors = self._rollback_files()
                rollback_errors: list[BaseException] = [*file_errors]
            except ValueError as exc:
                rollback_errors = [exc]
            if rollback_errors:
                preserved = tuple(
                    item.backup
                    for item in self.targets
                    if item.backup.exists() or item.backup.is_symlink()
                )
                raise RestoreRollbackError(cause, rollback_errors, preserved) from cause
            raise cause
        finally:
            self.process_lock.release()

    def finalize(self, *, release_lock: bool = True) -> tuple[Path, ...]:
        cleanup_warnings: list[Path] = []
        try:
            self._validate_paths()
            self.persist("finalizing")
            for _, backup in self.original_targets:
                try:
                    _remove_path(backup)
                except OSError:
                    cleanup_warnings.append(backup)
            if not cleanup_warnings:
                self._finish_journal()
        finally:
            if release_lock:
                self.process_lock.release()
        return tuple(cleanup_warnings)


def _read_restore_journal(path: Path) -> bytes | None:
    try:
        visible = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(visible.st_mode) or stat.S_ISLNK(visible.st_mode):
        raise ValueError(f"restore journal must be a regular file: {path}")
    fd = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _stat_identity(visible) != _stat_identity(
            before
        ):
            raise ValueError(f"restore journal must be a stable regular file: {path}")
        if before.st_size > MAX_SNAPSHOT_MANIFEST_BYTES:
            raise ValueError("restore journal is too large")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(_COPY_CHUNK_SIZE, MAX_SNAPSHOT_MANIFEST_BYTES + 1)):
            size += len(chunk)
            if size > MAX_SNAPSHOT_MANIFEST_BYTES:
                raise ValueError("restore journal is too large")
            chunks.append(chunk)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after):
            raise ValueError(f"restore journal changed while reading: {path}")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"restore journal must be a stable regular file: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _load_restore_journal(
    db_path: Path, vault: Path, process_lock: ProjectLock
) -> _RestoreJournal | None:
    path = restore_journal_path(db_path)
    raw = _read_restore_journal(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid restore journal: {path}") from exc
    expected_targets = (
        db_path.absolute(),
        Path(f"{db_path.absolute()}-wal"),
        Path(f"{db_path.absolute()}-shm"),
        vault.absolute(),
    )
    if (
        not isinstance(data, dict)
        or data.get("format") != _RESTORE_JOURNAL_FORMAT
        or data.get("version") != _RESTORE_JOURNAL_VERSION
        or data.get("state")
        not in {"prepared", "publishing", "pending", "rolling_back", "finalizing"}
        or data.get("replacement_targets") != [str(item) for item in expected_targets]
        or not isinstance(data.get("targets"), list)
        or len(data["targets"]) != len(expected_targets)
    ):
        raise ValueError(f"invalid restore journal: {path}")
    targets: list[_RestoreTarget] = []
    for expected, raw_target in zip(expected_targets, data["targets"], strict=True):
        if not isinstance(raw_target, dict):
            raise ValueError(f"invalid restore journal: {path}")
        target = Path(str(raw_target.get("target")))
        backup = Path(str(raw_target.get("backup")))
        had_original = raw_target.get("had_original")
        raw_identity = raw_target.get("backup_identity")
        backup_identity = (
            tuple(raw_identity)
            if isinstance(raw_identity, list)
            and len(raw_identity) == 3
            and all(isinstance(value, int) for value in raw_identity)
            else None
        )
        raw_target_identity = raw_target.get("target_identity")
        target_identity = (
            tuple(raw_target_identity)
            if isinstance(raw_target_identity, list)
            and len(raw_target_identity) == 3
            and all(isinstance(value, int) for value in raw_target_identity)
            else None
        )
        raw_original_fingerprint = raw_target.get("original_fingerprint")
        original_fingerprint = (
            tuple(raw_original_fingerprint)
            if isinstance(raw_original_fingerprint, list)
            and len(raw_original_fingerprint) == 5
            and all(
                isinstance(value, int)
                for value in raw_original_fingerprint[:4]
            )
            and isinstance(raw_original_fingerprint[4], str)
            else None
        )
        if (
            target != expected
            or backup.parent != expected.parent
            or not backup.name.startswith(f".{expected.name}.restore-backup-")
            or not isinstance(had_original, bool)
            or (raw_identity is not None and backup_identity is None)
            or (raw_target_identity is not None and target_identity is None)
            or (had_original and original_fingerprint is None)
            or (
                raw_original_fingerprint is not None
                and original_fingerprint is None
            )
        ):
            raise ValueError(f"invalid restore journal: {path}")
        targets.append(
            _RestoreTarget(
                target,
                backup,
                had_original,
                backup_identity,
                target_identity,
                original_fingerprint,
            )
        )
    return _RestoreJournal(
        tuple(targets),
        expected_targets,
        process_lock,
        path,
        str(data["state"]),
    )


def _recover_pending_restore(
    db_path: Path, vault: Path, process_lock: ProjectLock
) -> str | None:
    journal = _load_restore_journal(db_path, vault, process_lock)
    if journal is None:
        return None
    if journal.state == "finalizing":
        warnings = journal.finalize(release_lock=False)
        if warnings:
            raise OSError(f"restore backup cleanup failed: {warnings[0]}")
        return "finalized"
    errors = journal._rollback_files()
    if errors:
        raise RestoreRollbackError(
            RuntimeError("recovering interrupted restore"),
            errors,
            tuple(
                item.backup
                for item in journal.targets
                if item.backup.exists() or item.backup.is_symlink()
            ),
        )
    return "rolled_back"


def recover_pending_restore(db_path: Path, vault: Path) -> str | None:
    db_path, vault = Path(db_path), Path(vault)
    process_lock = ProjectLock(db_path).acquire()
    try:
        return _recover_pending_restore(db_path, vault, process_lock)
    finally:
        process_lock.release()


def _publish_restore(
    staged_db: Path,
    staged_vault: Path,
    db_path: Path,
    vault: Path,
    process_lock: ProjectLock,
) -> _RestoreJournal:
    targets = (
        db_path.absolute(),
        Path(f"{db_path.absolute()}-wal"),
        Path(f"{db_path.absolute()}-shm"),
        vault.absolute(),
    )
    records_list: list[_RestoreTarget] = []
    for index, target in enumerate(targets):
        try:
            visible = os.lstat(target)
        except FileNotFoundError:
            records_list.append(_RestoreTarget(target, _backup_name(target), False))
            continue
        if index == 3 and not stat.S_ISDIR(visible.st_mode):
            raise ValueError(f"restore vault target must be a directory: {target}")
        if index != 3 and not stat.S_ISREG(visible.st_mode):
            raise ValueError(f"restore database target must be a regular file: {target}")
        records_list.append(
            _RestoreTarget(
                target,
                _backup_name(target),
                True,
                original_fingerprint=_path_fingerprint(target),
            )
        )
    records = tuple(records_list)
    for record, staged in ((records[0], staged_db), (records[3], staged_vault)):
        visible = os.lstat(staged)
        record.target_identity = (
            visible.st_dev,
            visible.st_ino,
            stat.S_IFMT(visible.st_mode),
        )
    restore_journal = _RestoreJournal(
        records,
        targets,
        process_lock,
        restore_journal_path(db_path),
    )
    try:
        _fsync_staging(staged_db, staged_vault)
        restore_journal.persist("prepared")
        for item in records:
            if item.had_original:
                os.replace(item.target, item.backup)
                visible = os.lstat(item.backup)
                item.backup_identity = (
                    visible.st_dev,
                    visible.st_ino,
                    stat.S_IFMT(visible.st_mode),
                )
                if _path_fingerprint(item.backup) != item.original_fingerprint:
                    raise ValueError(
                        f"restore backup fingerprint changed during rename: {item.backup}"
                    )
        for parent in {item.target.parent for item in records}:
            _fsync_directory(parent)
        restore_journal.persist("publishing")
        os.replace(staged_db, db_path)
        os.replace(staged_vault, vault)
        for parent in {item.target.parent for item in records}:
            _fsync_directory(parent)
        restore_journal.persist("pending")
    except BaseException as publish_error:
        restore_journal.rollback(publish_error)
    return restore_journal


def restore_snapshot(
    src: Path,
    db_path: Path,
    vault: Path,
    *,
    force: bool,
) -> RestoreReport:
    """Validate a snapshot in sibling staging paths, then replace live targets."""
    src, db_path, vault = Path(src), Path(db_path), Path(vault)
    validate_restore_layout(db_path, vault)
    process_lock = ProjectLock(db_path).acquire()
    staged_db: Path | None = None
    try:
        _recover_pending_restore(db_path, vault, process_lock)
        db_nonempty = db_path.exists() and db_path.stat().st_size > 0
        vault_nonempty = vault.exists() and any(vault.iterdir())
        if (db_nonempty or vault_nonempty) and not force:
            raise FileExistsError("target database or vault is not empty")

        db_path.parent.mkdir(parents=True, exist_ok=True)
        vault.parent.mkdir(parents=True, exist_ok=True)
        db_fd, staged_db_raw = tempfile.mkstemp(
            prefix=f".{db_path.name}.restore-stage-", dir=db_path.parent
        )
        os.close(db_fd)
        staged_db = Path(staged_db_raw)
        staged_vault = Path(
            tempfile.mkdtemp(prefix=f".{vault.name}.restore-stage-", dir=vault.parent)
        )
    except BaseException:
        if staged_db is not None:
            staged_db.unlink(missing_ok=True)
        process_lock.release()
        raise
    primary_error: BaseException | None = None
    published = False
    try:
        try:
            with _open_stable_snapshot_archive(src) as archive_source:
                with tarfile.open(fileobj=archive_source, mode="r:") as archive:
                    manifest, members = _read_restore_manifest(archive)
                    files = _manifest_files(manifest)
                    expected_members = {"manifest.json", "wiki.db", *files}
                    actual_members = set(members)
                    if actual_members != expected_members:
                        missing = expected_members - actual_members
                        if missing:
                            raise ValueError("manifest payload is missing from archive")
                        raise ValueError("archive contains undeclared payload")

                    for path in ["wiki.db", *files]:
                        extracted = archive.extractfile(members[path])
                        if extracted is None:
                            raise ValueError("archive payload is missing")
                        target = (
                            staged_db
                            if path == "wiki.db"
                            else staged_vault / path.removeprefix("vault/")
                        )
                        size, digest = _copy_archive_file(extracted, target)
                        if path == "wiki.db" and manifest.get("format_version") == 2:
                            database = manifest["database"]
                            assert isinstance(database, dict)
                            if size != database["size"] or digest != database["sha256"]:
                                raise ValueError(
                                    "snapshot database verification failed: hash mismatch; "
                                    "invalid snapshot database"
                                )
                        elif path != "wiki.db":
                            entry = files[path]
                            if size != entry["size"] or digest != entry["sha256"]:
                                raise ValueError("manifest payload verification failed")
        except tarfile.TarError as exc:
            raise ValueError("invalid snapshot archive") from exc

        schema_version, doc_count = _validate_staged_database(
            staged_db, staged_vault, manifest, files
        )
        journal = _publish_restore(
            staged_db, staged_vault, db_path, vault, process_lock
        )
        published = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        staging_cleanup_errors: list[OSError] = []
        for staged_path in (staged_db, staged_vault):
            try:
                _remove_path(staged_path)
            except OSError as exc:
                staging_cleanup_errors.append(exc)
        if isinstance(primary_error, RestoreRollbackError):
            primary_error.staging_cleanup_errors = tuple(staging_cleanup_errors)
        elif primary_error is None and staging_cleanup_errors:
            journal.rollback(staging_cleanup_errors[0])
        elif primary_error is not None and staging_cleanup_errors:
            process_lock.release()
            remnants = tuple(
                path
                for path in (staged_db, staged_vault)
                if path.exists() or path.is_symlink()
            )
            raise RestorePreparationError(
                primary_error, staging_cleanup_errors, remnants
            ) from primary_error
        if primary_error is not None and not published:
            process_lock.release()

    embedding_model = manifest.get("embedding_model")
    return RestoreReport(
        schema_version,
        doc_count,
        embedding_model=embedding_model if isinstance(embedding_model, str) else None,
        _journal=journal,
    )
