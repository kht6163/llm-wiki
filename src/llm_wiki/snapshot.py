"""Generation-consistent database and vault snapshots."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .db import Database, get_meta
from .util import now_iso

SNAPSHOT_FORMAT = "llm-wiki-snapshot"
_ATTACHMENT_ATTEMPTS = 3


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
    data: bytes

    @property
    def manifest_entry(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
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


def _stat_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_attachment_once(path: Path) -> bytes | None:
    """Read a file only when its filesystem generation remains unchanged."""
    try:
        before = _stat_identity(path)
        data = path.read_bytes()
        after = _stat_identity(path)
    except (FileNotFoundError, OSError):
        return None
    if before != after or len(data) != before[2]:
        return None
    return data


def _read_stable_attachment(path: Path) -> bytes:
    for _ in range(_ATTACHMENT_ATTEMPTS):
        data = _read_attachment_once(path)
        if data is not None:
            return data
    raise RuntimeError(f"attachment changed while snapshotting: {path.name}")


def _add_bytes(tar: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _verify_archive(path: Path, files: list[_ArchiveFile]) -> None:
    """Verify the bytes actually persisted by tar before publishing the archive."""
    with tarfile.open(path, "r") as tar:
        for file in files:
            archived = tar.extractfile(file.path)
            if archived is None:
                raise RuntimeError(f"snapshot file verification failed: {file.path}")
            data = archived.read()
            expected = file.manifest_entry
            if (
                len(data) != expected["size"]
                or hashlib.sha256(data).hexdigest() != expected["sha256"]
            ):
                raise RuntimeError(f"snapshot file verification failed: {file.path}")


def _managed_files(conn: sqlite3.Connection) -> tuple[list[_ArchiveFile], set[str]]:
    rows = conn.execute(
        "SELECT d.path, r.body FROM documents d "
        "JOIN revisions r ON r.doc_id=d.id AND r.version=d.version "
        "WHERE d.is_deleted=0 ORDER BY d.path_norm"
    ).fetchall()
    expected = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
    ).fetchone()[0]
    if len(rows) != expected:
        raise RuntimeError("active document has no revision at its current version")

    files: list[_ArchiveFile] = []
    normalized: set[str] = set()
    for row in rows:
        archive_path, identity = _normalized_archive_path(str(row["path"]))
        if identity in normalized:
            raise ValueError(f"duplicate normalized snapshot path: {archive_path}")
        normalized.add(identity)
        files.append(_ArchiveFile(archive_path, "managed", str(row["body"]).encode("utf-8")))
    return files, normalized


def _attachment_files(vault: Path, normalized: set[str]) -> list[_ArchiveFile]:
    if not vault.exists():
        return []
    root = vault.resolve()
    files: list[_ArchiveFile] = []
    for path in sorted(vault.rglob("*"), key=lambda item: item.as_posix()):
        rel_path = path.relative_to(vault)
        if rel_path.parts and rel_path.parts[0] == ".tmp":
            continue
        if path.suffix.lower() == ".md" or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved == root or root not in resolved.parents:
            raise ValueError(f"unsafe vault path: {rel_path.as_posix()!r}")
        archive_path, identity = _normalized_archive_path(rel_path.as_posix())
        if identity in normalized:
            raise ValueError(f"duplicate normalized snapshot path: {archive_path}")
        normalized.add(identity)
        files.append(_ArchiveFile(archive_path, "attachment", _read_stable_attachment(path)))
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
            cloned_db = Path(directory) / "wiki.db"
            with db.reader() as conn:
                conn.execute("VACUUM INTO ?", (str(cloned_db),))

            clone = sqlite3.connect(cloned_db)
            clone.row_factory = sqlite3.Row
            try:
                managed, normalized = _managed_files(clone)
                schema = get_meta(clone, "schema_version")
                embedding_model = get_meta(clone, "embedding_model")
                embedding_dim = get_meta(clone, "embedding_dim")
            finally:
                clone.close()

            attachments = _attachment_files(vault, normalized)
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
                    _add_bytes(tar, file.path, file.data)
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
