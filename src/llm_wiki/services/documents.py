"""Document service: the optimistic-concurrency write pipeline, revisions, search
index maintenance, link graph, and external-edit reconciliation.

Canonicity: the DB owns version/identity/metadata and the latest revision body is
the durable source of truth for content; the .md file is an atomically-written
projection of it. On a crash between commit and file write, the file is re-projected
from the latest revision (see ``recover_pending``).
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import logging
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .. import graph, indexing, search
from ..db import Database
from ..embedding import Embedder
from ..markdown_utils import (
    SCHEME_RE,
    _mask,
    derive_title,
    document_properties,
    extract_links,
    extract_tags,
    heading_slug,
    parse_frontmatter,
    remove_frontmatter_property,
    rewrite_link_target,
    set_frontmatter_property,
    set_frontmatter_tags,
)
from ..metrics import DOC_WRITES
from ..util import (
    PathError,
    basename_stem,
    clamp_int,
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
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    WikiError,
)

log = logging.getLogger("llm_wiki.documents")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A markdown task-list line: "- [ ] ...", "* [x] ...", "1. [ ] ..." (captures the
# checkbox state for click-to-toggle). Groups: (prefix, state-char, rest).
_TASK_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Uploaded images/files live under this vault subdir (excluded from the .md scan).
ATTACH_DIR = "_attachments"
ATTACH_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}

# ---- bulk import (Obsidian/markdown directory ingest) ----------------------
IMPORT_MAX_BYTES = 50 * 1024 * 1024          # per-file ceiling for one note
IMPORT_DEFAULT_INCLUDE = ("*.md", "*.markdown", "*.mdown", "*.mkd")
IMPORT_MD_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
# Directories that legitimately appear inside an external vault but must never be
# ingested (app/editor metadata, VCS, dependency trees, our own scratch/trash).
IMPORT_EXCLUDED_DIRS = {".obsidian", ".trash", ".tmp", ".git", ".venv",
                        "node_modules", "__pycache__"}
# Assets the importer may copy when --import-attachments is on (same allow-list as
# interactive uploads, so they pass save_attachment's validation unchanged).
IMPORT_ATTACH_EXTS = ALLOWED_ATTACH_EXTS

# Obsidian embed `![[target]]` (incl. `![[a.png|300]]`, `![[note#heading]]`).
_EMBED_RE = re.compile(r"!\[\[([^\[\]\n]+?)\]\]")
# Standard markdown image `![alt](url "title")`.
_IMG_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")

# How long an idempotency key stays replayable. Retries happen within seconds, so a
# week is generous; older rows are swept opportunistically on each keyed write to
# bound the ledger's growth.
_IDEM_RETENTION_DAYS = 7


def _attachment_subname(name: str, ext: str, data: bytes) -> str:
    """Content-addressed ``<stem>-<sha8><ext>`` filename for a stored attachment.
    Shared by interactive uploads and the bulk importer so both name files the same
    way (and so an importer dry-run can predict the exact target without writing)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name[: len(name) - len(ext)]).strip("-_.") or "file"
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"{stem}-{digest}{ext}"


def _replace_outside_code(pattern: re.Pattern, repl: Callable[[re.Match], str], text: str) -> str:
    """Like ``pattern.sub(repl, text)`` but skips matches inside fenced/inline code and
    frontmatter — match positions are found against a code-masked copy, then applied to
    the original text. Used by the importer so normalizing Obsidian ``![[embeds]]`` never
    rewrites a literal embed shown inside a code block (matches markdown_utils' masking)."""
    masked = _mask(text)
    out: list[str] = []
    last = 0
    for m in pattern.finditer(masked):
        out.append(text[last:m.start()])
        out.append(repl(m))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _heading_matches(body: str):
    """All headings whose text equals nothing in particular — returns (lines, matches)
    where matches is [(line_index, level, text)] for every heading line, in order.
    The basis for section location and occurrence disambiguation."""
    lines = body.splitlines(keepends=True)
    matches: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m:
            matches.append((i, len(m.group(1)), m.group(2).strip()))
    return lines, matches


def _count_sections(body: str, heading: str) -> int:
    """How many headings carry this exact text — used to report occurrence range."""
    target = heading.strip().lower()
    _lines, matches = _heading_matches(body)
    return sum(1 for _i, _lvl, text in matches if text.lower() == target)


def _locate_section(body: str, heading: str, occurrence: int = 1):
    """Find a markdown section by heading text. ``occurrence`` (1-based) selects the
    Nth heading with that text, so repeated headings (e.g. several "예시"/"Notes") can
    be edited unambiguously instead of always hitting the first. Returns
    (lines, start, end, level) where start is the heading line index and end is the
    exclusive index of the next heading at the same-or-higher level (the section's
    subtree), or None when there's no such heading / the occurrence is out of range."""
    target = heading.strip().lower()
    occ = max(1, int(occurrence))
    lines, matches = _heading_matches(body)
    hits = [(i, lvl) for i, lvl, text in matches if text.lower() == target]
    if occ > len(hits):
        return None
    start, level = hits[occ - 1]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) <= level:
            end = j
            break
    return lines, start, end, level


def _locate_or_raise(doc: dict, heading: str, occurrence: int):
    """Locate a section or raise a precise error: ValidationError when the heading
    exists but the requested occurrence is out of range (so an agent learns the actual
    count instead of silently editing the wrong one), NotFoundError when nothing matches."""
    loc = _locate_section(doc["content"], heading, occurrence)
    if loc:
        return loc
    n = _count_sections(doc["content"], heading)
    if n and occurrence > n:
        raise ValidationError(
            f"occurrence {occurrence} is out of range: the document has {n} "
            f"section(s) titled {heading!r}.", path=doc["path"])
    raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])


def _as_block(text: str) -> str:
    """Normalize inserted text to end with exactly one trailing newline."""
    return text.rstrip("\n") + "\n"


