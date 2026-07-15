"""Document lifecycle CRUD for DocumentService.

create / update / delete / restore / purge / move / daily_note / list_deleted.

Projection primitives stay on DocumentService; these functions call them via svc.
Public entry points remain DocumentService methods (thin delegates).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .. import file_projection as fp
from .. import graph, indexing
from ..markdown_utils import (
    derive_content_title,
    derive_title,
    parse_frontmatter,
)
from ..metrics import DOC_WRITES
from ..util import (
    basename_stem,
    clamp_int,
    folder_of,
    normalize_folder_path,
    normalize_rel_path,
    now_iso,
    path_norm,
    sha256_hex,
)
from . import audit
from .auth import Principal
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger("llm_wiki.documents")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Mirror documents.py retention so keyed writes stay in lockstep.
_IDEM_RETENTION_DAYS = 7


def list_deleted(svc, limit: int = 100, offset: int = 0) -> list[dict]:
    """Soft-deleted documents (the trash), most-recently-deleted first. Each carries
    path/title/version/folder, when and by whom it was deleted — enough to decide
    what to restore or purge."""
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT id, path, title, version, folder, updated_at, updated_by "
            "FROM documents WHERE is_deleted=1 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (clamp_int(limit, 1, 1000), max(0, int(offset))),
        ).fetchall()
        return [
            {
                "path": r["path"],
                "title": r["title"] or r["path"],
                "version": r["version"],
                "folder": r["folder"],
                "updated_at": r["updated_at"],
                "deleted_by": svc._username(conn, r["updated_by"]),
            }
            for r in rows
        ]

def daily_note(
    svc, principal: Principal, date: str | None = None, *, folder: str = "daily"
) -> dict:
    """Open the daily note for ``date`` (YYYY-MM-DD; default today, UTC), creating it
    if absent — the journaling entry point. Reading an existing note needs no write
    permission; only creating one does. The new note carries a minimal ``# <date>``
    heading. Returns the document (path/version/content/…) plus ``created`` (True if
    it was just made)."""
    if date:
        date = str(date).strip()
        if not _DATE_RE.match(date):
            raise ValidationError("date must be in YYYY-MM-DD form.")
    else:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
    fold = normalize_folder_path(folder) if folder else ""
    rel = (fold + "/" if fold else "") + date + ".md"
    if svc.exists(rel):
        return {**svc.get(rel), "created": False}
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot create the daily note (read/search only)."
        )
    return {**svc.create(principal, rel, f"# {date}\n\n", title=date), "created": True}

def delete(svc, principal: Principal, path: str, base_version: int | None = None) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot delete documents (read/search only)."
        )
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    now = now_iso()
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        row = conn.execute(
            "SELECT d.id,d.path,d.path_norm,d.version,d.title,d.content_hash,"
            "d.is_deleted,r.body,r.content_hash AS revision_content_hash,"
            "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
            "AS has_purge_intent FROM documents d LEFT JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.path_norm=?",
            (norm,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise NotFoundError("No document at this path.", path=rel)
        if row["has_purge_intent"]:
            raise ConflictError("Permanent deletion is already in progress.", path=rel)
        actual_rel = str(row["path"])
        fp.managed_path(svc.vault, actual_rel, namespace="live")
        doc_id = int(row["id"])
        if base_version is not None and int(base_version) != row["version"]:
            raise svc._conflict(conn, doc_id, actual_rel)
        body = str(row["body"]) if row["body"] is not None else None
        if (
            body is None
            or row["revision_content_hash"] != row["content_hash"]
            or sha256_hex(body) != row["content_hash"]
        ):
            raise RuntimeError("current document revision is missing or corrupt")
        new_version = int(row["version"]) + 1
        conn.execute(
            "UPDATE documents SET is_deleted=1, version=version+1, file_state='pending', "
            "vector_dirty=0, updated_at=?, updated_by=? "
            "WHERE id=? AND path=? AND path_norm=? AND version=? AND is_deleted=0 "
            "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=?)",
            (
                now,
                principal.user_id,
                doc_id,
                actual_rel,
                str(row["path_norm"]),
                int(row["version"]),
                doc_id,
            ),
        )
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
            "VALUES(?,?,?,?,?,?, 'delete', ?, ?)",
            (
                doc_id,
                new_version,
                body,
                row["title"],
                sha256_hex(body),
                principal.user_id,
                principal.via,
                now,
            ),
        )
        indexing.remove_fts(conn, doc_id)
        indexing.clear_chunks(conn, doc_id)
        graph.unresolve_incoming(conn, doc_id)
        conn.execute("DELETE FROM links WHERE src_doc_id=?", (doc_id,))
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="doc_delete",
            target=actual_rel,
            detail=f"v{new_version}",
        )
    svc._require_projection(doc_id)
    DOC_WRITES.labels("delete").inc()
    svc._emit(
        "delete", actual_rel, new_version, updated_by=principal.username, via=principal.via
    )
    svc._bump_nav()
    return {"ok": True, "path": actual_rel, "deleted": True}

def restore(svc, principal: Principal, path: str) -> dict:
    """Bring a soft-deleted document back (editor/admin only): un-tombstone it, rebuild
    the search/graph artifacts that delete tore down (FTS rows, chunks, link edges,
    and inbound-link backfill), re-project the .md, and re-embed. The pre-delete body
    is the latest revision's, so no content travels through the caller."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot restore documents (read/search only)."
        )
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    now = now_iso()
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        row = conn.execute(
            "SELECT d.id,d.path,d.path_norm,d.version,d.title,d.folder,d.content_hash,"
            "d.is_deleted,r.body,r.content_hash AS revision_content_hash,"
            "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
            "AS has_purge_intent FROM documents d LEFT JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.path_norm=?",
            (norm,),
        ).fetchone()
        if not row:
            raise NotFoundError("No document at this path.", path=rel)
        if not row["is_deleted"]:
            raise ValidationError("Document is not deleted; nothing to restore.")
        actual_rel = str(row["path"])
        if row["has_purge_intent"]:
            raise ConflictError("Permanent deletion is already in progress.", path=actual_rel)
        fp.managed_path(svc.vault, actual_rel, namespace="live")
        doc_id, title, folder = int(row["id"]), row["title"], row["folder"]
        body = str(row["body"]) if row["body"] is not None else None
        if (
            body is None
            or row["revision_content_hash"] != row["content_hash"]
            or sha256_hex(body) != row["content_hash"]
        ):
            raise RuntimeError("current document revision is missing or corrupt")
        new_version = int(row["version"]) + 1
        conn.execute(
            "UPDATE documents SET version=version+1, content_hash=?, file_state='pending', "
            "vector_dirty=1, is_deleted=0, updated_at=?, updated_by=? "
            "WHERE id=? AND path=? AND path_norm=? AND version=? AND is_deleted=1 "
            "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=?)",
            (
                sha256_hex(body),
                now,
                principal.user_id,
                doc_id,
                actual_rel,
                str(row["path_norm"]),
                int(row["version"]),
                doc_id,
            ),
        )
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
            "VALUES(?,?,?,?,?,?, 'edit', ?, ?)",
            (
                doc_id,
                new_version,
                body,
                title,
                sha256_hex(body),
                principal.user_id,
                principal.via,
                now,
            ),
        )
        # tags survive a soft delete (delete() leaves the tags table alone), so only
        # the FTS/chunk/link artifacts — torn down on delete — need rebuilding.
        indexing.reindex_fts(conn, doc_id, title, body)
        indexing.rechunk(conn, doc_id, body)
        indexing.reindex_links(conn, doc_id, body, folder)
        graph.backfill_links_for(
            conn,
            doc_id,
            str(row["path_norm"]),
            basename_stem(actual_rel).lower(),
        )
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="doc_restore",
            target=actual_rel,
            detail=f"v{new_version}",
        )
    svc._require_projection(doc_id)
    svc._embed(doc_id)
    DOC_WRITES.labels("restore").inc()
    svc._emit(
        "restore",
        actual_rel,
        new_version,
        updated_by=principal.username,
        via=principal.via,
    )
    svc._bump_nav()
    return {
        "ok": True,
        "path": actual_rel,
        "version": new_version,
        "restored": True,
    }

