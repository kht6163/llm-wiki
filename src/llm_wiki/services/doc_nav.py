"""Navigation, folders, favorites, and templates for DocumentService.

Extracted so DocumentService stays a thin coordinator. Public entry points remain
on DocumentService (each delegates here).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import file_projection as fp
from ..markdown_utils import derive_title, parse_frontmatter
from ..util import (
    clamp_int,
    normalize_folder_path,
    normalize_rel_path,
    now_iso,
    path_norm,
    safe_join,
)
from . import audit
from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from .auth import Principal


def folders(svc) -> list[str]:
    """Distinct non-empty folder paths across non-deleted documents (sorted)."""
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT DISTINCT folder FROM documents WHERE is_deleted=0 AND folder<>'' "
            "ORDER BY folder"
        ).fetchall()
    return [r[0] for r in rows]

def folder_counts(svc) -> list[tuple[str, int]]:
    """(folder, document count) across ALL non-deleted docs — independent of any
    list page, so the sidebar stays accurate under pagination. Explicitly-created
    empty folders are included with a count of 0."""
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT folder, COUNT(*) AS n FROM documents WHERE is_deleted=0 AND folder<>'' "
            "GROUP BY folder"
        ).fetchall()
        empties = conn.execute("SELECT path FROM folders").fetchall()
    counts = {r["folder"]: r["n"] for r in rows}
    for e in empties:
        counts.setdefault(e["path"], 0)
    return sorted(counts.items())

def list_folders(svc) -> list[str]:
    """Every folder path that should appear in the tree: folders that hold
    documents, every ancestor of those, and explicitly-created empty folders.
    Sorted, root ('') excluded."""
    paths: set[str] = set()
    with svc.db.reader() as conn:
        for r in conn.execute(
            "SELECT DISTINCT folder FROM documents WHERE is_deleted=0 AND folder<>''"
        ):
            paths.add(r["folder"])
        for r in conn.execute("SELECT path FROM folders"):
            paths.add(r["path"])
    # Add every ancestor so the tree never has a gap (a/b/c implies a, a/b).
    for p in list(paths):
        segs = p.split("/")
        for i in range(1, len(segs)):
            paths.add("/".join(segs[:i]))
    paths.discard("")
    return sorted(paths)

def tree(svc) -> dict:
    """Hierarchical folder/document tree for the sidebar file explorer. Combines
    document folders, their ancestors, and explicitly-created empty folders.
    Returns a root node: {name, path, folders:[child nodes], docs:[{path,title}]}
    with folders/docs sorted for stable rendering."""
    with svc.db.reader() as conn:
        doc_rows = conn.execute(
            "SELECT path, title, folder FROM documents WHERE is_deleted=0"
        ).fetchall()
        folder_rows = conn.execute("SELECT path FROM folders").fetchall()
    root: dict = {"name": "", "path": "", "folders": {}, "docs": []}

    def ensure(folder_path: str) -> dict:
        node = root
        if not folder_path:
            return node
        acc: list[str] = []
        for seg in folder_path.split("/"):
            acc.append(seg)
            child = node["folders"].get(seg)
            if child is None:
                child = {"name": seg, "path": "/".join(acc), "folders": {}, "docs": []}
                node["folders"][seg] = child
            node = child
        return node

    for fr in folder_rows:
        ensure(fr["path"])
    for r in doc_rows:
        ensure(r["folder"] or "")["docs"].append(
            {"path": r["path"], "title": r["title"] or r["path"]}
        )

    def finalize(node: dict) -> dict:
        children = sorted(node["folders"].values(), key=lambda c: c["name"].lower())
        node["folders"] = [finalize(c) for c in children]
        node["docs"].sort(key=lambda d: (d["title"] or "").lower())
        return node

    return finalize(root)

def create_folder(svc, principal: Principal, path: str) -> dict:
    """Persist an (initially empty) folder so it survives with no documents.
    Idempotent-ish: a duplicate raises ConflictError. Projects a real directory
    into the vault to mirror the DB."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot create folders (read/search only)."
        )
    rel = normalize_folder_path(path)
    if not rel:
        raise ValidationError("folder path must not be empty.")
    norm = rel.lower()
    now = now_iso()
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        if conn.execute("SELECT 1 FROM folders WHERE path_norm=?", (norm,)).fetchone():
            raise ConflictError("A folder already exists at this path.", path=rel)
        if conn.execute(
            "SELECT 1 FROM documents WHERE is_deleted=0 AND (folder=? OR folder LIKE ?)",
            (rel, norm + "/%"),
        ).fetchone():
            # The folder is already populated by documents — registering it as a
            # row is harmless but pointless; treat as already-existing.
            raise ConflictError("A folder already exists at this path.", path=rel)
        conn.execute(
            "INSERT INTO folders(path, path_norm, created_at, created_by) VALUES(?,?,?,?)",
            (rel, norm, now, principal.user_id),
        )
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="folder_create",
            target=rel,
        )
    safe_join(svc.vault, rel).mkdir(parents=True, exist_ok=True)
    svc._bump_nav()
    return {"ok": True, "path": rel}

