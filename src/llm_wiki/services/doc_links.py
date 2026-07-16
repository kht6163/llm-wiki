"""Link-graph helpers for DocumentService.

Extracted so DocumentService stays a thin coordinator. Public entry points remain
on DocumentService (each delegates here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import graph as graph_mod
from ..markdown_utils import extract_links, rewrite_link_target
from ..util import (
    PathError,
    basename_stem,
    clamp_int,
    folder_of,
    normalize_rel_path,
    path_norm,
)
from .errors import ConflictError, ForbiddenError, NotFoundError

if TYPE_CHECKING:
    from .auth import Principal


def backlinks(svc, path: str, *, with_context: bool = False) -> dict:
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    with svc.db.reader() as conn:
        d = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
        ).fetchone()
        if not d:
            raise NotFoundError("No document at this path.", path=rel)
        return {
            "path": rel,
            "backlinks": graph_mod.get_backlinks(conn, d["id"], with_context=with_context),
        }

def links(svc, path: str) -> dict:
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    with svc.db.reader() as conn:
        d = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
        ).fetchone()
        if not d:
            raise NotFoundError("No document at this path.", path=rel)
        return {"path": rel, "links": graph_mod.get_outgoing(conn, d["id"])}

def graph(
    svc,
    root=None,
    depth=1,
    limit=500,
    include_unresolved=True,
    folder=None,
    tag=None,
    tags=None,
) -> dict:
    with svc.db.reader() as conn:
        return graph_mod.build_graph(
            conn,
            root,
            depth,
            limit,
            include_unresolved,
            folder=folder,
            tag=tag,
            tags=tags,
        )

def broken_links(svc, limit: int = 200) -> dict:
    """Vault-wide unresolved links (dangling references) for cleanup tooling."""
    limit = clamp_int(limit, 1, 2000)
    with svc.db.reader() as conn:
        items = graph_mod.list_broken_links(conn, limit)
    return {"count": len(items), "links": items}

def resolve_link(svc, target: str, from_path: str | None = None) -> str | None:
    """Resolve a wikilink/markdown target to an existing document path, or None."""
    src_folder = ""
    if from_path:
        try:
            src_folder = folder_of(normalize_rel_path(from_path))
        except Exception:
            src_folder = ""
    with svc.db.reader() as conn:
        return graph_mod.resolve_path(conn, target, src_folder)

def move_preview(svc, path: str, new_path: str) -> dict:
    """Read-only preview of a move: whether the destination is already taken, and the
    inbound links (other docs pointing at the current path) that fix_references would
    rewrite. Lets a caller see the blast radius before committing the move."""
    rel, new_rel = normalize_rel_path(path), normalize_rel_path(new_path)
    if not svc.exists(rel):
        raise NotFoundError("No document at this path.", path=rel)
    inbound = backlinks(svc, rel)["backlinks"]
    return {
        "from": rel,
        "to": new_rel,
        "dest_exists": svc.exists(new_rel),
        "inbound_count": len(inbound),
        "inbound": [b["src_path"] for b in inbound],
    }

def rename_references(svc, principal: Principal, old_path: str, new_path: str) -> dict:
    """Rewrite the link TEXT in other documents that pointed at ``old_path`` so it
    points at ``new_path`` — the cleanup ``move`` deliberately doesn't do inline.
    Only links that are currently broken AND keyed to the old path/name are
    touched (path-form links and bare-name links whose stem changed); a bare name
    that still resolves elsewhere is left alone. Each affected document gets one
    audited revision through the CAS update path, so a single conflict skips that
    document instead of aborting the rest.
    Returns {from, to, docs_rewritten, links_rewritten, skipped_conflicts}."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    old_rel, new_rel = normalize_rel_path(old_path), normalize_rel_path(new_path)
    old_norm, old_stem = path_norm(old_rel), basename_stem(old_rel).lower()
    new_noext = new_rel[:-3] if new_rel.lower().endswith(".md") else new_rel
    new_basename = new_noext.rsplit("/", 1)[-1]

    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT DISTINCT d.path FROM links l JOIN documents d ON d.id=l.src_doc_id "
            "WHERE d.is_deleted=0 AND l.is_resolved=0 AND "
            "((l.dst_is_path=1 AND l.dst_path_norm=?) OR (l.dst_is_path=0 AND l.dst_name=?))",
            (old_norm, old_stem),
        ).fetchall()
    candidates = [r["path"] for r in rows]

    docs_rewritten = links_rewritten = skipped = 0
    projection_pending: list[dict] = []
    for src_path in candidates:
        try:
            doc = svc.get(src_path)
        except NotFoundError:
            continue
        body = doc["content"]
        edits: list[tuple[int, int, str]] = []
        for link in extract_links(body):
            try:
                dpn, dname, is_path = graph_mod._link_keys(link.target)
            except PathError:
                continue
            if not ((is_path and dpn == old_norm) or (not is_path and dname == old_stem)):
                continue
            # Only repoint genuinely-broken refs; a bare name resolving elsewhere
            # is a legitimately different target now and must be left intact.
            if resolve_link(svc, link.target, doc["path"]):
                continue
            new_target = (
                (new_rel if link.target.lower().endswith(".md") else new_noext)
                if is_path
                else new_basename
            )
            edits.append((link.start, link.end, rewrite_link_target(link, new_target)))
        if not edits:
            continue
        new_body = body
        for start, end, new_raw in sorted(edits, key=lambda e: e[0], reverse=True):
            new_body = new_body[:start] + new_raw + new_body[end:]
        try:
            svc.update(principal, doc["path"], doc["version"], new_body)
        except ConflictError:
            skipped += 1
            continue
        except Exception as exc:
            from .documents import ProjectionPendingError

            if not isinstance(exc, ProjectionPendingError):  # pragma: no cover - unexpected
                raise
            # The reference rewrite committed to the DB.  Keep processing the
            # remaining candidates and report only the recoverable projection
            # work, rather than turning a vault-wide cleanup into an apparent
            # all-or-nothing failure.
            docs_rewritten += 1
            links_rewritten += len(edits)
            projection_pending.append(
                {
                    "path": doc["path"],
                    "reason": exc.result.reason,
                    "version": exc.extra.get("version"),
                }
            )
            continue
        docs_rewritten += 1
        links_rewritten += len(edits)
    return {
        "from": old_rel,
        "to": new_rel,
        "docs_rewritten": docs_rewritten,
        "links_rewritten": links_rewritten,
        "skipped_conflicts": skipped,
        "projection_pending": projection_pending,
    }