def move(
    svc, principal: Principal, path: str, new_path: str, fix_references: bool = False
) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot move documents (read/search only)."
        )
    rel, new_rel = normalize_rel_path(path), normalize_rel_path(new_path)
    norm, new_norm = path_norm(rel), path_norm(new_rel)
    if norm == new_norm:
        return svc.get(rel)
    fp.managed_path(svc.vault, new_rel, namespace="live")
    new_folder, new_stem = folder_of(new_rel), basename_stem(new_rel).lower()
    now = now_iso()
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        row = conn.execute(
            "SELECT d.id,d.path,d.path_norm,d.version,d.title,d.content_hash,"
            "d.is_deleted,r.body,r.content_hash AS revision_content_hash "
            "FROM documents d LEFT JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.path_norm=?",
            (norm,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise NotFoundError("No document at this path.", path=rel)
        source_rel = str(row["path"])
        source_norm = str(row["path_norm"])
        body = str(row["body"]) if row["body"] is not None else None
        if (
            body is None
            or row["revision_content_hash"] != row["content_hash"]
            or sha256_hex(body) != row["content_hash"]
        ):
            raise RuntimeError("current document revision is missing or corrupt")
        clash = conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?", (new_norm,)
        ).fetchone()
        if clash:
            raise ConflictError("The destination path is already occupied.", path=new_rel)
        doc_id, new_version = int(row["id"]), int(row["version"]) + 1

        # Capture the exact source file generation before changing the canonical
        # path. The DB writer fence serializes this authority with every managed
        # publisher; the full signature prevents a later external generation from
        # being deleted by a delayed cleanup.
        source_target = fp.managed_path(svc.vault, source_rel, namespace="live")
        source_signature = fp.confined_file_signature(
            svc.vault, source_target, missing_ok=True
        )
        conn.execute(
            "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
            (doc_id, new_norm),
        )
        signature_values = (
            (
                source_signature.dev,
                source_signature.ino,
                source_signature.size,
                source_signature.mtime_ns,
                source_signature.ctime_ns,
            )
            if source_signature is not None
            else (None, None, None, None, None)
        )
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(doc_id,path_norm) DO UPDATE SET path=excluded.path,"
            "expected_exists=excluded.expected_exists,expected_dev=excluded.expected_dev,"
            "expected_ino=excluded.expected_ino,expected_size=excluded.expected_size,"
            "expected_mtime_ns=excluded.expected_mtime_ns,"
            "expected_ctime_ns=excluded.expected_ctime_ns,"
            "queued_version=excluded.queued_version,created_at=excluded.created_at",
            (
                doc_id,
                source_rel,
                source_norm,
                int(source_signature is not None),
                *signature_values,
                new_version,
                now,
            ),
        )
        conn.execute(
            "UPDATE documents SET path=?, path_norm=?, folder=?, version=version+1, "
            "file_state='pending', updated_at=?, updated_by=? "
            "WHERE id=? AND path=? AND path_norm=? AND version=?",
            (
                new_rel,
                new_norm,
                new_folder,
                now,
                principal.user_id,
                doc_id,
                source_rel,
                source_norm,
                int(row["version"]),
            ),
        )
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
            "VALUES(?,?,?,?,?,?, 'rename', ?, ?)",
            (
                doc_id,
                new_version,
                body,
                row["title"],
                sha256_hex(body),
                principal.user_id,
                principal.via,
                now,
            ),
        )
        # Incoming links that resolved to the old path/name are now stale; drop
        # their resolution and re-resolve anything pointing at the new path/name.
        graph.unresolve_incoming(conn, doc_id)
        graph.backfill_links_for(conn, doc_id, new_norm, new_stem)
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="doc_move",
            target=f"{source_rel} -> {new_rel}",
        )
    svc._require_projection(doc_id)
    DOC_WRITES.labels("move").inc()
    # Keyed on the OLD path so a viewer of the moved doc can follow it to `to`.
    svc._emit(
        "move",
        source_rel,
        new_version,
        to=new_rel,
        updated_by=principal.username,
        via=principal.via,
    )
    result = svc.get(new_rel)
    if fix_references:
        # Re-resolution above fixed the GRAPH, but bodies still contain the old
        # link text; rewrite those so the references don't show up broken.
        result = {
            **result,
            "references": svc.rename_references(principal, source_rel, new_rel),
        }
    svc._bump_nav()
    return result