def delete_folder(svc, principal: Principal, path: str) -> dict:
    """Remove an empty folder (and any explicitly-created empty subfolders).
    Refuses if any document still lives under it."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot delete folders (read/search only)."
        )
    rel = normalize_folder_path(path)
    if not rel:
        raise ValidationError("folder path must not be empty.")
    norm = rel.lower()
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal, require_write=True)
        n = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND (folder=? OR folder LIKE ?)",
            (rel, norm + "/%"),
        ).fetchone()[0]
        if n:
            raise ValidationError(
                f"Folder is not empty ({n} document(s)); move or delete them first."
            )
        cur = conn.execute(
            "DELETE FROM folders WHERE path_norm=? OR path_norm LIKE ?", (norm, norm + "/%")
        )
        if cur.rowcount == 0:
            raise NotFoundError("No such folder.", path=rel)
        audit.record(
            conn,
            actor=principal.username,
            via=principal.via,
            action="folder_delete",
            target=rel,
        )
    # Best-effort: prune the now-empty projected directory tree (bottom-up,
    # leaving any directory that still holds stray external files).
    target = safe_join(svc.vault, rel)
    if target.is_dir():
        for root_, _dirs, _files in os.walk(target, topdown=False):
            try:
                os.rmdir(root_)
            except OSError:
                pass
    svc._bump_nav()
    return {"ok": True, "path": rel, "deleted": True}

def tags(svc) -> list[dict]:
    """Tag vocabulary across non-deleted documents, most-used first."""
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT t.tag AS tag, COUNT(*) AS count FROM tags t "
            "JOIN documents d ON d.id=t.doc_id WHERE d.is_deleted=0 "
            "GROUP BY t.tag ORDER BY count DESC, t.tag ASC"
        ).fetchall()
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]

def _top_tags(svc, n: int) -> list[dict]:
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT t.tag AS tag, COUNT(*) AS count FROM tags t "
            "JOIN documents d ON d.id=t.doc_id WHERE d.is_deleted=0 "
            "GROUP BY t.tag ORDER BY count DESC, t.tag ASC LIMIT ?",
            (n,),
        ).fetchall()
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]

def _ensure_nav(svc) -> None:
    with svc._nav_lock:
        if svc._nav_cache_gen != svc._nav_gen or svc._nav_tree is None:
            svc._nav_tree = tree(svc)
            svc._nav_tags = _top_tags(svc, 40)
            svc._nav_cache_gen = svc._nav_gen

def nav_tree(svc) -> dict:
    """Cached sidebar file tree (rebuilt lazily after a structural write)."""
    _ensure_nav(svc)
    assert svc._nav_tree is not None
    return svc._nav_tree

def nav_tags(svc) -> list[dict]:
    """Cached sidebar top-40 tag list (rebuilt lazily after a structural write)."""
    _ensure_nav(svc)
    assert svc._nav_tags is not None
    return svc._nav_tags

def list_templates(svc) -> list[dict]:
    """List ``.md`` templates under vault ``/_templates/``.

    Returns ``[{name, path, title, preview}]`` sorted by name. ``preview`` is
    the first ~200 characters of the body after frontmatter is stripped.
    Templates are not indexed as wiki documents.
    """
    from . import documents as dm
    items: list[dict] = []
    try:
        names = sorted(fp.list_confined_names(svc.vault, dm.TEMPLATES_DIR), key=str.lower)
    except (OSError, fp.FileProjectionError):
        return []
    for name in names:
        if not name.lower().endswith(".md"):
            continue
        if name.startswith(".") or "/" in name or "\\" in name:
            continue
        try:
            _path, data = fp.read_confined_bytes(svc.vault, f"{dm.TEMPLATES_DIR}/{name}")
            raw = data.decode("utf-8")
        except (OSError, UnicodeError, fp.FileProjectionError):
            continue
        meta, body_start = parse_frontmatter(raw)
        body = raw[body_start:]
        rel = f"{dm.TEMPLATES_DIR}/{name}"
        title = derive_title(meta, raw, rel)
        preview = body.lstrip("\n\r")[:dm.TEMPLATE_PREVIEW_CHARS]
        items.append(
            {
                "name": name[:-3],
                "path": rel,
                "title": title,
                "preview": preview,
            }
        )
    return items

def _resolve_template_path(svc, name: str) -> Path:
    """Resolve a template name to a file under ``_templates/`` only.

    Accepts ``foo`` or ``foo.md``. Rejects path traversal and absolute paths.
    """
    from . import documents as dm
    if name is None or not str(name).strip():
        raise ValidationError("template name is required")
    raw = str(name).strip().replace("\\", "/").lstrip("/")
    # Optional vault-relative prefix is allowed but stripped.
    if raw.lower().startswith(f"{dm.TEMPLATES_DIR}/"):
        raw = raw[len(dm.TEMPLATES_DIR) + 1 :]
    if not raw or raw.startswith("~"):
        raise ValidationError("invalid template path")
    parts: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." or any(ord(c) < 0x20 or ord(c) == 0x7F for c in seg):
            raise ValidationError("invalid template path")
        parts.append(seg)
    # Flat templates only: a single filename segment (no nested path / traversal).
    if len(parts) != 1:
        raise ValidationError("invalid template path")
    filename = parts[0]
    if not filename.lower().endswith(".md"):
        filename = f"{filename}.md"
    target = svc.vault / dm.TEMPLATES_DIR / filename
    try:
        fp.read_confined_bytes(svc.vault, f"{dm.TEMPLATES_DIR}/{filename}")
    except (FileNotFoundError, fp.ProjectionPathMissing):
        raise ValidationError(f"template not found: {name}") from None
    except (OSError, fp.FileProjectionError) as e:
        raise ValidationError("invalid template path") from e
    return target

def _load_template_body(svc, name: str) -> str:
    from . import documents as dm
    path = _resolve_template_path(svc, name)
    try:
        _target, data = fp.read_confined_bytes(
            svc.vault, f"{dm.TEMPLATES_DIR}/{path.name}"
        )
        return data.decode("utf-8")
    except (OSError, UnicodeError, fp.FileProjectionError) as e:
        raise ValidationError(f"template not readable: {name}") from e

def toggle_favorite(svc, principal: Principal, path: str) -> dict:
    """Flip whether the current user has pinned this document. Per-user and content-
    neutral: it creates no revision and needs no write permission (a reader may pin
    what they read). Returns the resulting ``favorite`` state."""
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal)
        d = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
        ).fetchone()
        if not d:
            raise NotFoundError("No document at this path.", path=rel)
        existing = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND doc_id=?", (principal.user_id, d["id"])
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM favorites WHERE user_id=? AND doc_id=?",
                (principal.user_id, d["id"]),
            )
            fav = False
        else:
            conn.execute(
                "INSERT OR IGNORE INTO favorites(user_id, doc_id, created_at) VALUES(?,?,?)",
                (principal.user_id, d["id"], now_iso()),
            )
            fav = True
    return {"ok": True, "path": rel, "favorite": fav}

def set_favorite(svc, principal: Principal, path: str, favorite: bool) -> dict:
    """Idempotently set whether the current user has pinned this document (unlike
    toggle_favorite, the resulting state is the one you asked for — friendlier for an
    agent than a flip). Per-user, content-neutral, no write permission required."""
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    with svc.db.writer() as conn:
        svc._fence_principal(conn, principal)
        d = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
        ).fetchone()
        if not d:
            raise NotFoundError("No document at this path.", path=rel)
        if favorite:
            conn.execute(
                "INSERT OR IGNORE INTO favorites(user_id, doc_id, created_at) VALUES(?,?,?)",
                (principal.user_id, d["id"], now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM favorites WHERE user_id=? AND doc_id=?",
                (principal.user_id, d["id"]),
            )
    return {"ok": True, "path": rel, "favorite": favorite}

def is_favorite(svc, user_id: int, path: str) -> bool:
    norm = path_norm(normalize_rel_path(path))
    with svc.db.reader() as conn:
        r = conn.execute(
            "SELECT 1 FROM favorites f JOIN documents d ON d.id=f.doc_id "
            "WHERE f.user_id=? AND d.path_norm=? AND d.is_deleted=0",
            (user_id, norm),
        ).fetchone()
    return r is not None

def list_favorites(svc, user_id: int) -> list[dict]:
    """The user's pinned documents (live only), title-sorted — for the sidebar
    favourites section and a favourites view."""
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT d.path, d.title FROM favorites f JOIN documents d ON d.id=f.doc_id "
            "WHERE f.user_id=? AND d.is_deleted=0 ORDER BY d.title COLLATE NOCASE",
            (user_id,),
        ).fetchall()
        return [{"path": r["path"], "title": r["title"] or r["path"]} for r in rows]

def list_docs(
    svc, folder=None, tag=None, limit=100, offset=0, sort="updated_at", tags=None
) -> list[dict]:
    sort_col = {"updated_at": "updated_at", "title": "title", "path": "path"}.get(
        sort, "updated_at"
    )
    order = "DESC" if sort_col == "updated_at" else "ASC"
    # The correlated subquery resolves each row's latest-revision surface (the
    # idx_revisions_doc(doc_id, version DESC) index makes it a single seek), so
    # the listing can mark which entries an agent/CLI touched last.
    q = (
        "SELECT id, path, title, version, folder, updated_at, "
        "(SELECT via FROM revisions r WHERE r.doc_id=documents.id "
        " ORDER BY r.version DESC LIMIT 1) AS last_via "
        "FROM documents WHERE is_deleted=0"
    )
    params: list = []
    if folder:
        f = folder.strip("/")
        q += " AND (folder=? OR folder LIKE ?)"
        params += [f, f + "/%"]
    for t in svc._tag_filter(tag, tags):
        q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
        params.append(t)
    q += f" ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"
    params += [clamp_int(limit, 1, 1000), max(0, int(offset))]
    out = []
    with svc.db.reader() as conn:
        rows = conn.execute(q, params).fetchall()
        tags_by = svc._tags_for_ids(conn, [r["id"] for r in rows])
        for r in rows:
            out.append(
                {
                    "path": r["path"],
                    "title": r["title"] or r["path"],
                    "version": r["version"],
                    "folder": r["folder"],
                    "tags": tags_by.get(r["id"], []),
                    "updated_at": r["updated_at"],
                    "last_via": r["last_via"],
                }
            )
    return out

def count(svc, folder=None, tag=None, tags=None) -> int:
    """Total non-deleted documents matching the same folder/tag filters as list()."""
    q = "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
    params: list = []
    if folder:
        f = folder.strip("/")
        q += " AND (folder=? OR folder LIKE ?)"
        params += [f, f + "/%"]
    for t in svc._tag_filter(tag, tags):
        q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
        params.append(t)
    with svc.db.reader() as conn:
        return conn.execute(q, params).fetchone()[0]

def complete(svc, q: str, limit: int = 10) -> list[dict]:
    """Path/title prefix-ish matches for wikilink autocomplete."""
    q = (q or "").strip()
    if not q:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with svc.db.reader() as conn:
        rows = conn.execute(
            "SELECT path, title FROM documents WHERE is_deleted=0 AND "
            "(path LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
            "ORDER BY updated_at DESC LIMIT ?",
            (like, like, clamp_int(limit, 1, 25)),
        ).fetchall()
    return [{"path": r["path"], "title": r["title"] or r["path"]} for r in rows]

def preview(svc, path: str, max_chars: int = 240) -> dict:
    """Short plain-text preview for hover popovers: the title plus a leading
    excerpt of the body (frontmatter stripped, heading markers removed). Plain
    text only — the caller renders it as text, never HTML."""
    doc = svc.get(path)
    body = doc["content"][parse_frontmatter(doc["content"])[1] :]
    parts: list[str] = []
    total = 0
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if not s:
            continue
        parts.append(s)
        total += len(s)
        if total >= max_chars:
            break
    return {"path": doc["path"], "title": doc["title"], "excerpt": " ".join(parts)[:max_chars]}