class DocumentService:
    def __init__(self, db: Database, embedder: Embedder, vault_path: Path | str, events=None,
                 search_params: search.FusionParams | None = None,
                 embed_worker: indexing.EmbeddingWorker | None = None):
        self.db = db
        self.embedder = embedder
        self.vault = Path(vault_path)
        # Optional EventHub for live change notifications (web WebSocket). None in
        # contexts that don't serve (tests/CLI) -> _emit is a silent no-op.
        self.events = events
        # Hybrid-search fusion tuning (from Settings); defaults match the old constants.
        self.search_params = search_params or search.DEFAULT_FUSION
        # When set (serving), writes only flag vector_dirty + notify this worker, so the
        # slow embedding forward pass runs off the request path. None (tests/CLI) -> embed
        # inline so a write is immediately visible to vector search.
        self.embed_worker = embed_worker
        # Sidebar nav cache (file tree + top tags), invalidated via a generation counter
        # bumped on every structural write. See nav_tree()/nav_tags()/_bump_nav().
        self._nav_lock = threading.Lock()
        self._nav_gen = 0
        self._nav_cache_gen = -1
        self._nav_tree: dict | None = None
        self._nav_tags: list[dict] | None = None

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

    def _embed(self, doc_id: int) -> None:
        """Embed a document's chunks after a write. With a background worker (serving),
        flag-and-notify only — ``vector_dirty`` was already set in the write txn — so the
        slow forward pass is off the request path; without one (tests/CLI), embed inline
        so the write is immediately visible to vector search."""
        if self.embed_worker is not None:
            self.embed_worker.notify()
        else:
            indexing.embed_doc(self.db, self.embedder, doc_id)

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
        # The surface of the COMPETING edit: an agent that loses a CAS race can use this
        # to decide whether to back off (current_via='web' -> a human is editing) or
        # rebase-and-retry (current_via='mcp'/'cli' -> another agent/import).
        lv = conn.execute(
            "SELECT via FROM revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (doc_id,)
        ).fetchone()
        msg = message or (
            f"Update rejected: the document changed since you read it. The current "
            f"version is {d['version']}. Re-read current_content below, reapply your "
            f"change on top of it, and retry with base_version={d['version']}."
        )
        return ConflictError(
            msg, path=rel, current_version=d["version"], current_title=d["title"],
            current_content=body, updated_by=self._username(conn, d["updated_by"]),
            updated_at=d["updated_at"], current_via=lv["via"] if lv else None,
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

    # ---- idempotency ----------------------------------------------------
    def _idem_lookup(self, scope: str, user_id: int, key: str) -> dict | None:
        """Return the cached result of a previously-applied write with this
        (scope, user, key), or None if the key is new. Lets a client safely retry
        a write whose response was lost without applying it twice."""
        with self.db.reader() as conn:
            row = conn.execute(
                "SELECT result_path, result_version FROM idempotency_keys "
                "WHERE scope=? AND user_id=? AND idem_key=?",
                (scope, user_id, key)).fetchone()
        if row is None:
            return None
        return {"ok": True, "path": row["result_path"],
                "version": row["result_version"], "deduplicated": True}

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

    def info(self, path: str) -> dict:
        """Document metadata WITHOUT the body — a cheap poll for an agent to check
        ``version``/``updated_by``/``last_via`` before deciding to re-read or rebase.
        Same shape as ``get()`` minus ``content``; skips loading the (possibly large)
        latest-revision body, so polling 'has this changed since version N' is cheap."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT * FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not d or d["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            lv = conn.execute(
                "SELECT via FROM revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (d["id"],)
            ).fetchone()
            tags = [t[0] for t in conn.execute(
                "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (d["id"],))]
            return {
                "path": d["path"], "title": d["title"], "version": d["version"],
                "tags": tags, "folder": d["folder"], "created_at": d["created_at"],
                "updated_at": d["updated_at"], "updated_by": self._username(conn, d["updated_by"]),
                "last_via": lv["via"] if lv else None,
            }

    def read_chunk(self, path: str, ordinal: int, *, before: int = 0, after: int = 0) -> dict:
        """Read one indexed chunk by ``ordinal``, optionally with neighbouring chunks.

        Chunks are the very passages the hybrid retriever matches, so an agent that
        got a ``chunk_ordinal`` from search can pull exactly that section — plus
        ``before``/``after`` neighbours for context — without re-fetching the whole
        document. Returns the joined ``text``, the per-chunk breakdown, the total
        ``chunk_count``, and ``has_before``/``has_after`` so a reader can page outward.
        """
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        before = clamp_int(before, 0, 20)
        after = clamp_int(after, 0, 20)
        with self.db.reader() as conn:
            d = conn.execute(
                "SELECT id, path, version FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            total = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id=?", (d["id"],)
            ).fetchone()[0]
            lo = max(0, int(ordinal) - before)
            hi = int(ordinal) + after
            rows = conn.execute(
                "SELECT ordinal, heading, heading_path, text, char_start, char_end "
                "FROM chunks WHERE doc_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                (d["id"], lo, hi),
            ).fetchall()
            if not rows:
                raise NotFoundError(
                    "No chunk at this ordinal." if total else "Document has no indexed chunks.",
                    path=rel, ordinal=int(ordinal), chunk_count=total,
                )
            chunks = [{
                "ordinal": r["ordinal"], "heading": r["heading"],
                "heading_path": r["heading_path"], "text": r["text"],
                "char_start": r["char_start"], "char_end": r["char_end"],
                "anchor": heading_slug(r["heading"]) if r["heading"] else None,
            } for r in rows]
            return {
                "path": d["path"], "version": d["version"],
                "ordinal": int(ordinal), "chunk_count": total,
                "char_start": chunks[0]["char_start"], "char_end": chunks[-1]["char_end"],
                "has_before": chunks[0]["ordinal"] > 0,
                "has_after": chunks[-1]["ordinal"] < total - 1,
                "text": "\n\n".join(c["text"] for c in chunks),
                "chunks": chunks,
            }

    def exists(self, path: str) -> bool:
        norm = path_norm(normalize_rel_path(path))
        with self.db.reader() as conn:
            r = conn.execute(
                "SELECT 1 FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
        return r is not None

    def list_docs(self, folder=None, tag=None, limit=100, offset=0, sort="updated_at",
                  tags=None) -> list[dict]:
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
        for t in self._tag_filter(tag, tags):
            q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
            params.append(t)
        q += f" ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"
        params += [clamp_int(limit, 1, 1000), max(0, int(offset))]
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

    @staticmethod
    def _tag_filter(tag=None, tags=None) -> list[str]:
        """Normalize the single ``tag`` and the multi ``tags`` arguments into one
        de-duplicated, order-stable list of tags that must ALL be present (AND)."""
        out: list[str] = []
        for t in ([tag] if tag else []) + list(tags or []):
            if t and t not in out:
                out.append(t)
        return out

    def count(self, folder=None, tag=None, tags=None) -> int:
        """Total non-deleted documents matching the same folder/tag filters as list()."""
        q = "SELECT COUNT(*) FROM documents WHERE is_deleted=0"
        params: list = []
        if folder:
            f = folder.strip("/")
            q += " AND (folder=? OR folder LIKE ?)"
            params += [f, f + "/%"]
        for t in self._tag_filter(tag, tags):
            q += " AND id IN (SELECT doc_id FROM tags WHERE tag=?)"
            params.append(t)
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
                (like, like, clamp_int(limit, 1, 25)),
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
        self._bump_nav()
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
        self._bump_nav()
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

    # ---- sidebar nav cache ----------------------------------------------
    # render() builds the file tree + top-tag list on EVERY authenticated page; both are
    # full scans. Cache a snapshot keyed to a generation counter bumped on each structural
    # write, so the scan is paid once per write instead of once per page (also speeds the
    # /api/tree live-refresh shell.js fires after every mutation). The public tree()/tags()
    # stay uncached so /tags and /api/tree always read canonical DB. Thread-safe: web + MCP
    # share one DocumentService across threads.
    def _bump_nav(self) -> None:
        with self._nav_lock:
            self._nav_gen += 1

    def _top_tags(self, n: int) -> list[dict]:
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT t.tag AS tag, COUNT(*) AS count FROM tags t "
                "JOIN documents d ON d.id=t.doc_id WHERE d.is_deleted=0 "
                "GROUP BY t.tag ORDER BY count DESC, t.tag ASC LIMIT ?", (n,)
            ).fetchall()
        return [{"tag": r["tag"], "count": r["count"]} for r in rows]

    def _ensure_nav(self) -> None:
        with self._nav_lock:
            if self._nav_cache_gen != self._nav_gen or self._nav_tree is None:
                self._nav_tree = self.tree()
                self._nav_tags = self._top_tags(40)
                self._nav_cache_gen = self._nav_gen

    def nav_tree(self) -> dict:
        """Cached sidebar file tree (rebuilt lazily after a structural write)."""
        self._ensure_nav()
        assert self._nav_tree is not None
        return self._nav_tree

    def nav_tags(self) -> list[dict]:
        """Cached sidebar top-40 tag list (rebuilt lazily after a structural write)."""
        self._ensure_nav()
        assert self._nav_tags is not None
        return self._nav_tags

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
                (d["id"], clamp_int(limit, 1, 500)),
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

    def compare_revisions(self, path: str, from_version: int, to_version: int) -> dict:
        """Unified line diff between two revisions, computed server-side (the bodies
        never travel to the caller). Returns classified diff lines (hunk/add/del/ctx)
        + a change summary — so an agent auditing edits doesn't fetch two full bodies
        and diff them itself. Mirrors the web /diff view."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute("SELECT id FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            rows = {r["version"]: r for r in conn.execute(
                "SELECT version, body, title FROM revisions WHERE doc_id=? AND version IN (?,?)",
                (d["id"], int(from_version), int(to_version)))}
        fr, to = rows.get(int(from_version)), rows.get(int(to_version))
        if fr is None:
            raise NotFoundError(f"No revision {from_version} for this document.", path=rel)
        if to is None:
            raise NotFoundError(f"No revision {to_version} for this document.", path=rel)
        diff: list[dict] = []
        added = deleted = 0
        for line in difflib.unified_diff(
                (fr["body"] or "").splitlines(), (to["body"] or "").splitlines(), lineterm="", n=3):
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("@@"):
                cls = "hunk"
            elif line.startswith("+"):
                cls, added = "add", added + 1
            elif line.startswith("-"):
                cls, deleted = "del", deleted + 1
            else:
                cls = "ctx"
            diff.append({"cls": cls, "text": line})
        return {"path": rel, "from_version": int(from_version), "to_version": int(to_version),
                "from_title": fr["title"], "to_title": to["title"], "diff": diff,
                "summary": {"lines_added": added, "lines_deleted": deleted}}

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

    def search_page(self, query: str, *, mode: str = "hybrid", top_k: int = 10,
                    folder: str | None = None, tags: list[str] | None = None,
                    since: str | None = None, until: str | None = None,
                    ) -> tuple[list[search.SearchResult], bool]:
        """Hybrid search returning ``(results, truncated)``, applying this service's
        configured fusion tuning (``search_params``). The single entry point both the
        web and MCP surfaces go through so tuning is honored uniformly. ``since``/``until``
        bound hits by ``updated_at`` (recency filter)."""
        return search.search_page(self.db, self.embedder, query, mode=mode, top_k=top_k,
                                  folder=folder, tags=tags, since=since, until=until,
                                  params=self.search_params)

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
            max_sources=max_sources, mode=mode, folder=folder, tags=tags,
            params=self.search_params)

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
               title: str | None = None, tags: list[str] | None = None,
               *, embed: bool = True) -> dict:
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
        if embed:
            self._embed(doc_id)
        DOC_WRITES.labels("create").inc()
        self._emit("create", rel, new_version, title=final_title,
                   updated_by=principal.username, via=principal.via)
        self._bump_nav()
        return self.get(rel)

    def update(self, principal: Principal, path: str, base_version: int | None, content: str,
               title: str | None = None, tags: list[str] | None = None,
               *, embed: bool = True,
               idempotency: tuple[str, int, str] | None = None) -> dict:
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
            if idempotency is not None:
                # Stamp the key in the SAME transaction as the write it guards. If a
                # concurrent request already committed this key, the UNIQUE constraint
                # raises here and the whole write rolls back — so the duplicate never
                # lands (the caller then replays the original result).
                scope, uid, key = idempotency
                conn.execute(
                    "INSERT INTO idempotency_keys(scope, user_id, idem_key, doc_id, "
                    "result_version, result_path, created_at) VALUES(?,?,?,?,?,?,?)",
                    (scope, uid, key, doc_id, new_version, rel, now))
                cutoff = (datetime.now(UTC) - timedelta(days=_IDEM_RETENTION_DAYS)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,))

        mtime = self._write_file(rel, content)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        if content_changed and embed:
            self._embed(doc_id)
        DOC_WRITES.labels("update").inc()
        self._emit("update", rel, new_version, title=final_title,
                   updated_by=principal.username, via=principal.via,
                   content_changed=content_changed)
        self._bump_nav()
        return self.get(rel)

    # ---- targeted edits (token-cheap; funnel through the CAS update path) ----
    def get_section(self, path: str, heading: str, occurrence: int = 1) -> dict:
        doc = self.get(path)
        lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
        return {"path": doc["path"], "heading": heading, "occurrence": occurrence,
                "version": doc["version"], "tags": doc["tags"],
                "content": "".join(lines[start:end])}

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
                        base_version: int | None = None, occurrence: int = 1) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
        # Keep the heading line; replace its body up to the next same/higher heading.
        body = "".join(lines[:start + 1]) + _as_block(text) + "".join(lines[end:])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, body)

    def append_section(self, principal: Principal, path: str, heading: str, text: str,
                       base_version: int | None = None, occurrence: int = 1) -> dict:
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        doc = self.get(path)
        lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
        head = "".join(lines[:end])
        # Guarantee a line boundary: a final section whose last line has no trailing
        # newline would otherwise glue the appended block onto that line.
        if head and not head.endswith("\n"):
            head += "\n"
        body = head + _as_block(text) + "".join(lines[end:])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, body)

    def append_to_document(self, principal: Principal, path: str, text: str,
                           ensure_heading: str | None = None,
                           base_version: int | None = None,
                           idempotency_key: str | None = None) -> dict:
        """Append a text block to the document — the natural agent-journaling
        primitive (decision logs, daily notes). With ``ensure_heading`` the block
        goes at the end of that heading's section, creating the heading (h2) at the
        document end if it doesn't exist yet; without it, the block lands at the very
        end. Like the section editors, ``base_version`` defaults to the server-read
        version so a plain append doesn't need a round-trip and won't spuriously
        conflict.

        ``idempotency_key`` makes the append retry-safe: a key the server has already
        applied replays the prior result instead of appending again, so a client that
        retries after a lost response won't duplicate the block. Use a fresh key per
        logical append."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        if not text or not text.strip():
            raise ValidationError("'text' is required.")
        if idempotency_key:
            cached = self._idem_lookup("append", principal.user_id, idempotency_key)
            if cached is not None:
                return cached
        doc = self.get(path)
        body = doc["content"]
        if ensure_heading and ensure_heading.strip():
            heading = ensure_heading.strip().lstrip("#").strip()
            loc = _locate_section(body, heading)
            if loc:
                lines, _start, end, _ = loc
                head = "".join(lines[:end])
                if head and not head.endswith("\n"):
                    head += "\n"
                new_body = head + _as_block(text) + "".join(lines[end:])
            else:
                base = body.rstrip("\n")
                prefix = (base + "\n\n") if base else ""
                new_body = f"{prefix}## {heading}\n\n{_as_block(text)}"
        else:
            base = body.rstrip("\n")
            prefix = (base + "\n\n") if base else ""
            new_body = prefix + _as_block(text)
        bv = doc["version"] if base_version is None else int(base_version)
        idem = ("append", principal.user_id, idempotency_key) if idempotency_key else None
        try:
            return self.update(principal, doc["path"], bv, new_body, idempotency=idem)
        except sqlite3.IntegrityError:
            # A concurrent request with the same key committed between our pre-check and
            # our commit; our write rolled back. Replay the original result.
            cached = self._idem_lookup("append", principal.user_id, idempotency_key) if idempotency_key else None
            if cached is not None:
                return cached
            raise

    def patch(self, principal: Principal, path: str, find: str, replace: str,
              base_version: int | None = None, count: int = 1,
              mode: str = "literal", occurrence: int | None = None) -> dict:
        """Find-and-replace a substring (``mode='literal'``) or a regular expression
        (``mode='regex'``, ``re.MULTILINE``; ``replace`` may use ``\\1`` backrefs).
        ``occurrence`` (1-based) targets a single match deterministically — the way
        out of "appears N times" failures on repetitive content; otherwise ``count``
        bounds how many matches may be replaced (0/None = all)."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        if not find:
            raise ValidationError("'find' text is required.")
        if mode not in ("literal", "regex"):
            raise ValidationError("mode must be 'literal' or 'regex'.")
        if occurrence is not None and occurrence < 1:
            raise ValidationError("occurrence is 1-based (must be >= 1).")
        doc = self.get(path)
        content, rel = doc["content"], doc["path"]

        if mode == "regex":
            if len(find) > 1000:
                raise ValidationError("regex pattern too long (max 1000 chars).")
            try:
                pat = re.compile(find, re.MULTILINE)
            except re.error as e:
                raise ValidationError(f"invalid regex: {e}") from None
            matches = list(pat.finditer(content))
            n = len(matches)
            if n == 0:
                raise NotFoundError("Pattern not found; nothing patched.", path=rel)
            if occurrence is not None:
                if occurrence > n:
                    raise ValidationError(f"occurrence {occurrence} out of range (1..{n}).")
                m = matches[occurrence - 1]
                new_body = content[:m.start()] + m.expand(replace) + content[m.end():]
            else:
                if count and n > count:
                    raise ValidationError(
                        f"Pattern matches {n} times (limit {count}); narrow it, pass "
                        f"'occurrence', or raise 'count'.")
                new_body = pat.sub(replace, content, count=count or 0)
        else:
            occurrences = content.count(find)
            if occurrences == 0:
                raise NotFoundError("Search text not found; nothing patched.", path=rel)
            if occurrence is not None:
                if occurrence > occurrences:
                    raise ValidationError(f"occurrence {occurrence} out of range (1..{occurrences}).")
                idx = -len(find)
                for _ in range(occurrence):
                    idx = content.find(find, idx + len(find))
                new_body = content[:idx] + replace + content[idx + len(find):]
            else:
                if count and occurrences > count:
                    raise ValidationError(
                        f"Search text appears {occurrences} times (limit {count}); make it more "
                        f"specific, pass 'occurrence', or raise 'count'.")
                new_body = content.replace(find, replace, count if count else -1)

        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, rel, bv, new_body)

    def restore_revision(self, principal: Principal, path: str, version: int,
                         base_version: int | None = None) -> dict:
        """Replay a past revision's body as a new edit (one CAS update) — a server-side
        undo. The old body is loaded here and never has to travel through the caller,
        so reverting a large document is a single small call. ``base_version`` defaults
        to the current version; pass it to reject the revert with 'conflict' if the
        document changed since you looked."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        rev = self.revision(path, int(version))
        bv = self.get(rev["path"])["version"] if base_version is None else int(base_version)
        return self.update(principal, rev["path"], bv, rev["content"], title=rev["title"])

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

    def merge_tags(self, principal: Principal, sources: list[str], dest: str) -> dict:
        """Vault-wide tag cleanup (editor/admin only): rewrite every document's frontmatter
        ``tags`` so each of ``sources`` becomes ``dest``. Each affected document is updated
        through patch_tags (its own CAS revision), so this is not one transaction. Tags
        written inline as ``#hashtags`` in the body are NOT rewritten (patch_tags only
        manages the frontmatter list) — edit the body for those. Returns the dest, the
        normalized sources, and how many documents were touched."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot rewrite tags (read/search only).")
        dest = str(dest or "").strip().lstrip("#")
        if not dest:
            raise ValidationError("dest tag must not be empty.")
        src = sorted({str(s).strip().lstrip("#") for s in (sources or []) if str(s).strip()} - {dest})
        if not src:
            raise ValidationError("no source tags to merge (after removing the dest tag).")
        ph = ",".join("?" * len(src))
        with self.db.reader() as conn:
            paths = [r["path"] for r in conn.execute(
                f"SELECT DISTINCT d.path FROM tags t JOIN documents d ON d.id=t.doc_id "
                f"WHERE t.tag IN ({ph}) AND d.is_deleted=0 ORDER BY d.path", src)]
        changed = 0
        for p in paths:
            before = self.get(p)["version"]
            after = self.patch_tags(principal, p, add=[dest], remove=src)
            if after["version"] != before:  # patch_tags is a no-op (no version bump) when there's no net change
                changed += 1
        return {"ok": True, "dest": dest, "sources": src,
                "docs_affected": len(paths), "docs_changed": changed}

    def rename_tag(self, principal: Principal, old: str, new: str) -> dict:
        """Rename one frontmatter tag across the whole vault (editor/admin only) — a
        single-source merge_tags. See merge_tags for the inline-hashtag caveat."""
        return self.merge_tags(principal, [old], new)

    # ``title``/``tags`` are surfaced and edited through dedicated paths (the heading and
    # the tag list), so the generic property editor leaves them alone to avoid two ways
    # to write the same field.
    _PROP_RESERVED = {"title", "tags"}
    _PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

    def _validate_prop_key(self, key: str) -> str:
        key = (key or "").strip()
        if not key or not self._PROP_KEY_RE.match(key):
            raise ValidationError("property key must be letters/digits/_/- only.")
        if key.lower() in self._PROP_RESERVED:
            raise ValidationError(f"'{key}' is managed elsewhere (use the title/tags editors).")
        return key

    @staticmethod
    def _norm_prop_value(value: str | list[str]) -> str | list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return str(value).strip()

    def set_property(self, principal: Principal, path: str, key: str,
                     value: str | list[str], base_version: int | None = None) -> dict:
        """Set/replace one frontmatter property (body + other keys untouched), through
        the CAS update path. An empty value removes the key."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        key = self._validate_prop_key(key)
        doc = self.get(path)
        val = self._norm_prop_value(value)
        if not val:
            new_content = remove_frontmatter_property(doc["content"], key)
        else:
            new_content = set_frontmatter_property(
                doc["content"], key, val if isinstance(val, str) or len(val) > 1 else val[0])
        if new_content == doc["content"]:
            return self.get(doc["path"])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, new_content)

    def remove_property(self, principal: Principal, path: str, key: str,
                        base_version: int | None = None) -> dict:
        """Remove one frontmatter property (no-op if absent), through CAS update."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        key = self._validate_prop_key(key)
        doc = self.get(path)
        new_content = remove_frontmatter_property(doc["content"], key)
        if new_content == doc["content"]:
            return self.get(doc["path"])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, new_content)

    def replace_properties(self, principal: Principal, path: str,
                           props: list[tuple[str, list[str]]],
                           base_version: int | None = None) -> dict:
        """Replace the whole editable property set in one revision: drops omitted keys,
        sets the rest. ``title``/``tags`` and the body are preserved. ``props`` is an
        ordered list of (key, values); empty value-lists drop the key."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        cleaned: list[tuple[str, list[str]]] = []
        seen_keys: set[str] = set()
        for key, values in props:
            key = self._validate_prop_key(key)
            if key.lower() in seen_keys:
                raise ValidationError(f"duplicate property key '{key}'.")
            seen_keys.add(key.lower())
            vals = [str(v).strip() for v in values if str(v).strip()]
            cleaned.append((key, vals))
        doc = self.get(path)
        content = doc["content"]
        keep = {k.lower() for k, _ in cleaned}
        for existing_key, _ in document_properties(content):
            if existing_key.lower() not in keep:
                content = remove_frontmatter_property(content, existing_key)
        for key, vals in cleaned:
            if not vals:
                content = remove_frontmatter_property(content, key)
            else:
                content = set_frontmatter_property(content, key, vals[0] if len(vals) == 1 else vals)
        if content == doc["content"]:
            return self.get(doc["path"])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, content)

    def broken_links(self, limit: int = 200) -> dict:
        """Vault-wide unresolved links (dangling references) for cleanup tooling."""
        limit = clamp_int(limit, 1, 2000)
        with self.db.reader() as conn:
            items = graph.list_broken_links(conn, limit)
        return {"count": len(items), "links": items}

    def move_preview(self, path: str, new_path: str) -> dict:
        """Read-only preview of a move: whether the destination is already taken, and the
        inbound links (other docs pointing at the current path) that fix_references would
        rewrite. Lets a caller see the blast radius before committing the move."""
        rel, new_rel = normalize_rel_path(path), normalize_rel_path(new_path)
        if not self.exists(rel):
            raise NotFoundError("No document at this path.", path=rel)
        inbound = self.backlinks(rel)["backlinks"]
        return {"from": rel, "to": new_rel, "dest_exists": self.exists(new_rel),
                "inbound_count": len(inbound), "inbound": [b["src_path"] for b in inbound]}

    def move(self, principal: Principal, path: str, new_path: str,
             fix_references: bool = False) -> dict:
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
        result = self.get(new_rel)
        if fix_references:
            # Re-resolution above fixed the GRAPH, but bodies still contain the old
            # link text; rewrite those so the references don't show up broken.
            result = {**result, "references": self.rename_references(principal, rel, new_rel)}
        self._bump_nav()
        return result

    def rename_references(self, principal: Principal, old_path: str, new_path: str) -> dict:
        """Rewrite the link TEXT in other documents that pointed at ``old_path`` so it
        points at ``new_path`` — the cleanup ``move`` deliberately doesn't do inline.
        Only links that are currently broken AND keyed to the old path/name are
        touched (path-form links and bare-name links whose stem changed); a bare name
        that still resolves elsewhere is left alone. Each affected document gets one
        audited revision through the CAS update path, so a single conflict skips that
        document instead of aborting the rest.
        Returns {from, to, docs_rewritten, links_rewritten, skipped_conflicts}."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot modify documents (read/search only).")
        old_rel, new_rel = normalize_rel_path(old_path), normalize_rel_path(new_path)
        old_norm, old_stem = path_norm(old_rel), basename_stem(old_rel).lower()
        new_noext = new_rel[:-3] if new_rel.lower().endswith(".md") else new_rel
        new_basename = new_noext.rsplit("/", 1)[-1]

        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT DISTINCT d.path FROM links l JOIN documents d ON d.id=l.src_doc_id "
                "WHERE d.is_deleted=0 AND l.is_resolved=0 AND "
                "((l.dst_is_path=1 AND l.dst_path_norm=?) OR (l.dst_is_path=0 AND l.dst_name=?))",
                (old_norm, old_stem),
            ).fetchall()
        candidates = [r["path"] for r in rows]

        docs_rewritten = links_rewritten = skipped = 0
        for src_path in candidates:
            try:
                doc = self.get(src_path)
            except NotFoundError:
                continue
            body = doc["content"]
            edits: list[tuple[int, int, str]] = []
            for link in extract_links(body):
                try:
                    dpn, dname, is_path = graph._link_keys(link.target)
                except PathError:
                    continue
                if not ((is_path and dpn == old_norm) or (not is_path and dname == old_stem)):
                    continue
                # Only repoint genuinely-broken refs; a bare name resolving elsewhere
                # is a legitimately different target now and must be left intact.
                if self.resolve_link(link.target, doc["path"]):
                    continue
                new_target = (new_rel if link.target.lower().endswith(".md") else new_noext) \
                    if is_path else new_basename
                edits.append((link.start, link.end, rewrite_link_target(link, new_target)))
            if not edits:
                continue
            new_body = body
            for start, end, new_raw in sorted(edits, key=lambda e: e[0], reverse=True):
                new_body = new_body[:start] + new_raw + new_body[end:]
            try:
                self.update(principal, doc["path"], doc["version"], new_body)
            except ConflictError:
                skipped += 1
                continue
            docs_rewritten += 1
            links_rewritten += len(edits)
        return {"from": old_rel, "to": new_rel, "docs_rewritten": docs_rewritten,
                "links_rewritten": links_rewritten, "skipped_conflicts": skipped}

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
        params.append(clamp_int(limit, 1, 200))
        with self.db.reader() as conn:
            rows = conn.execute(q, params).fetchall()
            tags_by = self._tags_for_ids(conn, [r["id"] for r in rows])
            return [{
                "path": r["path"], "title": r["title"] or r["path"], "version": r["version"],
                "folder": r["folder"], "tags": tags_by.get(r["id"], []), "updated_at": r["updated_at"],
            } for r in rows]

    def daily_note(self, principal: Principal, date: str | None = None, *,
                   folder: str = "daily") -> dict:
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
        if self.exists(rel):
            return {**self.get(rel), "created": False}
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot create the daily note (read/search only).")
        return {**self.create(principal, rel, f"# {date}\n\n", title=date), "created": True}

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
            # file_state='pending' guards the post-commit _trash_file the same way
            # create()/update() guard their file write: a crash between this commit
            # and the trash leaves the row pending, and recover_pending() finishes
            # the trash on the next start (it would otherwise be an on-disk orphan
            # the DB has already marked deleted).
            conn.execute(
                "UPDATE documents SET is_deleted=1, version=version+1, file_state='pending', "
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
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean' WHERE id=?", (doc_id,))
        DOC_WRITES.labels("delete").inc()
        self._emit("delete", rel, new_version,
                   updated_by=principal.username, via=principal.via)
        self._bump_nav()
        return {"ok": True, "path": rel, "deleted": True}

    def list_deleted(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Soft-deleted documents (the trash), most-recently-deleted first. Each carries
        path/title/version/folder, when and by whom it was deleted — enough to decide
        what to restore or purge."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT id, path, title, version, folder, updated_at, updated_by "
                "FROM documents WHERE is_deleted=1 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (clamp_int(limit, 1, 1000), max(0, int(offset))),
            ).fetchall()
            return [{
                "path": r["path"], "title": r["title"] or r["path"], "version": r["version"],
                "folder": r["folder"], "updated_at": r["updated_at"],
                "deleted_by": self._username(conn, r["updated_by"]),
            } for r in rows]

    def restore(self, principal: Principal, path: str) -> dict:
        """Bring a soft-deleted document back (editor/admin only): un-tombstone it, rebuild
        the search/graph artifacts that delete tore down (FTS rows, chunks, link edges,
        and inbound-link backfill), re-project the .md, and re-embed. The pre-delete body
        is the latest revision's, so no content travels through the caller."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot restore documents (read/search only).")
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        stem = basename_stem(rel)
        now = now_iso()
        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, version, title, folder, is_deleted FROM documents WHERE path_norm=?",
                (norm,)).fetchone()
            if not row:
                raise NotFoundError("No document at this path.", path=rel)
            if not row["is_deleted"]:
                raise ValidationError("Document is not deleted; nothing to restore.")
            doc_id, title, folder = row["id"], row["title"], row["folder"]
            body = self._latest_body(conn, doc_id)
            new_version = row["version"] + 1
            # file_state='pending' guards the post-commit file re-projection, mirroring
            # create()/delete(): a crash before the write is finished by recover_pending().
            conn.execute(
                "UPDATE documents SET version=version+1, content_hash=?, file_state='pending', "
                "vector_dirty=1, is_deleted=0, updated_at=?, updated_by=? WHERE id=?",
                (sha256_hex(body), now, principal.user_id, doc_id),
            )
            conn.execute(
                "INSERT INTO revisions(doc_id, version, body, title, content_hash, author_id, op, via, created_at) "
                "VALUES(?,?,?,?,?,?, 'edit', ?, ?)",
                (doc_id, new_version, body, title, sha256_hex(body), principal.user_id, principal.via, now),
            )
            # tags survive a soft delete (delete() leaves the tags table alone), so only
            # the FTS/chunk/link artifacts — torn down on delete — need rebuilding.
            indexing.reindex_fts(conn, doc_id, title, body)
            indexing.rechunk(conn, doc_id, body)
            indexing.reindex_links(conn, doc_id, body, folder)
            graph.backfill_links_for(conn, doc_id, norm, stem)
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_restore", target=rel, detail=f"v{new_version}")
        mtime = self._write_file(rel, body)
        with self.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?",
                         (mtime, doc_id))
        self._embed(doc_id)
        DOC_WRITES.labels("restore").inc()
        self._emit("restore", rel, new_version, updated_by=principal.username, via=principal.via)
        self._bump_nav()
        return {"ok": True, "path": rel, "version": new_version, "restored": True}

    def purge(self, principal: Principal, path: str) -> dict:
        """Permanently delete a TRASHED document and all its history (admin only) — there
        is no undo. Refuses a live document (soft-delete it first). The row's revisions /
        tags / links are removed by FK cascade (its chunks + vectors were already cleared
        on soft delete); the .trash copy of the file is removed best-effort."""
        if not principal.can_admin:
            raise ForbiddenError("Only an admin can permanently delete a document.")
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT id, is_deleted FROM documents WHERE path_norm=?", (norm,)).fetchone()
            if not row:
                raise NotFoundError("No document at this path.", path=rel)
            if not row["is_deleted"]:
                raise ValidationError("Document is not in the trash; delete it first.")
            doc_id = row["id"]
            graph.unresolve_incoming(conn, doc_id)  # break any inbound links before the row goes
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            audit.record(conn, actor=principal.username, via=principal.via,
                         action="doc_purge", target=rel)
        try:  # remove the trashed file copy (best-effort; DB is canonical)
            trashed = safe_join(self.vault / ".trash", rel)
            if trashed.is_file():
                trashed.unlink()
        except OSError:
            pass
        self._bump_nav()
        return {"ok": True, "path": rel, "purged": True}

    # ---- favorites (per-user pins) --------------------------------------
    def toggle_favorite(self, principal: Principal, path: str) -> dict:
        """Flip whether the current user has pinned this document. Per-user and content-
        neutral: it creates no revision and needs no write permission (a reader may pin
        what they read). Returns the resulting ``favorite`` state."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.writer() as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            existing = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND doc_id=?",
                (principal.user_id, d["id"])).fetchone()
            if existing:
                conn.execute("DELETE FROM favorites WHERE user_id=? AND doc_id=?",
                             (principal.user_id, d["id"]))
                fav = False
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO favorites(user_id, doc_id, created_at) VALUES(?,?,?)",
                    (principal.user_id, d["id"], now_iso()))
                fav = True
        return {"ok": True, "path": rel, "favorite": fav}

    def set_favorite(self, principal: Principal, path: str, favorite: bool) -> dict:
        """Idempotently set whether the current user has pinned this document (unlike
        toggle_favorite, the resulting state is the one you asked for — friendlier for an
        agent than a flip). Per-user, content-neutral, no write permission required."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.writer() as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            if favorite:
                conn.execute(
                    "INSERT OR IGNORE INTO favorites(user_id, doc_id, created_at) VALUES(?,?,?)",
                    (principal.user_id, d["id"], now_iso()))
            else:
                conn.execute("DELETE FROM favorites WHERE user_id=? AND doc_id=?",
                             (principal.user_id, d["id"]))
        return {"ok": True, "path": rel, "favorite": favorite}

    def is_favorite(self, user_id: int, path: str) -> bool:
        norm = path_norm(normalize_rel_path(path))
        with self.db.reader() as conn:
            r = conn.execute(
                "SELECT 1 FROM favorites f JOIN documents d ON d.id=f.doc_id "
                "WHERE f.user_id=? AND d.path_norm=? AND d.is_deleted=0", (user_id, norm)).fetchone()
        return r is not None

    def list_favorites(self, user_id: int) -> list[dict]:
        """The user's pinned documents (live only), title-sorted — for the sidebar
        favourites section and a favourites view."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT d.path, d.title FROM favorites f JOIN documents d ON d.id=f.doc_id "
                "WHERE f.user_id=? AND d.is_deleted=0 ORDER BY d.title COLLATE NOCASE",
                (user_id,)).fetchall()
            return [{"path": r["path"], "title": r["title"] or r["path"]} for r in rows]

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
        sub = _attachment_subname(name, ext, data)
        target = safe_join(self.vault / ATTACH_DIR, sub)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():  # content-addressed: skip rewrite of an identical file
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        url = "/attachments/" + quote(sub)
        alt = (name[: len(name) - len(ext)] or "file")
        return {"path": f"{ATTACH_DIR}/{sub}", "url": url, "markdown": f"![{alt}]({url})"}

    # ---- maintenance ----------------------------------------------------
    def recover_pending(self) -> int:
        """Finish any file projection a crash left half-done (file_state='pending').
        Live docs are re-written from their latest revision; deleted docs whose
        ``_trash_file`` never ran have their leftover file trashed now. Both then
        clear to 'clean'. Idempotent."""
        with self.db.reader() as conn:
            live = conn.execute(
                "SELECT id, path FROM documents WHERE file_state='pending' AND is_deleted=0"
            ).fetchall()
            gone = conn.execute(
                "SELECT id, path FROM documents WHERE file_state='pending' AND is_deleted=1"
            ).fetchall()
            bodies = {r["id"]: (r["path"], self._latest_body(conn, r["id"])) for r in live}
            trash = [(r["id"], r["path"]) for r in gone]
        for doc_id, (rel, body) in bodies.items():
            mtime = self._write_file(rel, body)
            with self.db.writer() as conn:
                conn.execute("UPDATE documents SET file_state='clean', file_mtime=? WHERE id=?", (mtime, doc_id))
        for doc_id, rel in trash:
            self._trash_file(rel)
            with self.db.writer() as conn:
                conn.execute("UPDATE documents SET file_state='clean' WHERE id=?", (doc_id,))
        if bodies or trash:
            log.info("recover_pending: re-projected %d file(s), trashed %d leftover delete(s)",
                     len(bodies), len(trash))
        return len(bodies) + len(trash)

    def embed_pending(self) -> int:
        """Embed any documents still flagged ``vector_dirty`` (no-op when none are).
        A crash can commit a write — version bumped, ``vector_dirty=1`` — but die before
        the post-commit embed (it runs off the write lock), leaving the doc absent from
        vector search until the next ``reindex --reembed``. Sweeping on startup closes
        that gap. Also catches docs left ``file_state='clean'`` but unembedded, which
        ``recover_pending`` (file-state only) does not see."""
        return indexing.embed_pending(self.db, self.embedder)

    def prune_revisions(self, *, keep: int, apply: bool) -> dict:
        """Delete all but the most recent ``keep`` revisions per document. ``keep`` is
        forced to >=1 so each document's latest snapshot — the source of its body — is
        always retained. ``apply=False`` counts without deleting. Irreversible: pruned
        revisions can no longer be viewed (history/diff) or restored. Used by the
        ``prune`` CLI to bound the full-body revision log's growth."""
        keep = max(1, int(keep))
        count_sql = (
            "SELECT COUNT(*) FROM (SELECT ROW_NUMBER() OVER "
            "(PARTITION BY doc_id ORDER BY version DESC) AS rn FROM revisions) WHERE rn > ?"
        )
        with self.db.reader() as conn:
            deletable = conn.execute(count_sql, (keep,)).fetchone()[0]
        if apply and deletable:
            with self.db.writer() as conn:
                conn.execute(
                    "DELETE FROM revisions WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() "
                    "OVER (PARTITION BY doc_id ORDER BY version DESC) AS rn FROM revisions) "
                    "WHERE rn > ?)",
                    (keep,),
                )
            log.info("revision prune: deleted %d revision(s), keeping latest %d per document",
                     deletable, keep)
        return {"keep": keep, "deletable_revisions": deletable, "applied": bool(apply)}

    def reindex_all(self, reembed: bool = False,
                    progress: Callable[[int, int], None] | None = None) -> dict:
        """Reconcile the DB with the on-disk vault (handles external edits / new files /
        renames). New files are created; changed files get an 'external-reconcile'
        revision. A new file whose content UNIQUELY matches a document whose own file
        vanished is treated as that document RENAMED — relocated in place, preserving its
        id / history / backlinks — instead of forking into a duplicate plus an orphan.
        Vanished files with no content match are reported (not auto-deleted). ``progress``,
        if given, is forwarded to the embedding sweep for a CLI progress line."""
        vault = self.vault.resolve()
        log.info("reindex: scanning vault %s (reembed=%s)", vault, reembed)

        def _rel_of(p) -> str | None:
            try:
                relp = p.resolve().relative_to(vault)
            except ValueError:
                return None
            if relp.parts and relp.parts[0] in (".trash", ".tmp"):
                return None
            return "/".join(relp.parts)

        paths = sorted(vault.rglob("*.md"))
        # Pre-pass (path-only, no content reads): document norms present on disk. Used
        # as `seen` for the missing-file report and to find DB docs whose file vanished.
        disk_norms = {path_norm(rel) for rel in (_rel_of(p) for p in paths) if rel is not None}
        # A non-deleted document whose file is gone is a rename SOURCE. A new file whose
        # content_hash UNIQUELY matches one (ambiguous duplicates are left alone) is that
        # document moved — matched by content, not path, so an external `mv` relocates
        # the doc instead of creating a duplicate and orphaning the old one's vectors.
        by_hash: dict[str, list] = {}
        with self.db.reader() as conn:
            for r in conn.execute(
                "SELECT id, path, path_norm, version, content_hash "
                "FROM documents WHERE is_deleted=0"
            ).fetchall():
                if r["path_norm"] not in disk_norms:
                    by_hash.setdefault(r["content_hash"], []).append(r)
        rename_src = {h: rows[0] for h, rows in by_hash.items() if len(rows) == 1}
        claimed: set[int] = set()

        created = updated = unchanged = renamed = 0
        skipped_deleted: list[str] = []
        renames: list[str] = []
        now = now_iso()
        for p in paths:
            rel = _rel_of(p)
            if rel is None:
                continue
            norm, folder, stem = path_norm(rel), folder_of(rel), basename_stem(rel).lower()
            content = p.read_text(encoding="utf-8", errors="replace")
            chash = sha256_hex(content)
            meta = parse_frontmatter(content)[0]
            title = derive_title(meta, content, rel)
            tagset = self._merge_tags(meta, content, None)
            mtime = p.stat().st_mtime
            renamed_from: str | None = None
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
                    src = rename_src.get(chash)
                    if src is not None and src["id"] not in claimed:
                        claimed.add(src["id"])
                        doc_id = src["id"]
                        new_version = src["version"] + 1
                        renamed_from = src["path"]
                        conn.execute(
                            "UPDATE documents SET path=?, path_norm=?, title=?, version=?, "
                            "content_hash=?, folder=?, file_state='clean', vector_dirty=1, "
                            "file_mtime=?, updated_at=?, updated_by=NULL WHERE id=?",
                            (rel, norm, title, new_version, chash, folder, mtime, now, doc_id),
                        )
                        # Path/name changed: incoming links resolved to the old name are
                        # now stale — drop them and re-resolve below (mirrors move()).
                        graph.unresolve_incoming(conn, doc_id)
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
                    "VALUES(?,?,?,?,?,NULL, ?, 'cli', ?)",
                    (doc_id, new_version, content, title, chash,
                     "rename" if renamed_from else "external-reconcile", now),
                )
                self._set_tags(conn, doc_id, tagset)
                indexing.reindex_fts(conn, doc_id, title, content)
                indexing.rechunk(conn, doc_id, content)
                indexing.reindex_links(conn, doc_id, content, folder)
                graph.backfill_links_for(conn, doc_id, norm, stem)
                # External reconciliation is otherwise a silent batch operation; record
                # who/when so "this file changed outside the app" stays auditable, like
                # every other write path.
                detail = (f"v{new_version} rename {renamed_from} -> {rel}" if renamed_from
                          else f"v{new_version} {'create' if is_new else 'update'}")
                audit.record(conn, actor=None, via="cli", action="doc_reconcile", target=rel,
                             detail=detail)
            if renamed_from:
                renamed += 1
                renames.append(f"{renamed_from} -> {rel}")
            elif is_new:
                created += 1
            else:
                updated += 1

        with self.db.reader() as conn:
            missing = [r["path"] for r in conn.execute(
                "SELECT path, path_norm FROM documents WHERE is_deleted=0").fetchall()
                if r["path_norm"] not in disk_norms]
        embedded = indexing.embed_pending(self.db, self.embedder, progress=progress)
        log.info("reindex: created=%d updated=%d renamed=%d unchanged=%d skipped_deleted=%d "
                 "missing_files=%d embedded=%d", created, updated, renamed, unchanged,
                 len(skipped_deleted), len(missing), embedded)
        self._bump_nav()
        return {"created": created, "updated": updated, "renamed": renamed,
                "renames": renames, "unchanged": unchanged,
                "missing_files": missing, "skipped_deleted": skipped_deleted,
                "embedded": embedded}

    # ---- bulk import ----------------------------------------------------
    def import_from_directory(
        self, principal: Principal, source_dir: str | Path, into: str = "", *,
        on_conflict: str = "skip", include: tuple[str, ...] = IMPORT_DEFAULT_INCLUDE,
        recurse: bool = True, import_attachments: bool = False,
        embed: bool = True, dry_run: bool = False,
    ) -> dict:
        """Bulk-ingest an external directory of markdown/Obsidian notes into the vault,
        routing every note through ``create()``/``update()`` so each gets a real
        revision, audit row, index entry, link backfill, and ``.md`` projection.

        Per file: classify the target (against the DB *and* an in-batch claim set so
        intra-batch case collisions resolve deterministically), then write — except in
        ``dry_run``, which classifies identically but skips every write, so the plan it
        prints exactly predicts the real run. Obsidian ``![[embeds]]`` are normalized
        to standard markdown (asset embeds → image links, note embeds → wikilinks) so
        they never enter the link graph as dangling ``.md`` references. Best-effort:
        one file's conflict / OS error is captured in ``errors`` and the rest proceed.
        """
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot import documents (read/search only).")
        if on_conflict not in ("skip", "overwrite", "rename"):
            raise ValidationError("on_conflict must be 'skip', 'overwrite', or 'rename'.")
        src = Path(source_dir).expanduser().resolve()
        if not src.is_dir():
            raise ValidationError(f"source directory not found: {src}")
        vault = self.vault.resolve()
        if src == vault or vault in src.parents or src in vault.parents:
            raise ValidationError("source directory overlaps the vault (self-import).")
        try:
            into_norm = normalize_folder_path(into)
        except PathError as e:
            raise ValidationError(str(e)) from None

        report: dict = {
            "created": 0, "revived": 0, "overwritten": 0, "skipped": 0, "renamed": 0,
            "scanned": 0, "embedded": 0,
            "attachments": {"copied": 0, "skipped": 0},
            "plan": [], "warnings": [], "errors": [], "broken_links": [],
            "dry_run": dry_run,
        }
        warn = report["warnings"].append
        claimed: set[str] = set()        # path_norm of targets created/planned this run
        imported: set[str] = set()       # path_norm actually written (broken-link report)
        asset_cache: dict[str, str] = {}  # resolved asset abs-path -> attachment url

        # -- attachment copy (only with import_attachments) -----------------
        def copy_asset(relpath: str, md_abs: Path) -> str | None:
            ref = relpath.split("#", 1)[0].strip()
            if not ref:
                return None
            # Resolve the reference both relative to the markdown file (standard
            # markdown) and relative to the source root (Obsidian's vault-relative
            # style), taking the first that lands on a real file inside the source.
            # .resolve() collapses any symlink, so the escape check rejects links that
            # point outside --from. (Full Obsidian shortest-path search is out of scope.)
            candidate = None
            escaped = False
            for base in (md_abs.parent, src):
                try:
                    c = (base / ref).resolve()
                except OSError:
                    continue
                if c != src and src not in c.parents:
                    escaped = True
                    continue
                if c.is_file():
                    candidate = c
                    break
            if candidate is None:
                if escaped:
                    warn(f"asset {relpath} (in {md_abs.name}) escapes the source dir; left as-is")
                else:
                    warn(f"missing asset {relpath} referenced by {md_abs.name} (left as broken link)")
                report["attachments"]["skipped"] += 1
                return None
            key = str(candidate)
            if key in asset_cache:
                return asset_cache[key]
            ext = candidate.suffix.lower()
            if ext not in IMPORT_ATTACH_EXTS:
                warn(f"unsupported asset {relpath} ({ext or 'no ext'}) in {md_abs.name}; left as-is")
                report["attachments"]["skipped"] += 1
                return None
            try:
                data = candidate.read_bytes()
            except OSError:
                report["attachments"]["skipped"] += 1
                return None
            if not data or len(data) > ATTACH_MAX_BYTES:
                warn(f"asset {relpath} in {md_abs.name} is empty or too large; left as-is")
                report["attachments"]["skipped"] += 1
                return None
            # Content-addressed: an identical asset already in the vault (e.g. a prior
            # import) is a no-op. Only count/plan/audit a genuinely new write so the
            # report reflects what actually hit disk (and a re-run reports copied=0).
            sub = _attachment_subname(candidate.name, ext, data)
            url = "/attachments/" + quote(sub)
            newly = not (self.vault / ATTACH_DIR / sub).exists()
            if newly and not dry_run:
                res = self.save_attachment(principal, candidate.name, data)
                url = res["url"]
                audit.record_tx(self.db, actor=principal.username, via=principal.via,
                                action="attachment_upload", target=res["path"])
            asset_cache[key] = url
            if newly:
                report["attachments"]["copied"] += 1
                report["plan"].append({"src": relpath, "target": f"{ATTACH_DIR}/{sub}",
                                       "action": "attach", "reason": None})
            return url

        # -- embed/asset normalization (always runs) ------------------------
        def normalize_body(raw: str, md_abs: Path) -> str:
            def embed_repl(m: re.Match) -> str:
                inner = m.group(1).strip()
                head = inner.split("|", 1)[0]
                target = head.split("#", 1)[0].strip()
                if not target:
                    return m.group(0)
                last = target.rsplit("/", 1)[-1]
                ext = ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""
                if ext == "" or ext in IMPORT_MD_EXTS:
                    # Note transclusion -> a plain wikilink (resolves by name, no '!').
                    anchor = ("#" + head.split("#", 1)[1]) if "#" in head else ""
                    return f"[[{target}{anchor}]]"
                # Asset embed -> a standard image link (never a graph wikilink).
                url = copy_asset(target, md_abs) if import_attachments else None
                return f"![{last}]({url or target})"

            out = _replace_outside_code(_EMBED_RE, embed_repl, raw)
            if import_attachments:
                def img_repl(m: re.Match) -> str:
                    alt, url = m.group(1), m.group(2).strip()
                    if not url or url[0] in "#/" or url.startswith("//") or SCHEME_RE.match(url):
                        return m.group(0)
                    new = copy_asset(url, md_abs)
                    return f"![{alt}]({new})" if new else m.group(0)
                out = _replace_outside_code(_IMG_RE, img_repl, out)
            return out

        # -- target path: extension-normalize, prefix --into, validate ------
        def target_for(source_rel: str) -> str:
            p, low = source_rel, source_rel.lower()
            for e in (".markdown", ".mdown", ".mkd"):
                if low.endswith(e):
                    p = p[: -len(e)] + ".md"
                    break
            combined = f"{into_norm}/{p}" if into_norm else p
            return normalize_rel_path(combined)

        def free_variant(target_rel: str) -> str:
            base = target_rel[:-3]  # strip the guaranteed lowercase '.md'
            with self.db.reader() as conn:
                for n in range(2, 10001):
                    cand = f"{base}-{n}.md"
                    cnorm = path_norm(cand)
                    if cnorm in claimed:
                        continue
                    if conn.execute("SELECT 1 FROM documents WHERE path_norm=?", (cnorm,)).fetchone():
                        continue
                    return cand
            raise ValidationError(f"no free rename variant for {target_rel} (10000 tried).")

        # -- per-file classify + (optionally) write -------------------------
        def handle(md_abs: Path, source_rel: str) -> None:
            try:
                size = md_abs.stat().st_size
            except OSError:
                warn(f"skipped {source_rel} (vanished before read)")
                return
            if size > IMPORT_MAX_BYTES:
                warn(f"skipped {source_rel} (file too large)")
                if not dry_run:
                    audit.record_tx(self.db, actor=principal.username, via=principal.via,
                                    action="doc_import_skip", target=source_rel,
                                    outcome="skipped", detail="file too large")
                return
            try:
                raw = md_abs.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                report["errors"].append({"path": source_rel, "error": str(e)})
                return
            if "�" in raw:
                warn(f"encoding replaced in {source_rel} (invalid UTF-8)")  # U+FFFD present
            if not raw.strip():
                warn(f"skipped {source_rel} (empty)")
                return

            target_rel = target_for(source_rel)
            content = normalize_body(raw, md_abs)
            chash = sha256_hex(content)
            norm = path_norm(target_rel)

            with self.db.reader() as conn:
                row = conn.execute(
                    "SELECT id, version, is_deleted, content_hash FROM documents WHERE path_norm=?",
                    (norm,)).fetchone()
            in_batch = norm in claimed
            live = bool(row and not row["is_deleted"])

            # Idempotent re-run: identical content already live -> no-op skip.
            if live and row["content_hash"] == chash:
                report["plan"].append({"src": source_rel, "target": target_rel,
                                       "action": "skip", "reason": "unchanged"})
                report["skipped"] += 1
                return

            final_rel = target_rel
            base_version: int | None = None
            reason: str | None = None
            if not row and not in_batch:
                action = "create"
            elif row and row["is_deleted"] and not in_batch:  # tombstone
                if on_conflict == "rename":
                    final_rel, action, reason = free_variant(target_rel), "rename", "tombstone"
                else:
                    action, reason = "revive", "tombstone"
            else:  # live conflict (DB row or already claimed this batch)
                if in_batch:
                    warn(f"case collision: {source_rel} maps to an already-imported path "
                         f"({target_rel}); applying on_conflict={on_conflict}")
                if on_conflict == "skip":
                    report["plan"].append({"src": source_rel, "target": target_rel,
                                           "action": "skip", "reason": "exists"})
                    report["skipped"] += 1
                    if not dry_run:
                        audit.record_tx(self.db, actor=principal.username, via=principal.via,
                                        action="doc_import_skip", target=target_rel,
                                        outcome="conflict", detail="exists")
                    return
                if on_conflict == "overwrite":
                    action = "overwrite"
                    base_version = row["version"] if live else None
                else:
                    final_rel, action = free_variant(target_rel), "rename"

            report["plan"].append({"src": source_rel, "target": final_rel,
                                   "action": action, "reason": reason})
            claimed.add(path_norm(final_rel))

            if dry_run:
                report[{"create": "created", "revive": "revived", "overwrite": "overwritten",
                        "rename": "renamed"}[action]] += 1
                if embed:  # predict the post-commit embed the real run would do
                    report["embedded"] += 1
                return

            try:
                if action == "overwrite":
                    self.update(principal, final_rel, base_version, content, embed=embed)
                    report["overwritten"] += 1
                else:  # create / revive / rename all create() at final_rel
                    self.create(principal, final_rel, content, embed=embed)
                    report["created" if action == "create"
                           else "revived" if action == "revive" else "renamed"] += 1
            except ConflictError as e:
                report["errors"].append({"path": source_rel, "error": e.message})
                return
            imported.add(path_norm(final_rel))
            if embed:
                report["embedded"] += 1

        # -- walk + process -------------------------------------------------
        for md_abs, source_rel in self._walk_import_files(src, include, recurse, warn):
            report["scanned"] += 1
            try:
                handle(md_abs, source_rel)
            except PathError as e:
                warn(f"skipped {source_rel} ({e})")
            except WikiError as e:
                report["errors"].append({"path": source_rel, "error": e.message})
            except OSError as e:
                report["errors"].append({"path": source_rel, "error": str(e)})

        if not dry_run and imported:
            with self.db.reader() as conn:
                broken = graph.list_broken_links(conn, 2000)
            report["broken_links"] = [b for b in broken if path_norm(b["src_path"]) in imported]
        return report

    def _walk_import_files(self, src: Path, include: tuple[str, ...], recurse: bool,
                           warn: Callable[[str], None]) -> Iterator[tuple[Path, str]]:
        """Yield (abs_path, source-relative POSIX path) for importable markdown files:
        prunes excluded/attachment dirs, never follows symlinks, and matches the
        ``include`` globs case-insensitively against the relative path."""
        def included(rel: str) -> bool:
            low = rel.lower()
            return any(fnmatch.fnmatchcase(low, pat.lower()) for pat in include)

        def consider(ap: Path, rel: str) -> tuple[Path, str] | None:
            if ap.is_symlink():
                warn(f"skipped {rel} (source symlink, not followed)")
                return None
            if not included(rel):
                return None
            return (ap, rel)

        if recurse:
            for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
                base = Path(dirpath)
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in IMPORT_EXCLUDED_DIRS and d != ATTACH_DIR
                    and not (base / d).is_symlink())
                for fn in sorted(filenames):
                    ap = base / fn
                    got = consider(ap, ap.relative_to(src).as_posix())
                    if got:
                        yield got
        else:
            for ap in sorted(src.iterdir()):
                if ap.is_dir():
                    continue
                got = consider(ap, ap.name)
                if got:
                    yield got