def purge(svc, principal: Principal, path: str) -> dict:
    """Durably request and finish permanent deletion of a soft-deleted document."""
    from .documents import ProjectionPendingError
    if not principal.can_admin:
        raise ForbiddenError("Only an admin can permanently delete a document.")
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    doc_id: int | None = None
    actual_rel = rel

    for _attempt in range(3):
        with svc.db.reader() as conn:
            row = conn.execute(
                "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,"
                "d.file_state,d.is_deleted,"
                "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
                "AS has_purge_intent FROM documents d WHERE d.path_norm=?",
                (norm,),
            ).fetchone()
        if row is None:
            raise NotFoundError("No document at this path.", path=rel)
        if not row["is_deleted"]:
            raise ValidationError("Document is not in the trash; delete it first.")

        doc_id = int(row["id"])
        actual_rel = str(row["path"])
        if row["has_purge_intent"]:
            break

        initial_token = (
            actual_rel,
            str(row["path_norm"]),
            int(row["version"]),
            str(row["content_hash"]),
            bool(row["is_deleted"]),
        )
        initially_pending = row["file_state"] == "pending"
        projection: fp.ProjectionResult | None = None
        if initially_pending:
            projection = svc._project_current(doc_id)
            if (
                not projection.settled
                and not projection.current_installed
                and projection.reason != "purge_pending"
            ):
                raise ProjectionPendingError(projection, committed=False)

        retry = False
        with svc.db.writer() as conn:
            svc._fence_principal(conn, principal, require_admin=True)
            current = conn.execute(
                "SELECT d.id,d.path,d.path_norm,d.version,d.content_hash,"
                "d.file_state,d.is_deleted,"
                "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
                "AS has_purge_intent,"
                "EXISTS(SELECT 1 FROM revisions r WHERE r.doc_id=d.id "
                "AND r.version=d.version AND r.content_hash=d.content_hash) "
                "AS exact_revision FROM documents d WHERE d.id=?",
                (doc_id,),
            ).fetchone()
            if current is None:
                retry = True
            elif current["has_purge_intent"]:
                actual_rel = str(current["path"])
            elif not current["is_deleted"]:
                raise ConflictError(
                    "The document was restored before purge could begin.",
                    path=actual_rel,
                )
            else:
                current_token = (
                    str(current["path"]),
                    str(current["path_norm"]),
                    int(current["version"]),
                    str(current["content_hash"]),
                    bool(current["is_deleted"]),
                )
                if current_token != initial_token or not current["exact_revision"]:
                    retry = True
                else:
                    if initially_pending:
                        live = fp.managed_path(svc.vault, actual_rel, namespace="live")
                        if not fp.confirm_confined_absence(svc.vault, live):
                            raise ProjectionPendingError(
                                fp.ProjectionResult(
                                    doc_id,
                                    actual_rel,
                                    False,
                                    False,
                                    "purge_live_present",
                                    1,
                                    True,
                                    "The pending tombstone still has a live file.",
                                    bool(projection and projection.current_installed),
                                ),
                                committed=False,
                            )
                    conn.execute(
                        "INSERT INTO document_purge_intents("
                        "doc_id,path,path_norm,version,actor,via,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            doc_id,
                            str(current["path"]),
                            str(current["path_norm"]),
                            int(current["version"]),
                            principal.username,
                            principal.via,
                            now_iso(),
                        ),
                    )
                    conn.execute(
                        "UPDATE documents SET file_state='pending' "
                        "WHERE id=? AND path=? AND path_norm=? AND version=? "
                        "AND content_hash=? AND is_deleted=1 "
                        "AND EXISTS(SELECT 1 FROM document_purge_intents p "
                        "WHERE p.doc_id=?)",
                        (
                            doc_id,
                            str(current["path"]),
                            str(current["path_norm"]),
                            int(current["version"]),
                            str(current["content_hash"]),
                            doc_id,
                        ),
                    )
                    actual_rel = str(current["path"])
        if retry:
            continue
        break
    else:
        raise ConflictError(
            "The deleted document kept changing while purge was requested.",
            path=actual_rel,
        )

    assert doc_id is not None
    result = svc._finish_purge(doc_id)
    if not result.settled:
        raise ProjectionPendingError(result)
    svc._bump_nav()
    return {"ok": True, "path": actual_rel, "purged": True}

