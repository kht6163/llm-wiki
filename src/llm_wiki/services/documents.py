"""Document service: the optimistic-concurrency write pipeline, revisions, search
index maintenance, link graph, and external-edit reconciliation.

Canonicity: the DB owns version/identity/metadata and the latest revision body is
the durable source of truth for content; the .md file is an atomically-written
projection of it. On a crash between commit and file write, the file is re-projected
from the latest revision (see ``recover_pending``).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

from .. import graph, indexing, search
from ..db import Database
from ..embedding import Embedder
from ..markdown_utils import (
    derive_title,
    extract_tags,
    parse_frontmatter,
    set_frontmatter_tags,
)
from ..metrics import DOC_WRITES
from ..util import (
    basename_stem,
    folder_of,
    normalize_folder_path,
    normalize_rel_path,
    now_iso,
    path_norm,
    safe_join,
    sha256_hex,
)
from . import audit
from .auth import Principal
from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

log = logging.getLogger("llm_wiki.documents")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A markdown task-list line: "- [ ] ...", "* [x] ...", "1. [ ] ..." (captures the
# checkbox state for click-to-toggle). Groups: (prefix, state-char, rest).
_TASK_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$")

# Uploaded images/files live under this vault subdir (excluded from the .md scan).
ATTACH_DIR = "_attachments"
ATTACH_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}


def _locate_section(body: str, heading: str):
    """Find a markdown section by heading text. Returns (lines, start, end, level)
    where start is the heading line index and end is the exclusive index of the
    next heading at the same-or-higher level (the section's subtree), or None."""
    lines = body.splitlines(keepends=True)
    target = heading.strip().lower()
    start: int | None = None
    level = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m and m.group(2).strip().lower() == target:
            start, level = i, len(m.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) <= level:
            end = j
            break
    return lines, start, end, level


def _as_block(text: str) -> str:
    """Normalize inserted text to end with exactly one trailing newline."""
    return text.rstrip("\n") + "\n"


class DocumentService:
    def __init__(self, db: Database, embedder: Embedder, vault_path: Path | str, events=None):
        self.db = db
        self.embedder = embedder
        self.vault = Path(vault_path)
        # Optional EventHub for live change notifications (web WebSocket). None in
        # contexts that don't serve (tests/CLI) -> _emit is a silent no-op.
        self.events = events

    # ---- helpers --------------------------------------------------------
    def _emit(self, op: str, path: str, version: int, **extra) -> None:
        """Best-effort live-change notification, fired after the commit + file write.
        Never raises — a notification failure must not break a write."""
        if self.events is None:
            return
        try:
            self.events.publish({"type": "doc_changed", "op": op, "path": path,
                                 "version": version, **extra})
        except Exception:
            pass

    def _merge_tags(self, meta: dict, content: str, extra: list[str] | None) -> list[str]:
        tags = set(extract_tags(meta, content))
        for x in extra or []:
            if x and str(x).strip():
                tags.add(str(x).strip().lstrip("#"))
        return sorted(tags)

    def _set_tags(self, conn, doc_id: int, tags: list[str]) -> None:
        conn.execute("DELETE FROM tags WHERE doc_id=?", (doc_id,))
        for t in tags:
            conn.execute("INSERT OR IGNORE INTO tags(doc_id, tag) VALUES(?,?)", (doc_id, t))

    def _tags_for_ids(self, conn, ids: list[int]) -> dict[int, list[str]]:
        """One grouped query for many docs' tags, instead of one query per doc."""
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        out: dict[int, list[str]] = {}
        for row in conn.execute(
            f"SELECT doc_id, tag FROM tags WHERE doc_id IN ({ph}) ORDER BY tag", ids
        ):
            out.setdefault(row["doc_id"], []).append(row["tag"])
        return out

    def _latest_body(self, conn, doc_id: int) -> str:
        r = conn.execute(
            "SELECT body FROM revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (doc_id,)
        ).fetchone()
        return r["body"] if r else ""

    def _username(self, conn, uid) -> str | None:
        if uid is None:
            return None
        r = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        return r["username"] if r else None

    def _conflict(self, conn, doc_id: int, rel: str, message: str | None = None) -> ConflictError:
        d = conn.execute(
            "SELECT version, title, updated_by, updated_at FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        body = self._latest_body(conn, doc_id)
        msg = message or (
            f"Update rejected: the document changed since you read it. The current "
            f"version is {d['version']}. Re-read current_content below, reapply your "
            f"change on top of it, and retry with base_version={d['version']}."
        )
        return ConflictError(
            msg, path=rel, current_version=d["version"], current_title=d["title"],
            current_content=body, updated_by=self._username(conn, d["updated_by"]),
            updated_at=d["updated_at"],
        )

    def _write_file(self, rel: str, body: str) -> float:
        target = safe_join(self.vault, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmpdir = self.vault / ".tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp = tmpdir / (uuid.uuid4().hex + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
        return target.stat().st_mtime

    def _trash_file(self, rel: str) -> None:
        src = safe_join(self.vault, rel)
        if src.exists():
            dest = self.vault / ".trash" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)

    # ---- reads ----------------------------------------------------------
    def get(self, path: str) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT * FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not d or d["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            body = self._latest_body(conn, d["id"])
            lv = conn.execute(
                "SELECT via FROM revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (d["id"],)
            ).fetchone()
            tags = [t[0] for t in conn.execute(
                "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (d["id"],))]
            return {
                "path": d["path"], "title": d["title"], "content": body,
                "version": d["version"], "tags": tags, "folder": d["folder"],
                "created_at": d["created_at"], "updated_at": d["updated_at"],
                "updated_by": self._username(conn, d["updated_by"]),
                "last_via": lv["via"] if lv else None,
            }

    def exists(self, path: str) -> bool:
        norm = path_norm(normalize_rel_path(path))
        with self.db.reader() as conn:
            r = conn.execute(
                "SELECT 1 FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
        return r is not None

    def list_docs(self, folder=None, tag=None, limit=100, offset=0, sort="updated_at") -> list[dict]:
        sort_col = {"updated_at": "updated_at", "title": "title", "path": "path"}.get(sort, "updated_at")
        order = "DESC" if sort_col == "updated_at" else "ASC"
        # The correlated subquery resolves each row's latest-revision surface (the
        # idx_revisions_doc(doc_id, version DESC) index makes it a single seek), so
        # the listing can mark which entries an agent/CLI touched last.
        q = ("SELECT id, path, title, version, folder, updated_at, "
             "(SELECT via FROM revisions r WHERE r.doc_id=documents.id "
             " ORDER BY r.version DESC LIMIT 1) AS last_via "
             "FROM documents WHERE is_deleted=0")
        params: list = []
        if folder:
            f = folder.strip("/")
            q += " AND (folder=? OR folder LIKE ?)"
            params += [f, f + "/%"]
        if tag:
            q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
            params.append(tag)
        q += f" ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"
        params += [max(1, min(int(limit), 1000)), max(0, int(offset))]
        out = []
        with self.db.reader() as conn:
            rows = conn.execute(q, params).fetchall()
            tags_by = self._tags_for_ids(conn, [r["id"] for r in rows])
            for r in rows:
                out.append({
                    "path": r["path"], "title": r["title"] or r["path"], "version": r["version"],
                    "folder": r["folder"], "tags": tags_by.get(r["id"], []), "updated_at": r["updated_at"],
                    "last_via": r["last_via"],
                })
        return out

    def count(self, folder=None, tag=None) -> int:
        """Total non-deleted documents matching the same folder/tag filters as list()."""
        q = "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
        params: list = []
        if folder:
            f = folder.strip("/")
            q += " AND (folder=? OR folder LIKE ?)"
            params += [f, f + "/%"]
        if tag:
            q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
            params.append(tag)
        with self.db.reader() as conn:
            return conn.execute(q, params).fetchone()[0]

    def complete(self, q: str, limit: int = 10) -> list[dict]:
        """Path/title prefix-ish matches for wikilink autocomplete."""
        q = (q or "").strip()
        if not q:
            return []
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT path, title FROM documents WHERE is_deleted=0 AND "
                "(path LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\') "
                "ORDER BY updated_at DESC LIMIT ?",
                (like, like, max(1, min(int(limit), 25))),
            ).fetchall()
        return [{"path": r["path"], "title": r["title"] or r["path"]} for r in rows]

    def preview(self, path: str, max_chars: int = 240) -> dict:
        """Short plain-text preview for hover popovers: the title plus a leading
        excerpt of the body (frontmatter stripped, heading markers removed). Plain
        text only — the caller renders it as text, never HTML."""
        doc = self.get(path)
        body = doc["content"][parse_frontmatter(doc["content"])[1]:]
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

    def folders(self) -> list[str]:
        """Distinct non-empty folder paths across non-deleted documents (sorted)."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT DISTINCT folder FROM documents WHERE is_deleted=0 AND folder<>'' "
                "ORDER BY folder"
            ).fetchall()
        return [r[0] for r in rows]

    def folder_counts(self) -> list[tuple[str, int]]:
        """(folder, document count) across ALL non-deleted docs — independent of any
        list page, so the sidebar stays accurate under pagination. Explicitly-created
        empty folders are included with a count of 0."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT folder, COUNT(*) AS n FROM documents WHERE is_deleted=0 AND folder<>'' "
                "GROUP BY folder"
            ).fetchall()
            empties = conn.execute("SELECT path FROM folders").fetchall()
        counts = {r["folder"]: r["n"] for r in rows}
        for e in empties:
            counts.setdefault(e["path"], 0)
        return sorted(counts.items())

    def list_folders(self) -> list[str]:
        """Every folder path that should appear in the tree: folders that hold
        documents, every ancestor of those, and explicitly-created empty folders.
        Sorted, root ('') excluded."""
        paths: set[str] = set()
        with self.db.reader() as conn:
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

    def tree(self) -> dict:
        """Hierarchical folder/document tree for the sidebar file explorer. Combines
        document folders, their ancestors, and explicitly-created empty folders.
        Returns a root node: {name, path, folders:[child nodes], docs:[{path,title}]}
        with folders/docs sorted for stable rendering."""
        with self.db.reader() as conn:
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
                {"path": r["path"], "title": r["title"] or r["path"]})

        def finalize(node: dict) -> dict:
            children = sorted(node["folders"].values(), key=lambda c: c["name"].lower())
            node["folders"] = [finalize(c) for c in children]
            node["docs"].sort(key=lambda d: (d["title"] or "").lower())
            return node

        return finalize(root)

    def create_folder(self, principal: Principal, path: str) -> dict:
        """Persist an (initially empty) folder so it survives with no documents.
        Idempotent-ish: a duplicate raises ConflictError. Projects a real directory
        into the vault to mirror the DB."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot create folders (read/search only).")
        rel = normalize_folder_path(path)
        if not rel:
            raise ValidationError("folder path must not be empty.")
        norm = rel.lower()
        now = now_iso()
        with self.db.writer() as conn:
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
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="folder_create", target=rel)
        safe_join(self.vault, rel).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": rel}

    def delete_folder(self, principal: Principal, path: str) -> dict:
        """Remove an empty folder (and any explicitly-created empty subfolders).
        Refuses if any document still lives under it."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot delete folders (read/search only).")
        rel = normalize_folder_path(path)
        if not rel:
            raise ValidationError("folder path must not be empty.")
        norm = rel.lower()
        with self.db.writer() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_deleted=0 AND (folder=? OR folder LIKE ?)",
                (rel, norm + "/%"),
            ).fetchone()[0]
            if n:
                raise ValidationError(f"Folder is not empty ({n} document(s)); move or delete them first.")
            cur = conn.execute(
                "DELETE FROM folders WHERE path_norm=? OR path_norm LIKE ?", (norm, norm + "/%"))
            if cur.rowcount == 0:
                raise NotFoundError("No such folder.", path=rel)
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="folder_delete", target=rel)
        # Best-effort: prune the now-empty projected directory tree (bottom-up,
        # leaving any directory that still holds stray external files).
        target = safe_join(self.vault, rel)
        if target.is_dir():
            for root_, _dirs, files in os.walk(target, topdown=False):
                if not files:
                    try:
                        os.rmdir(root_)
                    except OSError:
                        pass
        return {"ok": True, "path": rel, "deleted": True}

    def attachment_file(self, subpath: str) -> Path:
        """Resolve an uploaded attachment to a real file under the vault, safely.
        Raises PathError for traversal, NotFoundError if missing."""
        target = safe_join(self.vault / ATTACH_DIR, subpath)
        if not target.is_file():
            raise NotFoundError("No such attachment.", path=subpath)
        return target

    def tags(self) -> list[dict]:
        """Tag vocabulary across non-deleted documents, most-used first."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT t.tag AS tag, COUNT(*) AS count FROM tags t "
                "JOIN documents d ON d.id=t.doc_id WHERE d.is_deleted=0 "
                "GROUP BY t.tag ORDER BY count DESC, t.tag ASC"
            ).fetchall()
        return [{"tag": r["tag"], "count": r["count"]} for r in rows]

    def revisions(self, path: str, limit: int = 100) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT id, version FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            rows = conn.execute(
                "SELECT r.version, r.op, r.via, r.created_at, r.title, u.username AS author "
                "FROM revisions r LEFT JOIN users u ON u.id=r.author_id "
                "WHERE r.doc_id=? ORDER BY r.version DESC LIMIT ?",
                (d["id"], max(1, min(int(limit), 500))),
            ).fetchall()
        return {"path": rel, "current_version": d["version"], "revisions": [dict(r) for r in rows]}

    def revision(self, path: str, version: int) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT id FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            r = conn.execute(
                "SELECT r.version, r.body, r.title, r.op, r.via, r.created_at, u.username AS author "
                "FROM revisions r LEFT JOIN users u ON u.id=r.author_id "
                "WHERE r.doc_id=? AND r.version=?",
                (d["id"], int(version)),
            ).fetchone()
            if not r:
                raise NotFoundError(f"No revision {version} for this document.", path=rel)
            return {"path": rel, "version": r["version"], "title": r["title"], "content": r["body"],
                    "op": r["op"], "via": r["via"], "author": r["author"], "created_at": r["created_at"]}

    def backlinks(self, path: str) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            return {"path": rel, "backlinks": graph.get_backlinks(conn, d["id"])}

    def links(self, path: str) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            return {"path": rel, "links": graph.get_outgoing(conn, d["id"])}

    def graph(self, root=None, depth=1, limit=500, include_unresolved=True) -> dict:
        with self.db.reader() as conn:
            return graph.build_graph(conn, root, depth, limit, include_unresolved)

    def related(self, path: str, limit: int = 8) -> dict:
        """Documents semantically similar to this one (via the shared chunk-vector
        index). Empty list when the document has no embeddings yet."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            items = search.related_documents(conn, d["id"], k=limit)
        return {"path": rel, "related": items}

    def assemble_context(self, question: str, *, max_chars: int = 6000,
                         max_sources: int = 8, mode: str = "hybrid",
                         folder: str | None = None, tags: list[str] | None = None) -> dict:
        """Retrieve + assemble citation-tagged context for a question (RAG primitive)."""
        if not question or not question.strip():
            raise ValidationError("question must not be empty.")
        return search.assemble_context(
            self.db, self.embedder, question, max_chars=max_chars,
            max_sources=max_sources, mode=mode, folder=folder, tags=tags)

    def resolve_link(self, target: str, from_path: str | None = None) -> str | None:
        """Resolve a wikilink/markdown target to an existing document path, or None."""
        src_folder = ""
        if from_path:
            try:
                src_folder = folder_of(normalize_rel_path(from_path))
            except Exception:
                src_folder = ""
        with self.db.reader() as conn:
            return graph.resolve_path(conn, target, src_folder)

    # ---- writes ---------------------------------------------------------
    def create(self, principal: Principal, path: str, content: str,
               title: str | None = None, tags: list[str] | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot create documents (read/search only).")
        rel = normalize_rel_path(path)
        norm, folder, stem = path_norm(rel), folder_of(rel), basename_stem(rel).lower()
        content = content or ""
        meta = parse_frontmatter(content)[0]
        final_title = (title or derive_title(meta, content, rel)).strip()
        tagset = self._merge_tags(meta, content, tags)
        chash, now = sha256_hex(content), now_iso()

        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, version, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if row and not row["is_deleted"]:
                raise self._conflict(conn, row["id"], rel,
                                     message="A document already exists at this path.")
            if row and row["is_deleted"]:  # revive a tombstone
                doc_id = row["id"]
                new_version = row["version"] + 1
                conn.execute(
                    "UPDATE documents SET path=?, title=?, version=?, content_hash=?, folder=?, "
                    "file_state='pending', vector_dirty=1, is_deleted=0, updated_at=?, updated_by=? "
                    "WHERE id=?",
                    (rel, final_title, new_version, chash, folder, now, principal.user_id, doc_id),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO documents(path, path_norm, title, version, content_hash, folder, "
                    "file_state, vector_dirty, is_deleted, created_at, created_by, updated_at, updated_by) "
                    "VALUES(?,?,?,?,?,?, 'pending', 1, 0, ?,?,?,?)",
                    (rel, norm, final_title, 1, chash, folder, now, principal.user_id, now, principal.user_id),
                )
                doc_id, new_version = cur.lastrowid, 1
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                "VALUES(?,?,?,?,?,?, 'create', ?, ?)",
                (doc_id, new_version, content, final_title, chash, principal.user_id, principal.via, now),
            )
            self._set_tags(conn, doc_id, tagset)
            indexing.reindex_fts(conn, doc_id, final_title, content)
            indexing.rechunk(conn, doc_id, content)
            indexing.reindex_links(conn, doc_id, content, folder)
            graph.backfill_links_for(conn, doc_id, norm, stem)
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_create", target=rel, detail=f"v{new_version}")

        mtime = self._write_file(rel, content)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        indexing.embed_doc(self.db, self.embedder, doc_id)
        DOC_WRITES.labels("create").inc()
        self._emit("create", rel, new_version, title=final_title,
                   updated_by=principal.username, via=principal.via)
        return self.get(rel)

    def update(self, principal: Principal, path: str, base_version: int, content: str,
               title: str | None = None, tags: list[str] | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only).")
        if base_version is None:
            raise ValidationError("base_version is required for updates.")
        rel = normalize_rel_path(path)
        norm, folder = path_norm(rel), folder_of(rel)
        content = content or ""
        meta = parse_frontmatter(content)[0]
        final_title = (title or derive_title(meta, content, rel)).strip()
        tagset = self._merge_tags(meta, content, tags)
        chash, now = sha256_hex(content), now_iso()

        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, version, content_hash, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not row or row["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            doc_id = row["id"]
            content_changed = row["content_hash"] != chash
            # vector_dirty moves monotonically toward dirty: a content change forces
            # 1, but an unchanged-content edit must NOT clear a pending flag — doing so
            # would cancel an embedding that reindex queued (vector_dirty=1, no vectors
            # yet) and the doc would silently vanish from vector search forever.
            cur = conn.execute(
                "UPDATE documents SET version=version+1, title=?, content_hash=?, folder=?, "
                "file_state='pending', vector_dirty=CASE WHEN ? THEN 1 ELSE vector_dirty END, "
                "updated_at=?, updated_by=? WHERE id=? AND version=?",
                (final_title, chash, folder, 1 if content_changed else 0, now,
                 principal.user_id, doc_id, int(base_version)),
            )
            if cur.rowcount == 0:
                raise self._conflict(conn, doc_id, rel)
            new_version = int(base_version) + 1
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                "VALUES(?,?,?,?,?,?, 'edit', ?, ?)",
                (doc_id, new_version, content, final_title, chash, principal.user_id, principal.via, now),
            )
            self._set_tags(conn, doc_id, tagset)
            indexing.reindex_fts(conn, doc_id, final_title, content)
            if content_changed:
                indexing.rechunk(conn, doc_id, content)
            indexing.reindex_links(conn, doc_id, content, folder)
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_update", target=rel, detail=f"v{new_version}")

        mtime = self._write_file(rel, content)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        if content_changed:
            indexing.embed_doc(self.db, self.embedder, doc_id)
        DOC_WRITES.labels("update").inc()
        self._emit("update", rel, new_version, title=final_title,
                   updated_by=principal.username, via=principal.via,
                   content_changed=content_changed)
        return self.get(rel)

    # ---- targeted edits (token-cheap; funnel through the CAS update path) ----
    def get_section(self, path: str, heading: str) -> dict:
        doc = self.get(path)
        loc = _locate_section(doc["content"], heading)
        if not loc:
            raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])
        lines, start, end, _ = loc
        return {"path": doc["path"], "heading": heading, "version": doc["version"],
                "tags": doc["tags"], "content": "".join(lines[start:end])}

    def outline(self, path: str) -> dict:
        """Flat heading outline of a document: [{level, text, line}] (1-based lines).
        Lets an agent discover exact heading strings before a section read/edit."""
        doc = self.get(path)
        headings: list[dict] = []
        for i, line in enumerate(doc["content"].splitlines()):
            m = _HEADING_RE.match(line)
            if m:
                headings.append({"level": len(m.group(1)), "text": m.group(2).strip(), "line": i + 1})
        return {"path": doc["path"], "version": doc["version"], "headings": headings}

    def replace_section(self, principal: Principal, path: str, heading: str, text: str,
                        base_version: int | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        loc = _locate_section(doc["content"], heading)
        if not loc:
            raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])
        lines, start, end, _ = loc
        # Keep the heading line; replace its body up to the next same/higher heading.
        body = "".join(lines[:start + 1]) + _as_block(text) + "".join(lines[end:])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, body)

    def append_section(self, principal: Principal, path: str, heading: str, text: str,
                       base_version: int | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        loc = _locate_section(doc["content"], heading)
        if not loc:
            raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])
        lines, start, end, _ = loc
        head = "".join(lines[:end])
        # Guarantee a line boundary: a final section whose last line has no trailing
        # newline would otherwise glue the appended block onto that line.
        if head and not head.endswith("\n"):
            head += "\n"
        body = head + _as_block(text) + "".join(lines[end:])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, body)

    def patch(self, principal: Principal, path: str, find: str, replace: str,
              base_version: int | None = None, count: int = 1) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        if not find:
            raise ValidationError("'find' text is required.")
        doc = self.get(path)
        occurrences = doc["content"].count(find)
        if occurrences == 0:
            raise NotFoundError("Search text not found; nothing patched.", path=doc["path"])
        if count and occurrences > count:
            raise ValidationError(
                f"Search text appears {occurrences} times (limit {count}); make it more "
                f"specific or raise 'count'.")
        new_body = doc["content"].replace(find, replace, count if count else -1)
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, new_body)

    def toggle_task(self, principal: Principal, path: str, line: int | None = None,
                    *, index: int | None = None, base_version: int | None = None) -> dict:
        """Flip a single markdown task checkbox (``- [ ]`` <-> ``- [x]``), then save
        through the CAS update path. Target by 1-based ``line`` or by 0-based
        ``index`` (the Nth checkbox in document order — matches the rendered
        ``data-ti`` attribute used by click-to-toggle in the viewer)."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        lines = doc["content"].split("\n")
        if index is not None:
            tasks = [i for i, ln in enumerate(lines) if _TASK_LINE_RE.match(ln)]
            if int(index) < 0 or int(index) >= len(tasks):
                raise ValidationError("task index is out of range.")
            idx = tasks[int(index)]
        elif line is not None:
            idx = int(line) - 1
        else:
            raise ValidationError("line or index is required.")
        if idx < 0 or idx >= len(lines):
            raise ValidationError("line is out of range.")
        m = _TASK_LINE_RE.match(lines[idx])
        if not m:
            raise ValidationError("no task checkbox on that line.")
        lines[idx] = m.group(1) + (" " if m.group(2).lower() == "x" else "x") + m.group(3)
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, "\n".join(lines))

    def patch_tags(self, principal: Principal, path: str, add: list[str] | None = None,
                   remove: list[str] | None = None) -> dict:
        """Add/remove tags by rewriting the frontmatter ``tags`` list (body untouched).
        Returns the document's resulting tags. Tags written inline as ``#hashtags``
        in the body are re-derived on save, so they cannot be removed this way."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        current = set(doc["tags"])
        add_set = {str(t).strip().lstrip("#") for t in (add or []) if str(t).strip()}
        remove_set = {str(t).strip().lstrip("#") for t in (remove or []) if str(t).strip()}
        target = sorted((current | add_set) - remove_set)
        if target == sorted(current):  # no net change — stay idempotent, skip the version bump
            return {"path": doc["path"], "version": doc["version"], "tags": sorted(current)}
        new_content = set_frontmatter_tags(doc["content"], target)
        updated = self.update(principal, doc["path"], doc["version"], new_content)
        return {"path": updated["path"], "version": updated["version"], "tags": updated["tags"]}

    def broken_links(self, limit: int = 200) -> dict:
        """Vault-wide unresolved links (dangling references) for cleanup tooling."""
        limit = max(1, min(int(limit), 2000))
        with self.db.reader() as conn:
            items = graph.list_broken_links(conn, limit)
        return {"count": len(items), "links": items}

    def move(self, principal: Principal, path: str, new_path: str) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot move documents (read/search only).")
        rel, new_rel = normalize_rel_path(path), normalize_rel_path(new_path)
        norm, new_norm = path_norm(rel), path_norm(new_rel)
        if norm == new_norm:
            return self.get(rel)
        new_folder, new_stem = folder_of(new_rel), basename_stem(new_rel).lower()
        now = now_iso()
        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, version, title, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not row or row["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            clash = conn.execute(
                "SELECT 1 FROM documents WHERE path_norm=?", (new_norm,)).fetchone()
            if clash:
                raise ConflictError("The destination path is already occupied.", path=new_rel)
            doc_id, new_version = row["id"], row["version"] + 1
            body = self._latest_body(conn, doc_id)
            conn.execute(
                "UPDATE documents SET path=?, path_norm=?, folder=?, version=version+1, "
                "file_state='pending', updated_at=?, updated_by=? WHERE id=?",
                (new_rel, new_norm, new_folder, now, principal.user_id, doc_id),
            )
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                "VALUES(?,?,?,?,?,?, 'rename', ?, ?)",
                (doc_id, new_version, body, row["title"], sha256_hex(body), principal.user_id, principal.via, now),
            )
            # Incoming links that resolved to the old path/name are now stale; drop
            # their resolution and re-resolve anything pointing at the new path/name.
            graph.unresolve_incoming(conn, doc_id)
            graph.backfill_links_for(conn, doc_id, new_norm, new_stem)
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_move", target=f"{rel} -> {new_rel}")
        self._trash_file(rel)
        mtime = self._write_file(new_rel, body)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        DOC_WRITES.labels("move").inc()
        # Keyed on the OLD path so a viewer of the moved doc can follow it to `to`.
        self._emit("move", rel, new_version, to=new_rel,
                   updated_by=principal.username, via=principal.via)
        return self.get(new_rel)

    def recent_changes(self, limit: int = 20, since: str | None = None,
                       until: str | None = None) -> list[dict]:
        """Most-recently-updated non-deleted documents, optionally bounded by an
        ISO-8601 updated_at window (e.g. '2026-06-01')."""
        q = "SELECT id, path, title, version, folder, updated_at FROM documents WHERE is_deleted=0"
        params: list = []
        if since:
            q += " AND updated_at >= ?"
            params.append(since)
        if until:
            q += " AND updated_at <= ?"
            params.append(until)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self.db.reader() as conn:
            rows = conn.execute(q, params).fetchall()
            tags_by = self._tags_for_ids(conn, [r["id"] for r in rows])
            return [{
                "path": r["path"], "title": r["title"] or r["path"], "version": r["version"],
                "folder": r["folder"], "tags": tags_by.get(r["id"], []), "updated_at": r["updated_at"],
            } for r in rows]

    def delete(self, principal: Principal, path: str, base_version: int | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot delete documents (read/search only).")
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        now = now_iso()
        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, version, title, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not row or row["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            doc_id = row["id"]
            if base_version is not None and int(base_version) != row["version"]:
                raise self._conflict(conn, doc_id, rel)
            body = self._latest_body(conn, doc_id)
            new_version = row["version"] + 1
            conn.execute(
                "UPDATE documents SET is_deleted=1, version=version+1, file_state='clean', "
                "vector_dirty=0, updated_at=?, updated_by=? WHERE id=?",
                (now, principal.user_id, doc_id),
            )
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                "VALUES(?,?,?,?,?,?, 'delete', ?, ?)",
                (doc_id, new_version, body, row["title"], sha256_hex(body), principal.user_id, principal.via, now),
            )
            indexing.remove_fts(conn, doc_id)
            indexing.clear_chunks(conn, doc_id)
            graph.unresolve_incoming(conn, doc_id)
            conn.execute("DELETE FROM links WHERE src_doc_id=?", (doc_id,))
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_delete", target=rel, detail=f"v{new_version}")
        self._trash_file(rel)
        DOC_WRITES.labels("delete").inc()
        self._emit("delete", rel, new_version,
                   updated_by=principal.username, via=principal.via)
        return {"ok": True, "path": rel, "deleted": True}

    def save_attachment(self, principal: Principal, filename: str, data: bytes) -> dict:
        """Store an uploaded image/file under the vault's _attachments dir and return
        a markdown snippet to embed it. Content-addressed name (sha8) dedups
        identical uploads and avoids collisions. Type/size are validated."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot upload attachments.")
        name = (filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in ALLOWED_ATTACH_EXTS:
            raise ValidationError(
                f"Unsupported attachment type {ext or '(none)'!r}; allowed: "
                f"{', '.join(sorted(ALLOWED_ATTACH_EXTS))}.")
        if not data:
            raise ValidationError("Empty upload.")
        if len(data) > ATTACH_MAX_BYTES:
            raise ValidationError(
                f"Attachment too large ({len(data)} bytes; limit {ATTACH_MAX_BYTES}).")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name[: len(name) - len(ext)]).strip("-_.") or "file"
        digest = hashlib.sha256(data).hexdigest()[:8]
        sub = f"{stem}-{digest}{ext}"
        target = safe_join(self.vault / ATTACH_DIR, sub)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():  # content-addressed: skip rewrite of an identical file
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        url = "/attachments/" + quote(sub)
        return {"path": f"{ATTACH_DIR}/{sub}", "url": url, "markdown": f"![{stem}]({url})"}

    # ---- maintenance ----------------------------------------------------
    def recover_pending(self) -> int:
        """Re-project any documents left in file_state='pending' by a crash."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT id, path FROM documents WHERE file_state='pending' AND is_deleted=0"
            ).fetchall()
            bodies = {r["id"]: (r["path"], self._latest_body(conn, r["id"])) for r in rows}
        for doc_id, (rel, body) in bodies.items():
            mtime = self._write_file(rel, body)
            with self.db.writer() as conn:
                conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        if bodies:
            log.info("recover_pending: re-projected %d file(s) from the latest revision", len(bodies))
        return len(bodies)

    def embed_pending(self) -> int:
        """Embed any documents still flagged ``vector_dirty`` (no-op when none are).
        A crash can commit a write — version bumped, ``vector_dirty=1`` — but die before
        the post-commit embed (it runs off the write lock), leaving the doc absent from
        vector search until the next ``reindex --reembed``. Sweeping on startup closes
        that gap. Also catches docs left ``file_state='clean'`` but unembedded, which
        ``recover_pending`` (file-state only) does not see."""
        return indexing.embed_pending(self.db, self.embedder)

    def reindex_all(self, reembed: bool = False) -> dict:
        """Reconcile the DB with the on-disk vault (handles external edits / new
        files). New files are created; changed files get an 'external-reconcile'
        revision; vanished files are reported (not auto-deleted)."""
        vault = self.vault.resolve()
        log.info("reindex: scanning vault %s (reembed=%s)", vault, reembed)
        seen: set[str] = set()
        created = updated = unchanged = 0
        skipped_deleted: list[str] = []
        now = now_iso()
        for p in sorted(vault.rglob("*.md")):
            try:
                relp = p.resolve().relative_to(vault)
            except ValueError:
                continue
            if relp.parts and relp.parts[0] in (".trash", ".tmp"):
                continue
            rel = "/".join(relp.parts)
            norm, folder, stem = path_norm(rel), folder_of(rel), basename_stem(rel).lower()
            seen.add(norm)
            content = p.read_text(encoding="utf-8", errors="replace")
            chash = sha256_hex(content)
            meta = parse_frontmatter(content)[0]
            title = derive_title(meta, content, rel)
            tagset = self._merge_tags(meta, content, None)
            mtime = p.stat().st_mtime
            with self.db.writer() as conn:
                row = conn.execute(
                    "SELECT id, version, content_hash, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
                if row and row["is_deleted"]:
                    # A soft delete is an explicit, recorded intent and the DB is the
                    # canonical owner of deletion. An external .md reappearing must NOT
                    # silently undo it — report it and leave the tombstone in place.
                    skipped_deleted.append(rel)
                    audit.record(conn, actor=None, via="cli", action="doc_reconcile_skip",
                                 target=rel, outcome="skipped",
                                 detail="deleted document still present on disk")
                    continue
                if row and row["content_hash"] == chash and not reembed:
                    conn.execute("UPDATE documents SET file_mtime=? WHERE id=?", (mtime, row["id"]))
                    unchanged += 1
                    continue
                if row:
                    doc_id = row["id"]
                    new_version = row["version"] + 1
                    conn.execute(
                        "UPDATE documents SET path=?, title=?, version=?, content_hash=?, folder=?, "
                        "file_state='clean', vector_dirty=1, file_mtime=?, updated_at=?, "
                        "updated_by=NULL WHERE id=?",
                        (rel, title, new_version, chash, folder, mtime, now, doc_id),
                    )
                    is_new = False
                else:
                    cur = conn.execute(
                        "INSERT INTO documents(path, path_norm, title, version, content_hash, folder, "
                        "file_state, vector_dirty, is_deleted, file_mtime, created_at, created_by, updated_at, updated_by) "
                        "VALUES(?,?,?,?,?,?, 'clean', 1, 0, ?, ?, NULL, ?, NULL)",
                        (rel, norm, title, 1, chash, folder, mtime, now, now),
                    )
                    doc_id, new_version, is_new = cur.lastrowid, 1, True
                conn.execute(
                    "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                    "VALUES(?,?,?,?,?,NULL, 'external-reconcile', 'cli', ?)",
                    (doc_id, new_version, content, title, chash, now),
                )
                self._set_tags(conn, doc_id, tagset)
                indexing.reindex_fts(conn, doc_id, title, content)
                indexing.rechunk(conn, doc_id, content)
                indexing.reindex_links(conn, doc_id, content, folder)
                graph.backfill_links_for(conn, doc_id, norm, stem)
                # External reconciliation is otherwise a silent batch operation; record
                # who/when so "this file changed outside the app" stays auditable, like
                # every other write path.
                audit.record(conn, actor=None, via="cli", action="doc_reconcile", target=rel,
                             detail=f"v{new_version} {'create' if is_new else 'update'}")
            created += int(is_new)
            updated += int(not is_new)

        with self.db.reader() as conn:
            missing = [r["path"] for r in conn.execute(
                "SELECT path, path_norm FROM documents WHERE is_deleted=0").fetchall()
                if r["path_norm"] not in seen]
        embedded = indexing.embed_pending(self.db, self.embedder)
        log.info("reindex: created=%d updated=%d unchanged=%d skipped_deleted=%d "
                 "missing_files=%d embedded=%d", created, updated, unchanged,
                 len(skipped_deleted), len(missing), embedded)
        return {"created": created, "updated": updated, "unchanged": unchanged,
                "missing_files": missing, "skipped_deleted": skipped_deleted,
                "embedded": embedded}
