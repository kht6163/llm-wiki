"""Generation-consistent database and vault snapshots."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .db import Database, get_meta
from .util import now_iso

SNAPSHOT_FORMAT = "llm-wiki-snapshot"
_ATTACHMENT_ATTEMPTS = 3
_COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class SnapshotReport:
    schema_version: int | None
    doc_count: int
    file_count: int
    embedding_model: str | None


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
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        before = os.fstat(fd)
        visible = os.lstat(path)
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


def _verify_archive(path: Path, files: list[_ArchiveFile]) -> None:
    """Verify the bytes actually persisted by tar before publishing the archive."""
    with tarfile.open(path, "r") as tar:
        for file in files:
            archived = tar.extractfile(file.path)
            if archived is None:
                raise RuntimeError(f"snapshot file verification failed: {file.path}")
            digest = hashlib.sha256()
            size = 0
            while chunk := archived.read(_COPY_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
            if size != file.size or digest.hexdigest() != file.sha256:
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
        if path.suffix.lower() == ".md" or path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError(f"unsafe vault path: {rel_path.as_posix()!r}")
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
    if out.exists() and not force:
        raise FileExistsError(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary_out = Path(f"{out}.tmp")
    temporary_out.unlink(missing_ok=True)

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
            schema_version = int(schema) if schema else None
            manifest = {
                "format": SNAPSHOT_FORMAT,
                "format_version": 1,
                "schema_version": schema_version,
                "embedding_model": embedding_model,
                "embedding_dim": int(embedding_dim) if embedding_dim else None,
                "doc_count": len(managed),
                "created_at": now_iso(),
                "files": [file.manifest_entry for file in archive_files],
            }
            with tarfile.open(temporary_out, "w") as tar:
                tar.add(cloned_db, arcname="wiki.db")
                for file in archive_files:
                    _add_staged_file(tar, file)
                manifest_data = json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ).encode("utf-8")
                _add_bytes(tar, "manifest.json", manifest_data)
            _verify_archive(temporary_out, archive_files)
        os.replace(temporary_out, out)
    except BaseException:
        temporary_out.unlink(missing_ok=True)
        raise

    return SnapshotReport(
        schema_version=schema_version,
        doc_count=len(managed),
        file_count=len(archive_files),
        embedding_model=embedding_model,
    )