def create(
    svc,
    principal: Principal,
    path: str,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    *,
    embed: bool = True,
    template: str | None = None,
) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot create documents (read/search only)."
        )
    rel = normalize_rel_path(path)
    # Reject internal namespaces and unsafe existing path components before the
    # canonical DB write commits. Parent directories may be created later by the
    # staged projector, after the row is durably marked pending.
    fp.managed_path(svc.vault, rel, namespace="live")
    norm, folder, stem = path_norm(rel), folder_of(rel), basename_stem(rel).lower()
    if template is not None:
        content = svc._load_template_body(template)
    else:
        content = content or ""
    meta = parse_frontmatter(content)[0]
    final_title = (title or derive_title(meta, content, rel)).strip()
    tagset = svc._merge_tags(meta, content, tags)
    chash, now = sha256_hex(content), now_iso()

    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        row = conn.execute(
            "SELECT d.id,d.path,d.version,d.is_deleted,"
            "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
            "AS has_purge_intent FROM documents d WHERE d.path_norm=?",
            (norm,),
        ).fetchone()
        if row and not row["is_deleted"]:
            raise svc._conflict(
                conn, row["id"], rel, message="A document already exists at this path."
            )
        if row and row["is_deleted"]:  # revive a tombstone
            if row["has_purge_intent"]:
                raise ConflictError("Permanent deletion is already in progress.", path=rel)
            # A normalized-path match may differ only in spelling/casing. Keep
            # the tombstone's canonical path so the common live projector removes
            # the exact existing trash copy instead of orphaning it.
            rel = str(row["path"])
            norm = path_norm(rel)
            folder = folder_of(rel)
            stem = basename_stem(rel).lower()
            fp.managed_path(svc.vault, rel, namespace="live")
            doc_id = int(row["id"])
            new_version = int(row["version"]) + 1
            conn.execute(
                "UPDATE documents SET path=?, title=?, version=?, content_hash=?, folder=?, "
                "file_state='pending', vector_dirty=1, is_deleted=0, updated_at=?, updated_by=? "
                "WHERE id=? AND version=? AND is_deleted=1 "
                "AND NOT EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=?)",
                (
                    rel,
                    final_title,
                    new_version,
                    chash,
                    folder,
                    now,
                    principal.user_id,
                    doc_id,
                    int(row["version"]),
                    doc_id,
                ),
            )
        else:
            inserted = conn.execute(
                "INSERT INTO documents(path, path_norm, title, version, content_hash, folder, "
                "file_state, vector_dirty, is_deleted, created_at, created_by, updated_at, updated_by) "
                "VALUES(?,?,?,?,?,?, 'pending', 1, 0, ?,?,?,?) RETURNING id",
                (
                    rel,
                    norm,
                    final_title,
                    1,
                    chash,
                    folder,
                    now,
                    principal.user_id,
                    now,
                    principal.user_id,
                ),
            ).fetchone()
            doc_id, new_version = int(inserted["id"]), 1
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
            "VALUES(?,?,?,?,?,?, 'create', ?, ?)",
            (
                doc_id,
                new_version,
                content,
                final_title,
                chash,
                principal.user_id,
                principal.via,
                now,
            ),
        )
        svc._set_tags(conn, doc_id, tagset)
        indexing.reindex_fts(conn, doc_id, final_title, content)
        indexing.rechunk(conn, doc_id, content)
        indexing.reindex_links(conn, doc_id, content, folder)
        graph.backfill_links_for(conn, doc_id, norm, stem)
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="doc_create",
            target=rel,
            detail=f"v{new_version}",
        )

    svc._require_projection(int(doc_id))
    if embed:
        svc._embed(int(doc_id))
    DOC_WRITES.labels("create").inc()
    svc._emit(
        "create",
        rel,
        new_version,
        title=final_title,
        updated_by=principal.username,
        via=principal.via,
    )
    svc._bump_nav()
    return svc.get(rel)

