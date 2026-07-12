"""Reindex / pending-recovery maintenance for DocumentService.

Extracted from documents.py so DocumentService stays a thin coordinator.
Public entry points remain DocumentService.reindex_all / recover_pending /
embed_pending (each delegates here).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .. import file_projection as fp
from .. import graph, indexing
from ..markdown_utils import derive_title, parse_frontmatter
from ..util import (
    PathError,
    basename_stem,
    folder_of,
    normalize_rel_path,
    now_iso,
    path_norm,
    sha256_hex,
)
from . import audit

if TYPE_CHECKING:
    from .documents import DocumentService, ReindexTargetSnapshot

log = logging.getLogger("llm_wiki.documents")


class _ReindexRetry(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def reindex_snapshot_from_row(row: sqlite3.Row) -> ReindexTargetSnapshot:
    from .documents import ReindexTargetSnapshot

    return ReindexTargetSnapshot(
        doc_id=int(row["id"]),
        path=str(row["path"]),
        path_norm=str(row["path_norm"]),
        version=int(row["version"]),
        content_hash=str(row["content_hash"]),
        is_deleted=bool(row["is_deleted"]),
        file_state=str(row["file_state"]),
        has_purge_intent=bool(row["has_purge_intent"]),
        has_cleanup_intent=bool(row["has_cleanup_intent"]),
    )


def reindex_target_snapshot(conn: sqlite3.Connection, norm: str) -> ReindexTargetSnapshot | None:
    row = conn.execute(
        "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,d.is_deleted,"
        "d.file_state,"
        "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
        "AS has_purge_intent,"
        "EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id) "
        "AS has_cleanup_intent FROM documents d WHERE d.path_norm=?",
        (norm,),
    ).fetchone()
    if row is None:
        return None
    return reindex_snapshot_from_row(row)


def reindex_document_snapshot(
    conn: sqlite3.Connection, doc_id: int
) -> ReindexTargetSnapshot | None:
    row = conn.execute(
        "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,d.is_deleted,"
        "d.file_state,"
        "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
        "AS has_purge_intent,"
        "EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id) "
        "AS has_cleanup_intent FROM documents d WHERE d.id=?",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    return reindex_snapshot_from_row(row)


def recover_pending(svc: DocumentService) -> int:
    """Finish a bounded frontier of pending projections and report issues in logs."""
    report = svc._recover_pending_report()
    if report.recovered:
        log.info("recover_pending: settled %d document projection(s)", report.recovered)
    for issue in report.issues:
        log.warning(
            "recover_pending: document %d remains pending (%s)%s",
            issue.doc_id,
            issue.reason or "unknown",
            f": {issue.detail}" if issue.detail else "",
        )
    return report.recovered


def embed_pending(svc: DocumentService) -> int:
    """Embed any documents still flagged ``vector_dirty`` (no-op when none are).
    A crash can commit a write — version bumped, ``vector_dirty=1`` — but die before
    the post-commit embed (it runs off the write lock), leaving the doc absent from
    vector search until the next ``reindex --reembed``. Sweeping on startup closes
    that gap. Also catches docs left ``file_state='clean'`` but unembedded, which
    ``recover_pending`` (file-state only) does not see."""
    return indexing.embed_pending(svc.db, svc.embedder)


def reindex_all(
    svc: DocumentService, reembed: bool = False, progress: Callable[[int, int], None] | None = None
) -> dict:
    """Adopt only stable external generations behind exact DB and file fences."""
    from . import documents as dm

    vault = svc.vault.resolve(strict=True)
    log.info("reindex: scanning vault %s (reembed=%s)", vault, reembed)

    recovered_pending = 0

    def recover_one(target_id: int) -> fp.ProjectionResult:
        nonlocal recovered_pending
        try:
            with svc.db.reader() as conn:
                purge_requested = (
                    conn.execute(
                        "SELECT 1 FROM document_purge_intents WHERE doc_id=?",
                        (target_id,),
                    ).fetchone()
                    is not None
                )
            result = (
                svc._finish_purge(target_id) if purge_requested else svc._project_current(target_id)
            )
        except Exception as exc:
            log.exception("reindex: projection recovery failed for document %d", target_id)
            error_path = None
            with suppress(Exception):
                with svc.db.reader() as conn:
                    error_path = conn.execute(
                        "SELECT (SELECT path FROM documents WHERE id=?)",
                        (target_id,),
                    ).fetchone()[0]
            return fp.ProjectionResult(
                target_id,
                error_path,
                False,
                False,
                "recovery_error",
                1,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if result.transitioned:
            recovered_pending += 1
        return result

    initial_recovery = svc._recover_pending_report()
    recovered_pending += initial_recovery.recovered

    grouped: dict[str, list[tuple[str, Path]]] = {}
    scan_retry_entries: list[tuple[str, str, Path, str]] = []
    invalid_scan_conflicts: dict[str, dict] = {}
    for path in sorted(vault.rglob("*.md"), key=lambda item: item.as_posix()):
        relative = path.relative_to(vault)
        if relative.parts[0].casefold() in (".trash", ".tmp", dm.ATTACH_DIR, dm.TEMPLATES_DIR):
            continue
        rel = relative.as_posix()
        try:
            canonical_rel = normalize_rel_path(rel)
        except PathError:
            canonical_rel = None
        if canonical_rel != rel:
            diagnostic_path = repr(rel)
            invalid_scan_conflicts[diagnostic_path] = {
                "path": diagnostic_path,
                "reason": "file_unreadable",
                "attempts": 1,
            }
            continue
        try:
            fp.confined_file_signature(vault, path)
        except FileNotFoundError:
            scan_retry_entries.append((path_norm(rel), rel, path, "file_disappeared"))
            continue
        except (OSError, fp.FileProjectionError):
            scan_retry_entries.append((path_norm(rel), rel, path, "file_unreadable"))
            continue
        grouped.setdefault(path_norm(rel), []).append((rel, path))
    conflicts: dict[str, dict] = dict(invalid_scan_conflicts)
    process_paths: list[tuple[str, str, Path]] = []
    for norm, entries in sorted(grouped.items()):
        if len(entries) > 1:
            for rel, _path in entries:
                conflicts[rel] = {
                    "path": rel,
                    "reason": "path_collision",
                    "attempts": 1,
                }
            continue
        rel, path = entries[0]
        process_paths.append((norm, rel, path))
    failed_norm_counts: dict[str, int] = {}
    for failed_norm, _rel, _path, _reason in scan_retry_entries:
        failed_norm_counts[failed_norm] = failed_norm_counts.get(failed_norm, 0) + 1
    for failed_norm, rel, path, reason in scan_retry_entries:
        if failed_norm in grouped or failed_norm_counts[failed_norm] > 1:
            conflicts[rel] = {
                "path": rel,
                "reason": reason,
                "attempts": 1,
            }
        else:
            process_paths.append((failed_norm, rel, path))
    process_paths.sort(key=lambda entry: (entry[0], entry[1]))

    counts = {"created": 0, "updated": 0, "renamed": 0, "unchanged": 0}
    skipped_deleted: list[str] = []
    renames: list[str] = []
    classified_outcomes: dict[str, tuple[str, str | None]] = {}
    retried = 0

    def record_outcome(outcome: str, rel: str, renamed_from: str | None = None) -> None:
        previous = classified_outcomes.get(rel, ("skipped_deleted", None))
        candidate = (outcome, renamed_from)
        classified_outcomes[rel] = max(
            (candidate, previous), key=lambda item: outcome_priority[item[0]]
        )
        conflicts.pop(rel, None)

    def record_committed_best(best: tuple[str, str | None] | None, rel: str) -> None:
        if best is not None:
            record_outcome(best[0], rel, best[1])

    outcome_priority = {
        "skipped_deleted": 0,
        "unchanged": 1,
        "updated": 2,
        "created": 3,
        "renamed": 4,
    }

    pending_paths = {rel for _norm, rel, _path in process_paths}
    source_requeues: dict[str, int] = {}
    max_source_requeues = 3
    pending_dependency_paths: set[str] = set()
    transient_source_conflicts: set[str] = set()

    def inspect_source_absence(snapshot: ReindexTargetSnapshot, attempt: int) -> bool | None:
        try:
            source_target = fp.managed_path(vault, snapshot.path, namespace="live")
            source_absent = fp.confirm_confined_absence(vault, source_target)
        except (OSError, fp.FileProjectionError):
            if snapshot.path not in conflicts:
                conflicts[snapshot.path] = {
                    "path": snapshot.path,
                    "reason": "file_unreadable",
                    "attempts": attempt,
                }
                transient_source_conflicts.add(snapshot.path)
            return None
        if snapshot.path in transient_source_conflicts:
            transient_source_conflicts.discard(snapshot.path)
            conflicts.pop(snapshot.path, None)
        return source_absent

    superseded_paths: set[str] = set()
    process_index = 0
    while process_index < len(process_paths):
        norm, rel, path = process_paths[process_index]
        process_index += 1
        pending_paths.discard(rel)
        committed_best: tuple[str, str | None] | None = None
        cleanup_removed = False
        managed_superseded = rel in superseded_paths
        source_identity_changed = False

        attempt = 0
        while True:
            attempt += 1
            with svc.db.reader() as conn:
                target_snapshot = reindex_target_snapshot(conn, norm)
            if target_snapshot is not None and (
                target_snapshot.file_state == "pending"
                or target_snapshot.has_purge_intent
                or target_snapshot.has_cleanup_intent
            ):
                had_target = True
                recovered = recover_one(target_snapshot.doc_id)
                if not recovered.settled:
                    record_committed_best(committed_best, rel)
                    issue_path = recovered.path or rel
                    conflicts[issue_path] = {
                        "path": issue_path,
                        "reason": "pending_projection",
                        "attempts": attempt,
                    }
                    break
                with svc.db.reader() as conn:
                    target_snapshot = reindex_target_snapshot(conn, norm)
                if had_target and target_snapshot is None:
                    managed_superseded = True

            try:
                stable = fp.read_stable_markdown(vault, path)
            except fp.StableFileError as exc:
                if exc.reason == "file_disappeared":
                    with svc.db.reader() as conn:
                        latest = reindex_target_snapshot(conn, norm)
                    if (
                        managed_superseded
                        or cleanup_removed
                        or (target_snapshot is not None and (latest is None or latest.is_deleted))
                    ):
                        record_committed_best(committed_best, rel)
                        break
                if attempt < 3:
                    retried += 1
                    continue
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": exc.reason,
                    "attempts": attempt,
                }
                break

            content = stable.text
            chash = sha256_hex(content)
            meta = parse_frontmatter(content)[0]
            title = derive_title(meta, content, rel)
            tagset = svc._merge_tags(meta, content, None)
            folder = folder_of(rel)
            stem = basename_stem(rel).lower()
            prepared = indexing.prepare_markdown(content)
            mtime = stable.signature.mtime_ns / 1_000_000_000
            outcome: str | None = None
            renamed_from: str | None = None
            cleanup_owner_ids: tuple[int, ...] = ()
            affected_owner_ids: tuple[int, ...] = ()
            source_peer_states: tuple[tuple[ReindexTargetSnapshot, bool | None], ...] = ()
            ignored_source_owner_ids: tuple[int, ...] = ()
            pending_source_ids_to_recover: tuple[int, ...] = ()
            source_decision_made = False

            try:
                with svc.db.writer() as conn:
                    current = reindex_target_snapshot(conn, norm)
                    if current != target_snapshot:
                        raise _ReindexRetry("target_changed")
                    if not fp.stable_markdown_is_current(stable):
                        raise _ReindexRetry("file_changed")

                    if current is not None and current.is_deleted:
                        audit.record(
                            conn,
                            actor=None,
                            via="cli",
                            action="doc_reconcile_skip",
                            target=rel,
                            outcome="skipped",
                            detail="deleted document still present on disk",
                        )
                        outcome = "skipped_deleted"
                    elif current is not None:
                        doc_id = current.doc_id
                        owner_rows = conn.execute(
                            "SELECT DISTINCT doc_id FROM file_projection_cleanup "
                            "WHERE path_norm=? AND doc_id<>?",
                            (norm, current.doc_id),
                        ).fetchall()
                        affected_owner_ids = tuple(int(row["doc_id"]) for row in owner_rows)
                        exact_spelling = current.path == rel
                        if current.content_hash == chash and not reembed and exact_spelling:
                            conn.execute(
                                "UPDATE documents SET file_mtime=? "
                                "WHERE id=? AND path=? AND path_norm=? AND version=? "
                                "AND content_hash=? AND is_deleted=0 AND file_state='clean' "
                                "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                                "WHERE p.doc_id=?) "
                                "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                "WHERE c.doc_id=?)",
                                (
                                    mtime,
                                    current.doc_id,
                                    current.path,
                                    current.path_norm,
                                    current.version,
                                    current.content_hash,
                                    current.doc_id,
                                    current.doc_id,
                                ),
                            )
                            outcome = "unchanged"
                        else:
                            if not exact_spelling:
                                old_target = fp.managed_path(vault, current.path, namespace="live")
                                if not fp.confirm_confined_absence(vault, old_target):
                                    raise _ReindexRetry("rename_source_reappeared")
                                graph.unresolve_incoming(conn, current.doc_id)
                                renamed_from = current.path
                            new_version = current.version + 1
                            conn.execute(
                                "UPDATE documents SET path=?,path_norm=?,title=?,"
                                "version=?,content_hash=?,folder=?,file_state='clean',"
                                "vector_dirty=1,file_mtime=?,updated_at=?,updated_by=NULL "
                                "WHERE id=? AND path=? AND path_norm=? AND version=? "
                                "AND content_hash=? AND is_deleted=0 AND file_state='clean' "
                                "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                                "WHERE p.doc_id=?) "
                                "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                "WHERE c.doc_id=?)",
                                (
                                    rel,
                                    norm,
                                    title,
                                    new_version,
                                    chash,
                                    folder,
                                    mtime,
                                    now_iso(),
                                    current.doc_id,
                                    current.path,
                                    current.path_norm,
                                    current.version,
                                    current.content_hash,
                                    current.doc_id,
                                    current.doc_id,
                                ),
                            )
                            conn.execute(
                                "INSERT INTO revisions(doc_id,version,body,title,content_hash,"
                                "author_id,op,via,created_at) VALUES(?,?,?,?,?,NULL,?,'cli',?)",
                                (
                                    current.doc_id,
                                    new_version,
                                    content,
                                    title,
                                    chash,
                                    "rename" if renamed_from else "external-reconcile",
                                    now_iso(),
                                ),
                            )
                            svc._set_tags(conn, current.doc_id, tagset)
                            indexing.publish_prepared(conn, current.doc_id, title, folder, prepared)
                            graph.backfill_links_for(conn, current.doc_id, norm, stem)
                            audit.record(
                                conn,
                                actor=None,
                                via="cli",
                                action="doc_reconcile",
                                target=rel,
                                detail=(
                                    f"v{new_version} rename {renamed_from} -> {rel}"
                                    if renamed_from
                                    else f"v{new_version} update"
                                ),
                            )
                            outcome = "renamed" if renamed_from else "updated"
                        if affected_owner_ids:
                            conn.execute(
                                "DELETE FROM file_projection_cleanup "
                                "WHERE path_norm=? AND doc_id<>?",
                                (norm, current.doc_id),
                            )
                    else:
                        cleanup_rows = conn.execute(
                            "SELECT doc_id,expected_exists,expected_dev,expected_ino,"
                            "expected_size,expected_mtime_ns,expected_ctime_ns "
                            "FROM file_projection_cleanup WHERE path_norm=? "
                            "ORDER BY doc_id",
                            (norm,),
                        ).fetchall()
                        exact_cleanup_ids = []
                        for cleanup in cleanup_rows:
                            expected = svc._expected_cleanup_signature(cleanup)
                            if expected is not None and expected == stable.signature:
                                exact_cleanup_ids.append(int(cleanup["doc_id"]))
                        if exact_cleanup_ids:
                            cleanup_owner_ids = tuple(exact_cleanup_ids)
                        else:
                            affected_owner_ids = tuple(
                                int(cleanup["doc_id"]) for cleanup in cleanup_rows
                            )
                            ignored_source_owner_ids = affected_owner_ids
                            pending_rows = conn.execute(
                                "SELECT d.path_norm FROM documents d "
                                "WHERE d.is_deleted=0 AND d.content_hash=? "
                                "AND d.path_norm<>? AND (d.file_state<>'clean' "
                                "OR EXISTS(SELECT 1 FROM document_purge_intents p "
                                "WHERE p.doc_id=d.id) "
                                "OR EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                "WHERE c.doc_id=d.id)) ORDER BY d.id",
                                (chash, norm),
                            ).fetchall()
                            pending_snapshots = [
                                snapshot
                                for pending_row in pending_rows
                                if (
                                    snapshot := reindex_target_snapshot(
                                        conn, str(pending_row["path_norm"])
                                    )
                                )
                                is not None
                                and snapshot.doc_id not in affected_owner_ids
                            ]
                            pending_first_states = tuple(
                                (
                                    snapshot,
                                    inspect_source_absence(snapshot, attempt),
                                )
                                for snapshot in pending_snapshots
                            )
                            pending_second_states = tuple(
                                (
                                    snapshot,
                                    inspect_source_absence(snapshot, attempt),
                                )
                                for snapshot in pending_snapshots
                            )
                            if pending_second_states != pending_first_states:
                                raise _ReindexRetry("rename_source_changed")
                            if any(
                                source_absent is True
                                for _snapshot, source_absent in pending_second_states
                            ):
                                pending_source_ids_to_recover = tuple(
                                    snapshot.doc_id
                                    for snapshot, source_absent in pending_second_states
                                    if source_absent is True
                                )
                                pending_dependency_paths.add(rel)
                                raise _ReindexRetry("pending_projection")
                            candidate_rows = conn.execute(
                                "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,"
                                "d.is_deleted,d.file_state,"
                                "EXISTS(SELECT 1 FROM document_purge_intents p "
                                "WHERE p.doc_id=d.id) AS has_purge_intent,"
                                "EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                "WHERE c.doc_id=d.id) AS has_cleanup_intent "
                                "FROM documents d "
                                "WHERE d.is_deleted=0 AND d.file_state='clean' "
                                "AND d.content_hash=? AND d.path_norm<>? "
                                "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                                "WHERE p.doc_id=d.id) "
                                "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                "WHERE c.doc_id=d.id) ORDER BY d.id",
                                (chash, norm),
                            ).fetchall()
                            candidate_snapshots = [
                                reindex_snapshot_from_row(candidate) for candidate in candidate_rows
                            ]
                            first_states = tuple(
                                (
                                    candidate_snapshot,
                                    inspect_source_absence(candidate_snapshot, attempt),
                                )
                                for candidate_snapshot in candidate_snapshots
                            )
                            second_states = tuple(
                                (
                                    candidate_snapshot,
                                    inspect_source_absence(candidate_snapshot, attempt),
                                )
                                for candidate_snapshot in candidate_snapshots
                            )
                            if second_states != first_states:
                                raise _ReindexRetry("rename_source_changed")
                            source_peer_states = (
                                *pending_second_states,
                                *second_states,
                            )
                            source_decision_made = True
                            absent_sources = [
                                candidate_snapshot
                                for candidate_snapshot, source_absent in second_states
                                if source_absent is True
                            ]
                            source = absent_sources[0] if len(absent_sources) == 1 else None
                            if source is not None:
                                new_version = source.version + 1
                                conn.execute(
                                    "UPDATE documents SET path=?,path_norm=?,title=?,"
                                    "version=?,content_hash=?,folder=?,file_state='clean',"
                                    "vector_dirty=1,file_mtime=?,updated_at=?,updated_by=NULL "
                                    "WHERE id=? AND path=? AND path_norm=? AND version=? "
                                    "AND content_hash=? AND is_deleted=0 AND file_state='clean' "
                                    "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                                    "WHERE p.doc_id=?) "
                                    "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                                    "WHERE c.doc_id=?)",
                                    (
                                        rel,
                                        norm,
                                        title,
                                        new_version,
                                        chash,
                                        folder,
                                        mtime,
                                        now_iso(),
                                        source.doc_id,
                                        source.path,
                                        source.path_norm,
                                        source.version,
                                        source.content_hash,
                                        source.doc_id,
                                        source.doc_id,
                                    ),
                                )
                                doc_id = source.doc_id
                                renamed_from = source.path
                                graph.unresolve_incoming(conn, doc_id)
                                outcome = "renamed"
                            else:
                                created_at = now_iso()
                                inserted = conn.execute(
                                    "INSERT INTO documents(path,path_norm,title,version,"
                                    "content_hash,folder,file_state,vector_dirty,is_deleted,"
                                    "file_mtime,created_at,created_by,updated_at,updated_by) "
                                    "VALUES(?,?,?,?,?,?,'clean',1,0,?,?,NULL,?,NULL) "
                                    "RETURNING id",
                                    (
                                        rel,
                                        norm,
                                        title,
                                        1,
                                        chash,
                                        folder,
                                        mtime,
                                        created_at,
                                        created_at,
                                    ),
                                ).fetchone()
                                doc_id = int(inserted["id"])
                                new_version = 1
                                outcome = "created"
                            conn.execute(
                                "INSERT INTO revisions(doc_id,version,body,title,content_hash,"
                                "author_id,op,via,created_at) VALUES(?,?,?,?,?,NULL,?,'cli',?)",
                                (
                                    doc_id,
                                    new_version,
                                    content,
                                    title,
                                    chash,
                                    "rename" if renamed_from else "external-reconcile",
                                    now_iso(),
                                ),
                            )
                            svc._set_tags(conn, doc_id, tagset)
                            indexing.publish_prepared(conn, doc_id, title, folder, prepared)
                            graph.backfill_links_for(conn, doc_id, norm, stem)
                            conn.execute(
                                "DELETE FROM file_projection_cleanup WHERE path_norm=?",
                                (norm,),
                            )
                            audit.record(
                                conn,
                                actor=None,
                                via="cli",
                                action="doc_reconcile",
                                target=rel,
                                detail=(
                                    f"v{new_version} rename {renamed_from} -> {rel}"
                                    if renamed_from
                                    else f"v{new_version} create"
                                ),
                            )

                    if outcome is not None and not fp.stable_markdown_is_current(stable):
                        raise _ReindexRetry("file_changed")
            except _ReindexRetry as exc:
                if exc.reason == "pending_projection" and rel in pending_dependency_paths:
                    dependency_results = [
                        recover_one(source_id) for source_id in pending_source_ids_to_recover
                    ]
                    dependency_settled = all(result.settled for result in dependency_results)
                    if attempt < 3:
                        if dependency_settled:
                            pending_dependency_paths.discard(rel)
                        retried += 1
                        continue
                    record_committed_best(committed_best, rel)
                    conflicts[rel] = {
                        "path": rel,
                        "reason": exc.reason,
                        "attempts": attempt,
                    }
                    break
                if attempt < 3:
                    retried += 1
                    continue
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": exc.reason,
                    "attempts": attempt,
                }
                break
            except sqlite3.IntegrityError:
                if attempt < 3:
                    retried += 1
                    continue
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": "target_changed",
                    "attempts": attempt,
                }
                break
            except OSError:
                if attempt < 3:
                    retried += 1
                    continue
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": "file_unreadable",
                    "attempts": attempt,
                }
                break
            except fp.FileProjectionError:
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": "file_unreadable",
                    "attempts": attempt,
                }
                break

            if cleanup_owner_ids:
                owner_issues: list[fp.ProjectionResult] = []
                for owner_id in cleanup_owner_ids:
                    result = recover_one(owner_id)
                    if not result.settled:
                        owner_issues.append(result)
                try:
                    current_signature = fp.confined_file_signature(vault, path, missing_ok=True)
                except OSError:
                    if attempt < 3:
                        retried += 1
                        continue
                    record_committed_best(committed_best, rel)
                    conflicts[rel] = {
                        "path": rel,
                        "reason": "file_unreadable",
                        "attempts": attempt,
                    }
                    break
                except fp.FileProjectionError:
                    record_committed_best(committed_best, rel)
                    conflicts[rel] = {
                        "path": rel,
                        "reason": "file_unreadable",
                        "attempts": attempt,
                    }
                    break
                if current_signature is None:
                    cleanup_removed = True
                    record_committed_best(committed_best, rel)
                    break
                if owner_issues:
                    record_committed_best(committed_best, rel)
                    for issue in owner_issues:
                        issue_path = issue.path or rel
                        conflicts[issue_path] = {
                            "path": issue_path,
                            "reason": "pending_projection",
                            "attempts": attempt,
                        }
                    break
                if attempt < 3:
                    retried += 1
                    continue
                record_committed_best(committed_best, rel)
                conflicts[rel] = {
                    "path": rel,
                    "reason": "target_changed",
                    "attempts": attempt,
                }
                break

            outcome = cast(str, outcome)
            committed_priority = outcome_priority[outcome]
            committed = (outcome, renamed_from)
            if committed_best is None or committed_priority > outcome_priority[committed_best[0]]:
                committed_best = committed
            managed_superseded = False
            affected_owner_issues: list[fp.ProjectionResult] = []
            for owner_id in affected_owner_ids:
                result = recover_one(owner_id)
                if not result.settled:
                    affected_owner_issues.append(result)
            if affected_owner_issues:
                record_outcome(committed_best[0], rel, committed_best[1])
                for issue in affected_owner_issues:
                    issue_path = issue.path or rel
                    conflicts[issue_path] = {
                        "path": issue_path,
                        "reason": "pending_projection",
                        "attempts": attempt,
                    }
                break
            if outcome == "renamed" and renamed_from is not None:
                superseded_paths.add(renamed_from)
                transient_source_conflicts.discard(renamed_from)
                conflicts.pop(renamed_from, None)
            target_is_current = fp.stable_markdown_is_current(stable)
            source_peer_changed = False
            if source_decision_made and outcome in ("created", "renamed"):
                expected_peers = {
                    snapshot.doc_id: (snapshot, source_absent)
                    for snapshot, source_absent in source_peer_states
                    if snapshot.doc_id != doc_id and snapshot.doc_id not in ignored_source_owner_ids
                }
                with svc.db.reader() as conn:
                    post_peer_ids = [
                        int(row["id"])
                        for row in conn.execute(
                            "SELECT id FROM documents WHERE is_deleted=0 "
                            "AND content_hash=? AND id<>? ORDER BY id",
                            (chash, doc_id),
                        ).fetchall()
                        if int(row["id"]) not in ignored_source_owner_ids
                    ]
                    post_peers = {
                        peer_id: snapshot
                        for peer_id in post_peer_ids
                        if (snapshot := reindex_document_snapshot(conn, peer_id)) is not None
                    }
                if set(post_peers) != set(expected_peers):
                    source_peer_changed = True
                else:
                    for peer_id, post_peer in post_peers.items():
                        expected_peer, expected_absent = expected_peers[peer_id]
                        post_absent = inspect_source_absence(post_peer, attempt)
                        with svc.db.reader() as conn:
                            verified_peer = reindex_document_snapshot(conn, peer_id)
                        if (
                            post_peer != expected_peer
                            or verified_peer != post_peer
                            or post_absent != expected_absent
                        ):
                            source_peer_changed = True
                if source_peer_changed:
                    source_identity_changed = True
            if outcome == "renamed" and renamed_from is not None:
                post_source_target: Path | None = None
                old_signature: fp.FileSignature | None = None
                source_check_failed = False
                source_check_attempt = 0
                while True:
                    source_check_attempt += 1
                    try:
                        post_source_target = fp.managed_path(vault, renamed_from, namespace="live")
                        old_signature = fp.confined_file_signature(
                            vault, post_source_target, missing_ok=True
                        )
                    except (OSError, fp.FileProjectionError):
                        if source_check_attempt < 3:
                            retried += 1
                            continue
                        source_check_failed = True
                        conflicts.setdefault(
                            renamed_from,
                            {
                                "path": renamed_from,
                                "reason": "file_unreadable",
                                "attempts": source_check_attempt,
                            },
                        )
                    break
                if not source_check_failed:
                    assert post_source_target is not None
                    if old_signature is not None and renamed_from not in pending_paths:
                        requeue_count = source_requeues.get(renamed_from, 0)
                        if requeue_count < max_source_requeues:
                            source_requeues[renamed_from] = requeue_count + 1
                            pending_paths.add(renamed_from)
                            process_paths.append(
                                (
                                    path_norm(renamed_from),
                                    renamed_from,
                                    post_source_target,
                                )
                            )
                        else:
                            conflicts[renamed_from] = {
                                "path": renamed_from,
                                "reason": "rename_source_reappeared",
                                "attempts": max_source_requeues,
                            }
            if target_is_current:
                pending_dependency_paths.discard(rel)
                record_outcome(committed_best[0], rel, committed_best[1])
                if source_identity_changed:
                    conflicts[rel] = {
                        "path": rel,
                        "reason": "rename_source_changed",
                        "attempts": attempt,
                    }
                break
            if attempt < 3:
                retried += 1
                continue
            record_outcome(committed_best[0], rel, committed_best[1])
            conflicts[rel] = {
                "path": rel,
                "reason": "file_changed",
                "attempts": attempt,
            }
            break

    final_recovery = svc._recover_pending_report()
    recovered_pending += final_recovery.recovered
    for conflict_path, conflict in list(conflicts.items()):
        if conflict["reason"] != "pending_projection":
            continue
        if conflict_path in pending_dependency_paths:
            continue
        with svc.db.reader() as conn:
            pending = conn.execute(
                "SELECT 1 FROM documents d WHERE d.path_norm=? AND ("
                "d.file_state<>'clean' "
                "OR EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
                "OR EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id))",
                (path_norm(conflict_path),),
            ).fetchone()
        removable_key = {True: conflict_path, False: ""}[pending is None]
        conflicts.pop(removable_key, None)
    for issue in filter(lambda item: item.path is not None, final_recovery.issues):
        issue_path = cast(str, issue.path)
        replacement = {
            "path": issue_path,
            "reason": "pending_projection",
            "attempts": issue.attempts or 1,
        }
        existing = conflicts.get(issue_path, replacement)
        preserve_collision = existing["reason"] == "path_collision"
        conflicts[issue_path] = {
            True: existing,
            False: replacement,
        }[preserve_collision]

    def is_clean_live(snapshot: ReindexTargetSnapshot | None) -> bool:
        return bool(
            snapshot is not None
            and not snapshot.is_deleted
            and snapshot.file_state == "clean"
            and not snapshot.has_purge_intent
            and not snapshot.has_cleanup_intent
        )

    with svc.db.reader() as conn:
        clean_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT d.id FROM documents d "
                "WHERE d.is_deleted=0 AND d.file_state='clean' "
                "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p "
                "WHERE p.doc_id=d.id) "
                "AND NOT EXISTS(SELECT 1 FROM file_projection_cleanup c "
                "WHERE c.doc_id=d.id) ORDER BY d.path"
            ).fetchall()
        ]
        final_queue = [
            (snapshot, 1)
            for doc_id in clean_ids
            if (snapshot := reindex_document_snapshot(conn, doc_id)) is not None
        ]
    missing_paths: set[str] = set()
    final_index = 0
    while final_index < len(final_queue):
        snapshot, final_attempt = final_queue[final_index]
        final_index += 1
        final_rel = snapshot.path
        final_signature: fp.FileSignature | None = None
        final_unreadable = False
        try:
            final_path = fp.managed_path(vault, final_rel, namespace="live")
            final_signature = fp.confined_file_signature(vault, final_path, missing_ok=True)
        except (OSError, fp.FileProjectionError):
            final_unreadable = True
        with svc.db.reader() as conn:
            current = reindex_document_snapshot(conn, snapshot.doc_id)
        if current != snapshot:
            if is_clean_live(current):
                assert current is not None
                if final_attempt < 3:
                    final_queue.append((current, final_attempt + 1))
                else:
                    conflicts[current.path] = {
                        "path": current.path,
                        "reason": "target_changed",
                        "attempts": final_attempt,
                    }
            continue
        if final_unreadable:
            conflicts.setdefault(
                final_rel,
                {
                    "path": final_rel,
                    "reason": "file_unreadable",
                    "attempts": final_attempt,
                },
            )
        elif final_signature is None:
            missing_paths.add(final_rel)
    missing = sorted(missing_paths)
    for outcome_rel, (outcome, renamed_from) in classified_outcomes.items():
        if outcome == "skipped_deleted":
            skipped_deleted.append(outcome_rel)
        else:
            counts[outcome] += 1
            if outcome == "renamed" and renamed_from is not None:
                renames.append(f"{renamed_from} -> {outcome_rel}")
    embedded = indexing.embed_pending(svc.db, svc.embedder, progress=progress)
    skipped_conflicts = [conflicts[key] for key in sorted(conflicts)]
    log.info(
        "reindex: created=%d updated=%d renamed=%d unchanged=%d "
        "skipped_deleted=%d conflicts=%d missing_files=%d embedded=%d",
        counts["created"],
        counts["updated"],
        counts["renamed"],
        counts["unchanged"],
        len(skipped_deleted),
        len(skipped_conflicts),
        len(missing),
        embedded,
    )
    svc._bump_nav()
    return {
        **counts,
        "renames": renames,
        "retried": retried,
        "recovered_pending": recovered_pending,
        "missing_files": missing,
        "skipped_deleted": skipped_deleted,
        "skipped_conflicts": skipped_conflicts,
        "embedded": embedded,
    }
