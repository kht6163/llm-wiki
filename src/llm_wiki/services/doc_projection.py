"""Filesystem projection and recovery primitives for DocumentService.

DB is the source of truth; vault .md files are atomic projections. These helpers
own the pending/clean/purge projection lifecycle. Public entry points remain on
DocumentService as thin delegates.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .. import file_projection as fp
from .. import graph
from ..util import (
    clamp_int,
    sha256_hex,
)
from . import audit
from .errors import WikiError

if TYPE_CHECKING:
    pass

log = logging.getLogger("llm_wiki.documents")


class _ProjectionTokenState(StrEnum):
    MISSING = "missing"
    PURGE_PENDING = "purge_pending"
    CHANGED = "changed"
    CLEANUP_PENDING = "cleanup_pending"
    SETTLED = "settled"
    CURRENT_CLEANUP = "current_cleanup"
    CURRENT = "current"

@dataclass(frozen=True)
class ProjectionSnapshot:
    """One canonical document generation captured by a single DB read."""

    doc_id: int
    path: str
    path_norm: str
    version: int
    content_hash: str
    is_deleted: bool
    file_state: str
    revision_version: int | None
    revision_content_hash: str | None
    body: str | None
    has_purge_intent: bool
    has_cleanup_intent: bool

@dataclass(frozen=True)
class RecoveryReport:
    recovered: int
    issues: tuple[fp.ProjectionResult, ...]

@dataclass(frozen=True)
class CleanupIssue:
    path: str
    reason: str
    detail: str | None = None

@dataclass(frozen=True)
class PurgeIntentSnapshot:
    doc_id: int
    path: str
    path_norm: str
    version: int
    file_state: str
    actor: str
    via: str

@dataclass(frozen=True)
class ReindexTargetSnapshot:
    doc_id: int
    path: str
    path_norm: str
    version: int
    content_hash: str
    is_deleted: bool
    file_state: str
    has_purge_intent: bool
    has_cleanup_intent: bool

class ProjectionPendingError(WikiError):
    """A filesystem projection that remains recoverable.

    Most instances follow a durable DB mutation, but purge also performs projection
    safety checks *before* recording its durable intent.  ``committed`` keeps those
    two cases machine-distinguishable so a caller does not mistake a safe retry for
    a duplicate write.
    """

    code = "projection_pending"
    http_status = 202
    suggested_action = "check_status_do_not_repeat_write"

    def __init__(
        self,
        result: fp.ProjectionResult,
        *,
        version: int | None = None,
        committed: bool = True,
    ):
        detail = f": {result.detail}" if result.detail else ""
        self.suggested_action = (
            "check_status_do_not_repeat_write" if committed else "retry_after_recovery"
        )
        self.http_status = 202 if committed else 409
        super().__init__(
            f"Document file projection remains pending ({result.reason or 'unknown'}){detail}",
            committed=committed,
            path=result.path,
            version=version,
            projection_reason=result.reason,
            projection_attempts=result.attempts,
        )
        self.result = result
        self.committed = committed

def _projection_snapshot(
    svc, conn: sqlite3.Connection, doc_id: int
) -> ProjectionSnapshot | None:
    """Load a document and its exact current revision in one SQLite snapshot."""
    row = conn.execute(
        "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,d.is_deleted,"
        "d.file_state,r.version AS revision_version,"
        "r.content_hash AS revision_content_hash,r.body,"
        "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
        "AS has_purge_intent,"
        "EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id) "
        "AS has_cleanup_intent "
        "FROM documents d LEFT JOIN revisions r "
        "ON r.doc_id=d.id AND r.version=d.version WHERE d.id=?",
        (int(doc_id),),
    ).fetchone()
    if row is None:
        return None
    return ProjectionSnapshot(
        doc_id=int(row["id"]),
        path=str(row["path"]),
        path_norm=str(row["path_norm"]),
        version=int(row["version"]),
        content_hash=str(row["content_hash"]),
        is_deleted=bool(row["is_deleted"]),
        file_state=str(row["file_state"]),
        revision_version=(
            int(row["revision_version"]) if row["revision_version"] is not None else None
        ),
        revision_content_hash=(
            str(row["revision_content_hash"])
            if row["revision_content_hash"] is not None
            else None
        ),
        body=str(row["body"]) if row["body"] is not None else None,
        has_purge_intent=bool(row["has_purge_intent"]),
        has_cleanup_intent=bool(row["has_cleanup_intent"]),
    )

def _projection_token_state(
    svc,
    conn: sqlite3.Connection,
    snapshot: ProjectionSnapshot,
    *,
    allow_cleanup: bool = False,
) -> _ProjectionTokenState:
    """Revalidate a staged generation without loading its potentially large body."""
    row = conn.execute(
        "SELECT d.path,d.path_norm,d.version,d.content_hash,d.is_deleted,d.file_state,"
        "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
        "AS has_purge_intent,"
        "EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id) "
        "AS has_cleanup_intent,"
        "EXISTS(SELECT 1 FROM revisions r WHERE r.doc_id=d.id "
        "AND r.version=d.version AND r.content_hash=d.content_hash) AS exact_revision "
        "FROM documents d WHERE d.id=?",
        (snapshot.doc_id,),
    ).fetchone()
    if row is None:
        return _ProjectionTokenState.MISSING
    if row["has_purge_intent"]:
        return _ProjectionTokenState.PURGE_PENDING
    current_token = (
        str(row["path"]),
        str(row["path_norm"]),
        int(row["version"]),
        str(row["content_hash"]),
        bool(row["is_deleted"]),
    )
    staged_token = (
        snapshot.path,
        snapshot.path_norm,
        snapshot.version,
        snapshot.content_hash,
        snapshot.is_deleted,
    )
    if current_token != staged_token or not row["exact_revision"]:
        return _ProjectionTokenState.CHANGED
    if row["file_state"] == "clean":
        if row["has_cleanup_intent"]:
            return _ProjectionTokenState.CLEANUP_PENDING
        return _ProjectionTokenState.SETTLED
    if row["has_cleanup_intent"]:
        return (
            _ProjectionTokenState.CURRENT_CLEANUP
            if allow_cleanup
            else _ProjectionTokenState.CLEANUP_PENDING
        )
    return _ProjectionTokenState.CURRENT

def _install_projection_target(
    svc,
    snapshot: ProjectionSnapshot,
    staged: fp.StagedText,
    target: Path,
    live_target: Path,
    trash_target: Path,
) -> float | None:
    installed = fp.install_staged(staged, target)
    if snapshot.is_deleted:
        fp.unlink_regular(live_target, vault=svc.vault)
        return None
    fp.unlink_regular(trash_target, vault=svc.vault)
    return installed.mtime_ns / 1_000_000_000

def _mark_projection_clean(
    svc,
    conn: sqlite3.Connection,
    snapshot: ProjectionSnapshot,
    file_mtime: float | None,
) -> None:
    changed = conn.execute(
        "UPDATE documents SET file_state='clean',file_mtime=? "
        "WHERE id=? AND path=? AND path_norm=? AND version=? "
        "AND content_hash=? AND is_deleted=? AND file_state='pending' "
        "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
        "WHERE p.doc_id=?) "
        "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
        "WHERE c.doc_id=?) "
        "AND EXISTS(SELECT 1 FROM revisions r WHERE r.doc_id=? "
        "AND r.version=? AND r.content_hash=?)",
        (
            file_mtime,
            snapshot.doc_id,
            snapshot.path,
            snapshot.path_norm,
            snapshot.version,
            snapshot.content_hash,
            int(snapshot.is_deleted),
            snapshot.doc_id,
            snapshot.doc_id,
            snapshot.doc_id,
            snapshot.version,
            snapshot.content_hash,
        ),
    )
    if changed.rowcount != 1:
        raise RuntimeError("projection fence changed inside the serialized writer")

def _expected_cleanup_signature(row: sqlite3.Row) -> fp.FileSignature | None:
    if not row["expected_exists"]:
        return None
    values = (
        row["expected_dev"],
        row["expected_ino"],
        row["expected_size"],
        row["expected_mtime_ns"],
        row["expected_ctime_ns"],
    )
    return fp.FileSignature(*(int(value) for value in values))

def _process_cleanup_batch(
    svc,
    conn: sqlite3.Connection,
    snapshot: ProjectionSnapshot,
    *,
    after_norm: str,
    batch_size: int = 64,
) -> tuple[str | None, tuple[CleanupIssue, ...]]:
    """Visit one cleanup keyset page, preserving only unsafe/conflicting rows."""
    rows = conn.execute(
        "SELECT path,path_norm,expected_exists,expected_dev,expected_ino,"
        "expected_size,expected_mtime_ns,expected_ctime_ns "
        "FROM file_projection_cleanup WHERE doc_id=? AND path_norm>? "
        "ORDER BY path_norm LIMIT ?",
        (snapshot.doc_id, after_norm, clamp_int(batch_size, 1, 64)),
    ).fetchall()
    if not rows:
        return None, ()

    issues: list[CleanupIssue] = []
    for row in rows:
        rel = str(row["path"])
        norm = str(row["path_norm"])

        # A stale intent must never remove the document's current target. Moves
        # back to an old path normally delete this row in their own transaction;
        # this guard also makes manually repaired/inconsistent rows harmless.
        if norm == snapshot.path_norm:
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (snapshot.doc_id, norm),
            )
            continue

        owner = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? LIMIT 1",
            (norm,),
        ).fetchone()
        if owner is not None:
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (snapshot.doc_id, norm),
            )
            continue

        try:
            target = fp.managed_path(svc.vault, rel, namespace="live")
            current = fp.confined_file_signature(svc.vault, target, missing_ok=True)
            if current is None:
                if fp.confirm_confined_absence(svc.vault, target):
                    conn.execute(
                        "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                        (snapshot.doc_id, norm),
                    )
                else:
                    issues.append(CleanupIssue(rel, "cleanup_changed"))
                continue
            expected = _expected_cleanup_signature(row)
            if expected is None or current != expected:
                issues.append(CleanupIssue(rel, "cleanup_changed"))
                continue
            if not fp.unlink_regular(target, expected=expected, vault=svc.vault):
                issues.append(CleanupIssue(rel, "cleanup_changed"))
                continue
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (snapshot.doc_id, norm),
            )
        except (OSError, fp.FileProjectionError) as exc:
            issues.append(
                CleanupIssue(
                    rel,
                    "cleanup_io_error",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return str(rows[-1]["path_norm"]), tuple(issues)

def _purge_intent_snapshot(conn: sqlite3.Connection, doc_id: int) -> PurgeIntentSnapshot | None:
    row = conn.execute(
        "SELECT p.doc_id,p.path,p.path_norm,p.version,p.actor,p.via,"
        "d.path AS document_path,d.path_norm AS document_path_norm,"
        "d.version AS document_version,d.file_state,d.is_deleted "
        "FROM document_purge_intents p JOIN documents d ON d.id=p.doc_id "
        "WHERE p.doc_id=?",
        (int(doc_id),),
    ).fetchone()
    if row is None:
        return None
    if (
        not row["is_deleted"]
        or row["file_state"] != "pending"
        or row["document_path"] != row["path"]
        or row["document_path_norm"] != row["path_norm"]
        or int(row["document_version"]) != int(row["version"])
    ):
        raise RuntimeError("purge intent and tombstone generation do not agree")
    return PurgeIntentSnapshot(
        doc_id=int(row["doc_id"]),
        path=str(row["path"]),
        path_norm=str(row["path_norm"]),
        version=int(row["version"]),
        file_state=str(row["file_state"]),
        actor=str(row["actor"]),
        via=str(row["via"]),
    )

def _process_purge_cleanup_batch(
    svc,
    conn: sqlite3.Connection,
    intent: PurgeIntentSnapshot,
    *,
    after_norm: str,
    batch_size: int = 64,
) -> tuple[str | None, tuple[CleanupIssue, ...]]:
    """Discharge one purge cleanup page; only actual I/O errors remain durable."""
    rows = conn.execute(
        "SELECT path,path_norm,expected_exists,expected_dev,expected_ino,"
        "expected_size,expected_mtime_ns,expected_ctime_ns "
        "FROM file_projection_cleanup WHERE doc_id=? AND path_norm>? "
        "ORDER BY path_norm LIMIT ?",
        (intent.doc_id, after_norm, clamp_int(batch_size, 1, 64)),
    ).fetchall()
    if not rows:
        return None, ()

    issues: list[CleanupIssue] = []
    for row in rows:
        rel = str(row["path"])
        norm = str(row["path_norm"])

        # The current tombstone owns this normalized namespace, but its canonical
        # file is in .trash. A stale live cleanup row for the same path can be
        # retired without touching either namespace.
        if norm == intent.path_norm:
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (intent.doc_id, norm),
            )
            continue

        owner = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? LIMIT 1", (norm,)
        ).fetchone()
        if owner is not None:
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (intent.doc_id, norm),
            )
            continue

        try:
            target = fp.managed_path(svc.vault, rel, namespace="live")
            current = fp.confined_file_signature(svc.vault, target, missing_ok=True)
            if current is None:
                if not fp.confirm_confined_absence(svc.vault, target):
                    # A new generation appeared. Purge preserves it and retires
                    # the old document's cleanup authority.
                    pass
            else:
                expected = _expected_cleanup_signature(row)
                if expected is not None and current == expected:
                    # False means it changed/disappeared between stat and unlink;
                    # either way the new/external generation is preserved.
                    removed = fp.unlink_regular(target, expected=expected, vault=svc.vault)
                    if not removed:
                        after = fp.confined_file_signature(svc.vault, target, missing_ok=True)
                        if after is None:
                            fp.confirm_confined_absence(svc.vault, target)
            conn.execute(
                "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                (intent.doc_id, norm),
            )
        except (OSError, fp.FileProjectionError) as exc:
            issues.append(
                CleanupIssue(
                    rel,
                    "purge_cleanup_io_error",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return str(rows[-1]["path_norm"]), tuple(issues)

def _finish_purge(svc, doc_id: int) -> fp.ProjectionResult:
    """Finish one immutable purge request, idempotently and audit-exactly-once."""
    cursor = ""
    issues: list[CleanupIssue] = []
    last_intent: PurgeIntentSnapshot | None = None
    while True:
        batch_cursor: str | None = None
        batch_issues: tuple[CleanupIssue, ...] = ()
        with svc.db.writer() as conn:
            intent = _purge_intent_snapshot(conn, int(doc_id))
            if intent is None:
                document = conn.execute(
                    "SELECT path FROM documents WHERE id=?", (int(doc_id),)
                ).fetchone()
                return fp.ProjectionResult(
                    int(doc_id),
                    str(document["path"]) if document is not None else None,
                    document is None,
                    False,
                    "missing" if document is None else "purge_intent_missing",
                    1,
                    True,
                )
            last_intent = intent
            batch_cursor, batch_issues = svc._process_purge_cleanup_batch(
                conn, intent, after_norm=cursor
            )
        issues.extend(batch_issues)
        if batch_cursor is None:
            break
        cursor = batch_cursor

    assert last_intent is not None
    if issues:
        with svc.db.reader() as conn:
            intent_exists = (
                conn.execute(
                    "SELECT 1 FROM document_purge_intents WHERE doc_id=?",
                    (last_intent.doc_id,),
                ).fetchone()
                is not None
            )
            document_exists = (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id=?", (last_intent.doc_id,)
                ).fetchone()
                is not None
            )
            cleanup_remains = (
                conn.execute(
                    "SELECT 1 FROM file_projection_cleanup WHERE doc_id=? LIMIT 1",
                    (last_intent.doc_id,),
                ).fetchone()
                is not None
            )
        if not intent_exists:
            return fp.ProjectionResult(
                last_intent.doc_id,
                last_intent.path,
                not document_exists,
                False,
                "missing" if not document_exists else "purge_intent_missing",
                1,
                True,
            )
        if cleanup_remains:
            sample = ", ".join(issue.path for issue in issues[:3])
            return fp.ProjectionResult(
                last_intent.doc_id,
                last_intent.path,
                False,
                False,
                "purge_cleanup_io_error",
                1,
                True,
                f"{len(issues)} purge cleanup path(s) failed"
                + (f": {sample}" if sample else ""),
            )

    try:
        with svc.db.writer() as conn:
            intent = _purge_intent_snapshot(conn, int(doc_id))
            if intent is None:
                document = conn.execute(
                    "SELECT path FROM documents WHERE id=?", (int(doc_id),)
                ).fetchone()
                return fp.ProjectionResult(
                    int(doc_id),
                    str(document["path"]) if document is not None else None,
                    document is None,
                    False,
                    "missing" if document is None else "purge_intent_missing",
                    1,
                    True,
                )
            if (
                conn.execute(
                    "SELECT 1 FROM file_projection_cleanup WHERE doc_id=? LIMIT 1",
                    (intent.doc_id,),
                ).fetchone()
                is not None
            ):
                return fp.ProjectionResult(
                    intent.doc_id,
                    intent.path,
                    False,
                    False,
                    "purge_cleanup_pending",
                    1,
                    True,
                )

            trash = fp.managed_path(svc.vault, intent.path, namespace="trash")
            trash_signature = fp.confined_file_signature(svc.vault, trash, missing_ok=True)
            if trash_signature is not None:
                if not fp.unlink_regular(trash, expected=trash_signature, vault=svc.vault):
                    raise fp.FileGenerationChanged(
                        f"purge trash changed during removal: {trash}"
                    )
            elif not fp.confirm_confined_absence(svc.vault, trash):
                raise fp.FileGenerationChanged(f"purge trash changed during removal: {trash}")

            graph.unresolve_incoming(conn, intent.doc_id)
            deleted = conn.execute(
                "DELETE FROM documents WHERE id=? AND path=? AND path_norm=? "
                "AND version=? AND is_deleted=1 AND file_state='pending' "
                "AND EXISTS(SELECT 1 FROM document_purge_intents p "
                "WHERE p.doc_id=? AND p.path=? AND p.path_norm=? AND p.version=?) "
                "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                "WHERE c.doc_id=?)",
                (
                    intent.doc_id,
                    intent.path,
                    intent.path_norm,
                    intent.version,
                    intent.doc_id,
                    intent.path,
                    intent.path_norm,
                    intent.version,
                    intent.doc_id,
                ),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("purge tombstone fence changed before deletion")
            audit.record(
                conn,
                actor=intent.actor,
                via=intent.via,
                action="doc_purge",
                target=intent.path,
            )
        return fp.ProjectionResult(
            intent.doc_id,
            intent.path,
            True,
            True,
            None,
            1,
            True,
        )
    except (OSError, fp.FileProjectionError) as exc:
        return fp.ProjectionResult(
            last_intent.doc_id,
            last_intent.path,
            False,
            False,
            "purge_io_error",
            1,
            True,
            f"{type(exc).__name__}: {exc}",
        )

def _project_current(svc, doc_id: int, *, max_attempts: int = 3) -> fp.ProjectionResult:
    """Install only the latest exact revision, fenced by a final writer token.

    Staging is intentionally outside the SQLite writer. Publication, removal of
    the opposite live/trash copy, and the exact ``pending -> clean`` transition
    happen while the writer lock prevents another DB generation from committing.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    last_path: str | None = None
    last_deleted: bool | None = None
    for attempt in range(1, max_attempts + 1):
        with svc.db.reader() as conn:
            snapshot = svc._projection_snapshot(conn, int(doc_id))
        if snapshot is None:
            return fp.ProjectionResult(int(doc_id), None, True, False, "missing", attempt)

        last_path = snapshot.path
        last_deleted = snapshot.is_deleted
        if snapshot.has_purge_intent:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "purge_pending",
                attempt,
                snapshot.is_deleted,
            )
        if snapshot.file_state == "clean" and not snapshot.has_cleanup_intent:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                True,
                False,
                "already_settled",
                attempt,
                snapshot.is_deleted,
            )
        if (
            snapshot.file_state not in ("clean", "pending")
            or snapshot.revision_version != snapshot.version
            or snapshot.revision_content_hash != snapshot.content_hash
            or snapshot.body is None
            or sha256_hex(snapshot.body) != snapshot.content_hash
        ):
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "projection_corrupt",
                attempt,
                snapshot.is_deleted,
                "The current document row and exact revision do not agree.",
            )
        canonical_body = snapshot.body
        assert canonical_body is not None

        if snapshot.file_state == "clean":
            # Older/legacy call sites can temporarily leave cleanup authority on
            # a row they marked clean. Re-open that exact generation as pending so
            # recovery can discharge the durable intents instead of looping on
            # cleanup_pending forever.
            with svc.db.writer() as conn:
                conn.execute(
                    "UPDATE documents SET file_state='pending' "
                    "WHERE id=? AND path=? AND path_norm=? AND version=? "
                    "AND content_hash=? AND is_deleted=? AND file_state='clean' "
                    "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                    "WHERE p.doc_id=?) "
                    "AND EXISTS(SELECT 1 FROM file_projection_cleanup c "
                    "WHERE c.doc_id=?) "
                    "AND EXISTS(SELECT 1 FROM revisions r WHERE r.doc_id=? "
                    "AND r.version=? AND r.content_hash=?)",
                    (
                        snapshot.doc_id,
                        snapshot.path,
                        snapshot.path_norm,
                        snapshot.version,
                        snapshot.content_hash,
                        int(snapshot.is_deleted),
                        snapshot.doc_id,
                        snapshot.doc_id,
                        snapshot.doc_id,
                        snapshot.version,
                        snapshot.content_hash,
                    ),
                )
            continue

        current_installed = False
        live_target: Path
        trash_target: Path
        target: Path
        try:
            live_target = fp.managed_path(svc.vault, snapshot.path, namespace="live")
            trash_target = fp.managed_path(svc.vault, snapshot.path, namespace="trash")
            target = trash_target if snapshot.is_deleted else live_target
        except (OSError, fp.FileProjectionError) as exc:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "io_error",
                attempt,
                snapshot.is_deleted,
                f"{type(exc).__name__}: {exc}",
            )

        # Publish the canonical current target before removing any historical
        # paths. If cleanup spans transactions, this leaves a usable latest file
        # while the DB remains explicitly pending.
        retry_snapshot = False
        cleanup_required = False
        immediate_result: fp.ProjectionResult | None = None
        try:
            staged = fp.stage_text(svc.vault, target, canonical_body)
        except (OSError, fp.FileProjectionError) as exc:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "io_error",
                attempt,
                snapshot.is_deleted,
                f"{type(exc).__name__}: {exc}",
                current_installed,
            )
        try:
            with svc.db.writer() as conn:
                token_state = svc._projection_token_state(conn, snapshot, allow_cleanup=True)
                if token_state == "changed":
                    retry_snapshot = True
                elif token_state == "missing":
                    immediate_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "missing",
                        attempt,
                        snapshot.is_deleted,
                    )
                elif token_state == "settled":
                    immediate_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "already_settled",
                        attempt,
                        snapshot.is_deleted,
                    )
                elif token_state in ("purge_pending", "cleanup_pending"):
                    immediate_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        False,
                        False,
                        token_state,
                        attempt,
                        snapshot.is_deleted,
                    )
                else:
                    file_mtime = svc._install_projection_target(
                        snapshot,
                        staged,
                        target,
                        live_target,
                        trash_target,
                    )
                    current_installed = True
                    if token_state == "current":
                        svc._mark_projection_clean(conn, snapshot, file_mtime)
                    else:
                        cleanup_required = True
        except (OSError, fp.FileProjectionError) as exc:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "io_error",
                attempt,
                snapshot.is_deleted,
                f"{type(exc).__name__}: {exc}",
                current_installed,
            )
        finally:
            try:
                fp.cleanup_staged(staged)
            except (OSError, fp.FileProjectionError) as exc:
                log.warning(
                    "Could not clean staged projection for document %d: %s",
                    snapshot.doc_id,
                    exc,
                )

        if retry_snapshot:
            continue
        if immediate_result is not None:
            return immediate_result
        if not cleanup_required:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                True,
                True,
                None,
                attempt,
                snapshot.is_deleted,
                current_installed=current_installed,
            )

        # Visit every cleanup row once in path_norm order. A conflict advances
        # the cursor and remains durable for the next recovery instead of
        # starving later batches.
        cursor = ""
        cleanup_issues: list[CleanupIssue] = []
        terminal_result: fp.ProjectionResult | None = None
        while True:
            batch_cursor: str | None = None
            batch_issues: tuple[CleanupIssue, ...] = ()
            with svc.db.writer() as conn:
                token_state = svc._projection_token_state(conn, snapshot, allow_cleanup=True)
                if token_state == "changed":
                    retry_snapshot = True
                elif token_state == "missing":
                    terminal_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "missing",
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                elif token_state == "settled":
                    terminal_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "already_settled",
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                elif token_state in ("purge_pending", "cleanup_pending"):
                    terminal_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        False,
                        False,
                        token_state,
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                elif token_state == "current_cleanup":
                    batch_cursor, batch_issues = svc._process_cleanup_batch(
                        conn, snapshot, after_norm=cursor
                    )

            cleanup_issues.extend(batch_issues)
            if retry_snapshot or terminal_result is not None:
                break
            if batch_cursor is None:
                break
            cursor = batch_cursor

        if retry_snapshot:
            continue
        if terminal_result is not None:
            return terminal_result
        if cleanup_issues:
            reason = (
                "cleanup_io_error"
                if any(issue.reason == "cleanup_io_error" for issue in cleanup_issues)
                else "cleanup_changed"
            )
            sample = ", ".join(issue.path for issue in cleanup_issues[:3])
            detail = f"{len(cleanup_issues)} cleanup path(s) remain unresolved" + (
                f": {sample}" if sample else ""
            )
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                reason,
                attempt,
                snapshot.is_deleted,
                detail,
                current_installed,
            )

        # Cleanup crossed at least one writer boundary. Re-stage and publish the
        # canonical target in the same final writer transaction as exact clean,
        # fencing external edits made while historical paths were processed.
        final_result: fp.ProjectionResult | None = None
        try:
            final_staged = fp.stage_text(svc.vault, target, canonical_body)
        except (OSError, fp.FileProjectionError) as exc:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "io_error",
                attempt,
                snapshot.is_deleted,
                f"{type(exc).__name__}: {exc}",
                current_installed,
            )
        try:
            with svc.db.writer() as conn:
                token_state = svc._projection_token_state(conn, snapshot)
                if token_state == "changed":
                    retry_snapshot = True
                elif token_state == "missing":
                    final_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "missing",
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                elif token_state == "settled":
                    final_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        True,
                        False,
                        "already_settled",
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                elif token_state in ("purge_pending", "cleanup_pending"):
                    final_result = fp.ProjectionResult(
                        snapshot.doc_id,
                        snapshot.path,
                        False,
                        False,
                        token_state,
                        attempt,
                        snapshot.is_deleted,
                        current_installed=current_installed,
                    )
                else:
                    file_mtime = svc._install_projection_target(
                        snapshot,
                        final_staged,
                        target,
                        live_target,
                        trash_target,
                    )
                    current_installed = True
                    svc._mark_projection_clean(conn, snapshot, file_mtime)
        except (OSError, fp.FileProjectionError) as exc:
            return fp.ProjectionResult(
                snapshot.doc_id,
                snapshot.path,
                False,
                False,
                "io_error",
                attempt,
                snapshot.is_deleted,
                f"{type(exc).__name__}: {exc}",
                current_installed,
            )
        finally:
            try:
                fp.cleanup_staged(final_staged)
            except (OSError, fp.FileProjectionError) as exc:
                log.warning(
                    "Could not clean final staged projection for document %d: %s",
                    snapshot.doc_id,
                    exc,
                )

        if retry_snapshot:
            continue
        if final_result is not None:
            return final_result
        return fp.ProjectionResult(
            snapshot.doc_id,
            snapshot.path,
            True,
            True,
            None,
            attempt,
            snapshot.is_deleted,
            current_installed=current_installed,
        )

    return fp.ProjectionResult(
        int(doc_id),
        last_path,
        False,
        False,
        "target_changed",
        max_attempts,
        last_deleted,
    )