def update(
    svc,
    principal: Principal,
    path: str,
    base_version: int | None,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    *,
    embed: bool = True,
    idempotency: tuple[str, int, str, str] | None = None,
) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    if base_version is None:
        raise ValidationError("base_version is required for updates.")
    rel = normalize_rel_path(path)
    fp.managed_path(svc.vault, rel, namespace="live")
    norm, folder = path_norm(rel), folder_of(rel)
    content = content or ""
    meta = parse_frontmatter(content)[0]
    content_title = derive_content_title(meta, content)
    derived_tags = svc._merge_tags(meta, content, tags)
    chash, now = sha256_hex(content), now_iso()

    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        row = conn.execute(
            "SELECT id, title, version, content_hash, is_deleted FROM documents "
            "WHERE path_norm=?",
            (norm,),
        ).fetchone()
        if not row or row["is_deleted"]:
            raise NotFoundError("No document at this path.", path=rel)
        doc_id = row["id"]
        final_title = (
            title.strip() if title and title.strip() else content_title or row["title"]
        )
        current_tags = svc._tags_for_ids(conn, [doc_id]).get(doc_id, [])
        tagset = derived_tags if (tags is not None or derived_tags) else current_tags
        content_changed = row["content_hash"] != chash
        # vector_dirty moves monotonically toward dirty: a content change forces
        # 1, but an unchanged-content edit must NOT clear a pending flag — doing so
        # would cancel an embedding that reindex queued (vector_dirty=1, no vectors
        # yet) and the doc would silently vanish from vector search forever.
        cur = conn.execute(
            "UPDATE documents SET version=version+1, title=?, content_hash=?, folder=?, "
            "file_state='pending', vector_dirty=CASE WHEN ? THEN 1 ELSE vector_dirty END, "
            "updated_at=?, updated_by=? WHERE id=? AND version=?",
            (
                final_title,
                chash,
                folder,
                1 if content_changed else 0,
                now,
                principal.user_id,
                doc_id,
                int(base_version),
            ),
        )
        if cur.rowcount == 0:
            raise svc._conflict(conn, doc_id, rel)
        new_version = int(base_version) + 1
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
            "VALUES(?,?,?,?,?,?, 'edit', ?, ?)",
            (
                doc_id,
                new_version,
                content,
                final_title,
                chash,
                principal.user_id,
                principal.via,
                now,
            ),
        )
        svc._set_tags(conn, doc_id, tagset)
        indexing.reindex_fts(conn, doc_id, final_title, content)
        if content_changed:
            indexing.rechunk(conn, doc_id, content)
        indexing.reindex_links(conn, doc_id, content, folder)
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="doc_update",
            target=rel,
            detail=f"v{new_version}",
        )
        if idempotency is not None:
            # Stamp the key in the SAME transaction as the write it guards. If a
            # concurrent request already committed this key, the UNIQUE constraint
            # raises here and the whole write rolls back — so the duplicate never
            # lands (the caller then replays the original result).
            scope, uid, key, request_hash = idempotency
            conn.execute(
                "INSERT INTO idempotency_keys(scope, user_id, idem_key, doc_id, "
                "result_version, result_path, request_hash, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (scope, uid, key, doc_id, new_version, rel, request_hash, now),
            )
            cutoff = (datetime.now(UTC) - timedelta(days=_IDEM_RETENTION_DAYS)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,))

    svc._require_projection(int(doc_id))
    if content_changed and embed:
        svc._embed(int(doc_id))
    DOC_WRITES.labels("update").inc()
    svc._emit(
        "update",
        rel,
        new_version,
        title=final_title,
        updated_by=principal.username,
        via=principal.via,
        content_changed=content_changed,
    )
    svc._bump_nav()
    return svc.get(rel)
