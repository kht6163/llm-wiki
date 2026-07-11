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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from .db import SCHEMA_VERSION, Database, get_meta
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
class RestoreReport:
    schema_version: int | None
    doc_count: int
    recovered: int = 0
    embedding_model: str | None = None


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
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _read_restore_manifest(
    archive: tarfile.TarFile,
) -> tuple[dict[str, object], dict[str, tarfile.TarInfo]]:
    members: dict[str, tarfile.TarInfo] = {}
    identities: set[str] = set()
    for member in archive.getmembers():
        name, identity = _safe_member_name(member.name)
        if identity in identities:
            raise ValueError("duplicate archive member")
        identities.add(identity)
        if not member.isfile():
            raise ValueError("archive members must be regular files")
        members[name] = member

    manifest_member = members.get("manifest.json")
    if manifest_member is None:
        raise ValueError("missing manifest.json")
    manifest_file = archive.extractfile(manifest_member)
    if manifest_file is None:
        raise ValueError("missing manifest.json")
    try:
        manifest = json.load(manifest_file)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid manifest.json") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("invalid manifest.json")
    if manifest.get("format_version") != 1:
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


def _publish_restore(staged_db: Path, staged_vault: Path, db_path: Path, vault: Path) -> None:
    targets = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), vault]
    journal: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for target in targets:
            if target.exists() or target.is_symlink():
                backup = _backup_name(target)
                os.replace(target, backup)
                journal.append((target, backup))
        os.replace(staged_db, db_path)
        published.append(db_path)
        os.replace(staged_vault, vault)
        published.append(vault)
    except BaseException as publish_error:
        rollback_errors: list[OSError] = []
        for target in reversed(published):
            try:
                _remove_path(target)
            except OSError as exc:
                rollback_errors.append(exc)
        for target, backup in reversed(journal):
            try:
                os.replace(backup, target)
            except OSError as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError(
                "restore publication failed and rollback could not complete; "
                "restore backups were preserved"
            ) from publish_error
        raise
    for _, backup in journal:
        _remove_path(backup)


def restore_snapshot(
    src: Path,
    db_path: Path,
    vault: Path,
    *,
    force: bool,
) -> RestoreReport:
    """Validate a snapshot in sibling staging paths, then replace live targets."""
    src, db_path, vault = Path(src), Path(db_path), Path(vault)
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
    try:
        try:
            with tarfile.open(src, "r") as archive:
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
                    if path != "wiki.db":
                        entry = files[path]
                        if size != entry["size"] or digest != entry["sha256"]:
                            raise ValueError("manifest payload verification failed")
        except tarfile.TarError as exc:
            raise ValueError("invalid snapshot archive") from exc

        schema_version, doc_count = _validate_staged_database(
            staged_db, staged_vault, manifest, files
        )
        _publish_restore(staged_db, staged_vault, db_path, vault)
    finally:
        _remove_path(staged_db)
        _remove_path(staged_vault)

    embedding_model = manifest.get("embedding_model")
    return RestoreReport(
        schema_version,
        doc_count,
        embedding_model=embedding_model if isinstance(embedding_model, str) else None,
    )