def _require_projection(svc, doc_id: int) -> fp.ProjectionResult:
    result = svc._project_current(doc_id)
    if not result.settled:
        with svc.db.reader() as conn:
            row = conn.execute("SELECT version FROM documents WHERE id=?", (doc_id,)).fetchone()
        raise ProjectionPendingError(result, version=int(row["version"]) if row else None)
    return result

def _recover_pending_report(svc, *, page_size: int = 64) -> RecoveryReport:
    """Visit a bounded ID frontier and continue after per-document failures."""
    page_size = clamp_int(page_size, 1, 1024)
    pending_where = (
        "d.file_state='pending' "
        "OR EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
        "OR EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id)"
    )
    with svc.db.reader() as conn:
        max_id = int(
            conn.execute(
                f"SELECT COALESCE(MAX(d.id),0) FROM documents d WHERE {pending_where}"
            ).fetchone()[0]
        )
    cursor = 0
    recovered = 0
    issues: list[fp.ProjectionResult] = []
    while cursor < max_id:
        with svc.db.reader() as conn:
            rows = conn.execute(
                f"SELECT d.id FROM documents d WHERE d.id>? AND d.id<=? "
                f"AND ({pending_where}) ORDER BY d.id LIMIT ?",
                (cursor, max_id, page_size),
            ).fetchall()
        if not rows:
            break
        ids = [int(row["id"]) for row in rows]
        for current_id in ids:
            try:
                with svc.db.reader() as conn:
                    has_purge_intent = (
                        conn.execute(
                            "SELECT 1 FROM document_purge_intents WHERE doc_id=?",
                            (current_id,),
                        ).fetchone()
                        is not None
                    )
                result = (
                    svc._finish_purge(current_id)
                    if has_purge_intent
                    else svc._project_current(current_id)
                )
                if result.reason == "purge_pending":
                    result = svc._finish_purge(current_id)
            except Exception as exc:
                log.exception("recover_pending: document %d raised unexpectedly", current_id)
                error_path = None
                try:
                    with svc.db.reader() as conn:
                        row = conn.execute(
                            "SELECT path FROM documents WHERE id=?", (current_id,)
                        ).fetchone()
                    if row is not None:
                        error_path = str(row["path"])
                except Exception:
                    pass
                result = fp.ProjectionResult(
                    current_id,
                    error_path,
                    False,
                    False,
                    "recovery_error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            if result.transitioned:
                recovered += 1
            if not result.settled:
                issues.append(result)
        cursor = ids[-1]
    return RecoveryReport(recovered, tuple(issues))

