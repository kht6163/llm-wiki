"""Document service: the optimistic-concurrency write pipeline, revisions, search
index maintenance, link graph, and external-edit reconciliation.

Canonicity: the DB owns version/identity/metadata and the latest revision body is
the durable source of truth for content; the .md file is an atomically-written
projection of it. On a crash between commit and file write, the file is re-projected
from the latest revision (see ``recover_pending``).
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from .. import graph, indexing
from ..db import Database
from ..embedding import Embedder
from ..markdown_utils import derive_title, extract_tags, parse_frontmatter
from ..util import (
    basename_stem,
    folder_of,
    normalize_rel_path,
    now_iso,
    path_norm,
    safe_join,
    sha256_hex,
)
from .auth import Principal
from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError


class DocumentService:
    def __init__(self, db: Database, embedder: Embedder, vault_path: Path | str):
        self.db = db
        self.embedder = embedder
        self.vault = Path(vault_path)

    # ---- helpers --------------------------------------------------------
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
            tags = [t[0] for t in conn.execute(
                "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (d["id"],))]
            return {
                "path": d["path"], "title": d["title"], "content": body,
                "version": d["version"], "tags": tags, "folder": d["folder"],
                "created_at": d["created_at"], "updated_at": d["updated_at"],
                "updated_by": self._username(conn, d["updated_by"]),
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
        q = "SELECT id, path, title, version, folder, updated_at FROM documents WHERE is_deleted=0"
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
            for r in conn.execute(q, params).fetchall():
                tags = [t[0] for t in conn.execute("SELECT tag FROM tags WHERE doc_id=?", (r["id"],))]
                out.append({
                    "path": r["path"], "title": r["title"] or r["path"], "version": r["version"],
                    "folder": r["folder"], "tags": tags, "updated_at": r["updated_at"],
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
                "SELECT r.version, r.op, r.created_at, r.title, u.username AS author "
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
                "SELECT r.version, r.body, r.title, r.op, r.created_at, u.username AS author "
                "FROM revisions r LEFT JOIN users u ON u.id=r.author_id "
                "WHERE r.doc_id=? AND r.version=?",
                (d["id"], int(version)),
            ).fetchone()
            if not r:
                raise NotFoundError(f"No revision {version} for this document.", path=rel)
            return {"path": rel, "version": r["version"], "title": r["title"], "content": r["body"],
                    "op": r["op"], "author": r["author"], "created_at": r["created_at"]}

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
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, created_at) "
                "VALUES(?,?,?,?,?,?, 'create', ?)",
                (doc_id, new_version, content, final_title, chash, principal.user_id, now),
            )
            self._set_tags(conn, doc_id, tagset)
            indexing.reindex_fts(conn, doc_id, final_title, content)
            indexing.rechunk(conn, doc_id, content)
            indexing.reindex_links(conn, doc_id, content, folder)
            graph.backfill_links_for(conn, doc_id, norm, stem)

        mtime = self._write_file(rel, content)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        indexing.embed_doc(self.db, self.embedder, doc_id)
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
            cur = conn.execute(
                "UPDATE documents SET version=version+1, title=?, content_hash=?, folder=?, "
                "file_state='pending', vector_dirty=?, updated_at=?, updated_by=? "
                "WHERE id=? AND version=?",
                (final_title, chash, folder, 1 if content_changed else 0, now,
                 principal.user_id, doc_id, int(base_version)),
            )
            if cur.rowcount == 0:
                raise self._conflict(conn, doc_id, rel)
            new_version = int(base_version) + 1
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, created_at) "
                "VALUES(?,?,?,?,?,?, 'edit', ?)",
                (doc_id, new_version, content, final_title, chash, principal.user_id, now),
            )
            self._set_tags(conn, doc_id, tagset)
            indexing.reindex_fts(conn, doc_id, final_title, content)
            if content_changed:
                indexing.rechunk(conn, doc_id, content)
            indexing.reindex_links(conn, doc_id, content, folder)

        mtime = self._write_file(rel, content)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        if content_changed:
            indexing.embed_doc(self.db, self.embedder, doc_id)
        return self.get(rel)

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
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, created_at) "
                "VALUES(?,?,?,?,?,?, 'delete', ?)",
                (doc_id, new_version, body, row["title"], sha256_hex(body), principal.user_id, now),
            )
            indexing.remove_fts(conn, doc_id)
            indexing.clear_chunks(conn, doc_id)
            graph.unresolve_incoming(conn, doc_id)
            conn.execute("DELETE FROM links WHERE src_doc_id=?", (doc_id,))
        self._trash_file(rel)
        return {"ok": True, "path": rel, "deleted": True}

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
        return len(bodies)

    def reindex_all(self, reembed: bool = False) -> dict:
        """Reconcile the DB with the on-disk vault (handles external edits / new
        files). New files are created; changed files get an 'external-reconcile'
        revision; vanished files are reported (not auto-deleted)."""
        vault = self.vault.resolve()
        seen: set[str] = set()
        created = updated = unchanged = 0
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
                if row and not row["is_deleted"] and row["content_hash"] == chash and not reembed:
                    conn.execute("UPDATE documents SET file_mtime=? WHERE id=?", (mtime, row["id"]))
                    unchanged += 1
                    continue
                if row:
                    doc_id = row["id"]
                    new_version = row["version"] + 1
                    conn.execute(
                        "UPDATE documents SET path=?, title=?, version=?, content_hash=?, folder=?, "
                        "file_state='clean', vector_dirty=1, is_deleted=0, file_mtime=?, updated_at=?, "
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
                    "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, created_at) "
                    "VALUES(?,?,?,?,?,NULL, 'external-reconcile', ?)",
                    (doc_id, new_version, content, title, chash, now),
                )
                self._set_tags(conn, doc_id, tagset)
                indexing.reindex_fts(conn, doc_id, title, content)
                indexing.rechunk(conn, doc_id, content)
                indexing.reindex_links(conn, doc_id, content, folder)
                graph.backfill_links_for(conn, doc_id, norm, stem)
            created += int(is_new)
            updated += int(not is_new)

        with self.db.reader() as conn:
            missing = [r["path"] for r in conn.execute(
                "SELECT path, path_norm FROM documents WHERE is_deleted=0").fetchall()
                if r["path_norm"] not in seen]
        embedded = indexing.embed_pending(self.db, self.embedder)
        return {"created": created, "updated": updated, "unchanged": unchanged,
                "missing_files": missing, "embedded": embedded}
