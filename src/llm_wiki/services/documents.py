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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import groupby, islice
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlencode

import regex as regex_lib

from .. import file_projection as fp
from .. import graph, indexing, search
from ..db import Database
from ..embedding import Embedder
from ..embedding_contract import EmbeddingBindingChanged
from ..markdown_utils import (
    SCHEME_RE,
    _mask,
    derive_content_title,
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
from ..merge import three_way_merge
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
    EmbeddingUnavailableError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    WikiError,
)

REGEX_TIMEOUT_S = 0.25
MAX_PATCH_MATCHES = 10_000

log = logging.getLogger("llm_wiki.documents")

_EMBEDDING_UNAVAILABLE_MESSAGE = (
    "Embedding search is temporarily unavailable because this service is using "
    "an outdated embedding generation. Restart the service and retry."
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A markdown task-list line: "- [ ] ...", "* [x] ...", "1. [ ] ..." (captures the
# checkbox state for click-to-toggle). Groups: (prefix, state-char, rest).
_TASK_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CORPUS_DESCRIPTION_PREFIX_CHARS = 16 * 1024
SEARCH_WORKBENCH_MAX_RESULTS = 600

# Uploaded images/files live under this vault subdir (excluded from the .md scan).
ATTACH_DIR = "_attachments"
ATTACH_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}

# ---- bulk import (Obsidian/markdown directory ingest) ----------------------
IMPORT_MAX_BYTES = 50 * 1024 * 1024  # per-file ceiling for one note
IMPORT_DEFAULT_INCLUDE = ("*.md", "*.markdown", "*.mdown", "*.mkd")
IMPORT_MD_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
_IMPORT_RENAME_MAX_SUFFIX = 10_000
# Directories that legitimately appear inside an external vault but must never be
# ingested (app/editor metadata, VCS, dependency trees, our own scratch/trash).
IMPORT_EXCLUDED_DIRS = {
    ".obsidian",
    ".trash",
    ".tmp",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
}
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


@dataclass(frozen=True)
class NormalizedSearchFilter:
    operator: str
    value: str


@dataclass(frozen=True)
class SearchFilters:
    query: str
    mode: str
    folder: str
    tags: tuple[str, ...]
    normalized: tuple[NormalizedSearchFilter, ...]

    def url_for_page(self, page: int, per_page: int) -> str:
        pairs = [("q", self.query), ("mode", self.mode)]
        if self.folder:
            pairs.append(("folder", self.folder))
        pairs.extend(("tag", tag) for tag in self.tags)
        pairs.extend((("page", str(page)), ("per_page", str(per_page))))
        return "/search?" + urlencode(pairs)


@dataclass(frozen=True)
class SearchPage:
    items: tuple[search.SearchResult, ...]
    total_or_more: int | None
    page: int
    per_page: int
    has_prev: bool
    has_next: bool
    bounded: bool
    filters: SearchFilters

    @property
    def prev_url(self) -> str | None:
        if not self.has_prev:
            return None
        return self.filters.url_for_page(self.page - 1, self.per_page)

    @property
    def next_url(self) -> str | None:
        if not self.has_next:
            return None
        return self.filters.url_for_page(self.page + 1, self.per_page)


class ProjectionPendingError(RuntimeError):
    """A committed DB write whose filesystem projection remains recoverable."""

    def __init__(self, result: fp.ProjectionResult):
        detail = f": {result.detail}" if result.detail else ""
        super().__init__(
            f"Document file projection remains pending ({result.reason or 'unknown'}){detail}"
        )
        self.result = result


class _ReindexRetry(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
        out.append(text[last : m.start()])
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
            f"section(s) titled {heading!r}.",
            path=doc["path"],
        )
    raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])


def _as_block(text: str) -> str:
    """Normalize inserted text to end with exactly one trailing newline."""
    return text.rstrip("\n") + "\n"


class DocumentService:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        vault_path: Path | str,
        events=None,
        search_params: search.FusionParams | None = None,
        embed_worker: indexing.EmbeddingWorker | None = None,
    ):
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
            self.events.publish(
                {"type": "doc_changed", "op": op, "path": path, "version": version, **extra}
            )
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
            msg,
            path=rel,
            current_version=d["version"],
            current_title=d["title"],
            current_content=body,
            updated_by=self._username(conn, d["updated_by"]),
            updated_at=d["updated_at"],
            current_via=lv["via"] if lv else None,
        )

    def _projection_snapshot(
        self, conn: sqlite3.Connection, doc_id: int
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
        self,
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
        self,
        snapshot: ProjectionSnapshot,
        staged: fp.StagedText,
        target: Path,
        live_target: Path,
        trash_target: Path,
    ) -> float | None:
        installed = fp.install_staged(staged, target)
        if snapshot.is_deleted:
            fp.unlink_regular(live_target, vault=self.vault)
            return None
        fp.unlink_regular(trash_target, vault=self.vault)
        return installed.mtime_ns / 1_000_000_000

    def _mark_projection_clean(
        self,
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

    @staticmethod
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
        self,
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
                target = fp.managed_path(self.vault, rel, namespace="live")
                current = fp.confined_file_signature(self.vault, target, missing_ok=True)
                if current is None:
                    if fp.confirm_confined_absence(self.vault, target):
                        conn.execute(
                            "DELETE FROM file_projection_cleanup WHERE doc_id=? AND path_norm=?",
                            (snapshot.doc_id, norm),
                        )
                    else:
                        issues.append(CleanupIssue(rel, "cleanup_changed"))
                    continue
                expected = self._expected_cleanup_signature(row)
                if expected is None or current != expected:
                    issues.append(CleanupIssue(rel, "cleanup_changed"))
                    continue
                if not fp.unlink_regular(target, expected=expected, vault=self.vault):
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

    @staticmethod
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
        self,
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
                target = fp.managed_path(self.vault, rel, namespace="live")
                current = fp.confined_file_signature(self.vault, target, missing_ok=True)
                if current is None:
                    if not fp.confirm_confined_absence(self.vault, target):
                        # A new generation appeared. Purge preserves it and retires
                        # the old document's cleanup authority.
                        pass
                else:
                    expected = self._expected_cleanup_signature(row)
                    if expected is not None and current == expected:
                        # False means it changed/disappeared between stat and unlink;
                        # either way the new/external generation is preserved.
                        removed = fp.unlink_regular(target, expected=expected, vault=self.vault)
                        if not removed:
                            after = fp.confined_file_signature(self.vault, target, missing_ok=True)
                            if after is None:
                                fp.confirm_confined_absence(self.vault, target)
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

    def _finish_purge(self, doc_id: int) -> fp.ProjectionResult:
        """Finish one immutable purge request, idempotently and audit-exactly-once."""
        cursor = ""
        issues: list[CleanupIssue] = []
        last_intent: PurgeIntentSnapshot | None = None
        while True:
            batch_cursor: str | None = None
            batch_issues: tuple[CleanupIssue, ...] = ()
            with self.db.writer() as conn:
                intent = self._purge_intent_snapshot(conn, int(doc_id))
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
                batch_cursor, batch_issues = self._process_purge_cleanup_batch(
                    conn, intent, after_norm=cursor
                )
            issues.extend(batch_issues)
            if batch_cursor is None:
                break
            cursor = batch_cursor

        assert last_intent is not None
        if issues:
            with self.db.reader() as conn:
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
            with self.db.writer() as conn:
                intent = self._purge_intent_snapshot(conn, int(doc_id))
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

                trash = fp.managed_path(self.vault, intent.path, namespace="trash")
                trash_signature = fp.confined_file_signature(self.vault, trash, missing_ok=True)
                if trash_signature is not None:
                    if not fp.unlink_regular(trash, expected=trash_signature, vault=self.vault):
                        raise fp.FileGenerationChanged(
                            f"purge trash changed during removal: {trash}"
                        )
                elif not fp.confirm_confined_absence(self.vault, trash):
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

    def _project_current(self, doc_id: int, *, max_attempts: int = 3) -> fp.ProjectionResult:
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
            with self.db.reader() as conn:
                snapshot = self._projection_snapshot(conn, int(doc_id))
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
                with self.db.writer() as conn:
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
                live_target = fp.managed_path(self.vault, snapshot.path, namespace="live")
                trash_target = fp.managed_path(self.vault, snapshot.path, namespace="trash")
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
                staged = fp.stage_text(self.vault, target, canonical_body)
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
                with self.db.writer() as conn:
                    token_state = self._projection_token_state(conn, snapshot, allow_cleanup=True)
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
                        file_mtime = self._install_projection_target(
                            snapshot,
                            staged,
                            target,
                            live_target,
                            trash_target,
                        )
                        current_installed = True
                        if token_state == "current":
                            self._mark_projection_clean(conn, snapshot, file_mtime)
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
                with self.db.writer() as conn:
                    token_state = self._projection_token_state(conn, snapshot, allow_cleanup=True)
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
                        batch_cursor, batch_issues = self._process_cleanup_batch(
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
                final_staged = fp.stage_text(self.vault, target, canonical_body)
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
                with self.db.writer() as conn:
                    token_state = self._projection_token_state(conn, snapshot)
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
                        file_mtime = self._install_projection_target(
                            snapshot,
                            final_staged,
                            target,
                            live_target,
                            trash_target,
                        )
                        current_installed = True
                        self._mark_projection_clean(conn, snapshot, file_mtime)
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

    def _require_projection(self, doc_id: int) -> fp.ProjectionResult:
        result = self._project_current(doc_id)
        if not result.settled:
            raise ProjectionPendingError(result)
        return result

    def _recover_pending_report(self, *, page_size: int = 64) -> RecoveryReport:
        """Visit a bounded ID frontier and continue after per-document failures."""
        page_size = clamp_int(page_size, 1, 1024)
        pending_where = (
            "d.file_state='pending' "
            "OR EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
            "OR EXISTS(SELECT 1 FROM file_projection_cleanup c WHERE c.doc_id=d.id)"
        )
        with self.db.reader() as conn:
            max_id = int(
                conn.execute(
                    f"SELECT COALESCE(MAX(d.id),0) FROM documents d WHERE {pending_where}"
                ).fetchone()[0]
            )
        cursor = 0
        recovered = 0
        issues: list[fp.ProjectionResult] = []
        while cursor < max_id:
            with self.db.reader() as conn:
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
                    with self.db.reader() as conn:
                        has_purge_intent = (
                            conn.execute(
                                "SELECT 1 FROM document_purge_intents WHERE doc_id=?",
                                (current_id,),
                            ).fetchone()
                            is not None
                        )
                    result = (
                        self._finish_purge(current_id)
                        if has_purge_intent
                        else self._project_current(current_id)
                    )
                    if result.reason == "purge_pending":
                        result = self._finish_purge(current_id)
                except Exception as exc:
                    log.exception("recover_pending: document %d raised unexpectedly", current_id)
                    error_path = None
                    try:
                        with self.db.reader() as conn:
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

    # ---- idempotency ----------------------------------------------------
    def _idem_lookup(self, scope: str, user_id: int, key: str) -> dict | None:
        """Return the cached result of a previously-applied write with this
        (scope, user, key), or None if the key is new. Lets a client safely retry
        a write whose response was lost without applying it twice."""
        with self.db.reader() as conn:
            row = conn.execute(
                "SELECT result_path, result_version FROM idempotency_keys "
                "WHERE scope=? AND user_id=? AND idem_key=?",
                (scope, user_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "ok": True,
            "path": row["result_path"],
            "version": row["result_version"],
            "deduplicated": True,
        }

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
            tags = [
                t[0]
                for t in conn.execute(
                    "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (d["id"],)
                )
            ]
            return {
                "path": d["path"],
                "title": d["title"],
                "content": body,
                "version": d["version"],
                "tags": tags,
                "folder": d["folder"],
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
                "updated_by": self._username(conn, d["updated_by"]),
                "last_via": lv["via"] if lv else None,
            }

    def merge_preview(
        self, principal: Principal, path: str, base_version: int, mine: str
    ) -> dict:
        """Build a non-persisting three-way merge proposal from an exact revision."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        rel = normalize_rel_path(path)
        fp.managed_path(self.vault, rel, namespace="live")
        norm = path_norm(rel)
        requested_version = int(base_version)
        with self.db.reader() as conn:
            row = conn.execute(
                "SELECT d.id,d.version,d.is_deleted,d.updated_at,u.username AS updated_by,"
                "current.body AS current_body,current.via AS current_via,base.body AS base_body "
                "FROM documents d "
                "LEFT JOIN revisions current "
                "ON current.doc_id=d.id AND current.version=d.version "
                "LEFT JOIN revisions base "
                "ON base.doc_id=d.id AND base.version=? "
                "LEFT JOIN users u ON u.id=d.updated_by WHERE d.path_norm=?",
                (requested_version, norm),
            ).fetchone()
            if row is None or row["is_deleted"]:
                raise NotFoundError("No document at this path.", path=rel)
            if row["current_body"] is None:
                raise RuntimeError("current document revision is missing or corrupt")
            current = str(row["current_body"])
            current_version = int(row["version"])
            base = row["base_body"]

        preview = {
            "base_version": requested_version,
            "current_version": current_version,
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "current_via": row["current_via"],
            "base": base,
            "mine": mine,
            "current": current,
            "merged": None,
            "conflicts": [],
            "manual_only": base is None,
        }
        if base is None:
            return preview

        result = three_way_merge(str(base), mine, current)
        preview["merged"] = result.text
        # The merge engine indexes Python code points; browser String.slice() uses
        # UTF-16 code units. Convert the exact merged prefix before serializing.
        preview["conflicts"] = [
            {
                "start_line": hunk.start_line,
                "base": hunk.base,
                "mine": hunk.mine,
                "current": hunk.current,
                "resolved": hunk.resolved,
                "merged_start": len(
                    result.text[: cast(int, hunk.merged_start)].encode("utf-16-le")
                )
                // 2,
            }
            for hunk in result.conflicts
        ]
        return preview

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
            tags = [
                t[0]
                for t in conn.execute(
                    "SELECT tag FROM tags WHERE doc_id=? ORDER BY tag", (d["id"],)
                )
            ]
            return {
                "path": d["path"],
                "title": d["title"],
                "version": d["version"],
                "tags": tags,
                "folder": d["folder"],
                "created_at": d["created_at"],
                "updated_at": d["updated_at"],
                "updated_by": self._username(conn, d["updated_by"]),
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
                "SELECT id, path, version FROM documents WHERE path_norm=? AND is_deleted=0",
                (norm,),
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
                    path=rel,
                    ordinal=int(ordinal),
                    chunk_count=total,
                )
            chunks = [
                {
                    "ordinal": r["ordinal"],
                    "heading": r["heading"],
                    "heading_path": r["heading_path"],
                    "text": r["text"],
                    "char_start": r["char_start"],
                    "char_end": r["char_end"],
                    "anchor": heading_slug(r["heading"]) if r["heading"] else None,
                }
                for r in rows
            ]
            return {
                "path": d["path"],
                "version": d["version"],
                "ordinal": int(ordinal),
                "chunk_count": total,
                "char_start": chunks[0]["char_start"],
                "char_end": chunks[-1]["char_end"],
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

    def list_docs(
        self, folder=None, tag=None, limit=100, offset=0, sort="updated_at", tags=None
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
                {"path": r["path"], "title": r["title"] or r["path"]}
            )

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
            raise ForbiddenError(
                f"Role '{principal.role}' cannot create folders (read/search only)."
            )
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
            audit.record(
                conn,
                actor=principal.username,
                via=principal.via,
                action="folder_create",
                target=rel,
            )
        safe_join(self.vault, rel).mkdir(parents=True, exist_ok=True)
        self._bump_nav()
        return {"ok": True, "path": rel}

    def delete_folder(self, principal: Principal, path: str) -> dict:
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
        with self.db.writer() as conn:
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
        target = safe_join(self.vault, rel)
        if target.is_dir():
            for root_, _dirs, _files in os.walk(target, topdown=False):
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
                "GROUP BY t.tag ORDER BY count DESC, t.tag ASC LIMIT ?",
                (n,),
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
            d = conn.execute(
                "SELECT id, version FROM documents WHERE path_norm=?", (norm,)
            ).fetchone()
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
            return {
                "path": rel,
                "version": r["version"],
                "title": r["title"],
                "content": r["body"],
                "op": r["op"],
                "via": r["via"],
                "author": r["author"],
                "created_at": r["created_at"],
            }

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
            rows = {
                r["version"]: r
                for r in conn.execute(
                    "SELECT version, body, title FROM revisions WHERE doc_id=? AND version IN (?,?)",
                    (d["id"], int(from_version), int(to_version)),
                )
            }
        fr, to = rows.get(int(from_version)), rows.get(int(to_version))
        if fr is None:
            raise NotFoundError(f"No revision {from_version} for this document.", path=rel)
        if to is None:
            raise NotFoundError(f"No revision {to_version} for this document.", path=rel)
        diff: list[dict] = []
        added = deleted = 0
        for line in difflib.unified_diff(
            (fr["body"] or "").splitlines(), (to["body"] or "").splitlines(), lineterm="", n=3
        ):
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
        return {
            "path": rel,
            "from_version": int(from_version),
            "to_version": int(to_version),
            "from_title": fr["title"],
            "to_title": to["title"],
            "diff": diff,
            "summary": {"lines_added": added, "lines_deleted": deleted},
        }

    def backlinks(self, path: str, *, with_context: bool = False) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            return {
                "path": rel,
                "backlinks": graph.get_backlinks(conn, d["id"], with_context=with_context),
            }

    def links(self, path: str) -> dict:
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.reader() as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0", (norm,)
            ).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            return {"path": rel, "links": graph.get_outgoing(conn, d["id"])}

    def graph(self, root=None, depth=1, limit=500, include_unresolved=True) -> dict:
        with self.db.reader() as conn:
            return graph.build_graph(conn, root, depth, limit, include_unresolved)

    def search_page(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        top_k: int = 10,
        folder: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        offset: int = 0,
        parsed_query: search.ParsedQuery | None = None,
        candidate_k: int | None = None,
    ) -> tuple[list[search.SearchResult], bool]:
        """Hybrid search returning ``(results, truncated)``, applying this service's
        configured fusion tuning (``search_params``). The single entry point both the
        web and MCP surfaces go through so tuning is honored uniformly. ``since``/``until``
        bound hits by ``updated_at`` (recency filter)."""
        try:
            return search.search_page(
                self.db,
                self.embedder,
                query,
                mode=mode,
                top_k=top_k,
                folder=folder,
                tags=tags,
                since=since,
                until=until,
                params=self.search_params,
                offset=offset,
                parsed_query=parsed_query,
                candidate_k=candidate_k,
            )
        except EmbeddingBindingChanged as exc:
            raise EmbeddingUnavailableError(_EMBEDDING_UNAVAILABLE_MESSAGE) from exc

    def search_workbench_page(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        page: int = 1,
        per_page: int = 20,
        folder: str | None = None,
        tags: list[str] | None = None,
    ) -> SearchPage:
        """Return stable web pagination while preserving the legacy search API."""
        page = max(1, int(page))
        per_page = clamp_int(per_page, 1, 50)
        mode = mode if mode in ("hybrid", "bm25", "vector") else "hybrid"
        normalized_folder = folder or ""
        normalized_tags = tuple(tag.strip() for tag in tags or () if tag.strip())
        parsed = search.parse_query(query)
        offset = (page - 1) * per_page
        available = max(0, SEARCH_WORKBENCH_MAX_RESULTS - offset)
        if available:
            items, has_next = self.search_page(
                query,
                mode=mode,
                top_k=min(per_page, available),
                folder=normalized_folder or None,
                tags=list(normalized_tags) or None,
                offset=offset,
                parsed_query=parsed,
                candidate_k=SEARCH_WORKBENCH_MAX_RESULTS,
            )
        else:
            items, has_next = [], False
        frozen_items = tuple(items)
        bounded = offset >= SEARCH_WORKBENCH_MAX_RESULTS or (
            len(frozen_items) == available and available <= per_page
        )
        if bounded:
            has_next = False
        if has_next or bounded or mode != "bm25" or (page > 1 and not frozen_items):
            total_or_more = None
        else:
            total_or_more = offset + len(frozen_items)
        filters = SearchFilters(
            query=query,
            mode=mode,
            folder=normalized_folder,
            tags=normalized_tags,
            normalized=tuple(
                NormalizedSearchFilter(operator, value)
                for operator, value in parsed.filters.normalized
            ),
        )
        return SearchPage(
            items=frozen_items,
            total_or_more=total_or_more,
            page=page,
            per_page=per_page,
            has_prev=page > 1,
            has_next=has_next,
            bounded=bounded,
            filters=filters,
        )

    def related(self, path: str, limit: int = 8) -> dict:
        """Documents semantically similar to this one (via the shared chunk-vector
        index). Empty list when the document has no embeddings yet."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        try:
            expected = self.db.expected_embedding_binding()
            with self.db.embedding_read_snapshot(expected) as conn:
                d = conn.execute(
                    "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0",
                    (norm,),
                ).fetchone()
                if not d:
                    raise NotFoundError("No document at this path.", path=rel)
                items = search._related_documents(conn, d["id"], k=limit)
        except EmbeddingBindingChanged as exc:
            raise EmbeddingUnavailableError(_EMBEDDING_UNAVAILABLE_MESSAGE) from exc
        return {"path": rel, "related": items}

    def assemble_context(
        self,
        question: str,
        *,
        max_chars: int = 6000,
        max_sources: int = 8,
        mode: str = "hybrid",
        folder: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Retrieve + assemble citation-tagged context for a question (RAG primitive)."""
        if not question or not question.strip():
            raise ValidationError("question must not be empty.")
        try:
            return search.assemble_context(
                self.db,
                self.embedder,
                question,
                max_chars=max_chars,
                max_sources=max_sources,
                mode=mode,
                folder=folder,
                tags=tags,
                params=self.search_params,
            )
        except EmbeddingBindingChanged as exc:
            raise EmbeddingUnavailableError(_EMBEDDING_UNAVAILABLE_MESSAGE) from exc

    # ---- llms.txt corpus export (agent-facing site map / full ingest) ----
    @contextmanager
    def _corpus_read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Keep one read snapshot for an export without owning caller transactions."""
        with self.db.reader() as conn:
            owned = not conn.in_transaction
            if owned:
                conn.execute("BEGIN")
            try:
                # A long reader is intentional here: totals and emitted rows must describe
                # the same corpus even when a writer commits during a streamed iteration.
                yield conn
            finally:
                if owned and conn.in_transaction:
                    conn.execute("ROLLBACK")

    def _iter_corpus_docs(
        self,
        folder: str | None = None,
        batch_size: int = 128,
        *,
        conn: sqlite3.Connection,
        body_max_chars: int | None = None,
    ) -> Iterator[dict]:
        """Yield ordered corpus metadata in batches, materializing one body at a time."""
        where = " WHERE d.is_deleted=0"
        params: list = []
        if folder:
            f = folder.strip("/")
            where += " AND (d.folder=? OR d.folder LIKE ?)"
            params += [f, f + "/%"]
        order = " ORDER BY d.folder, d.path"
        metadata_q = (
            "SELECT d.id, d.path, d.title, d.folder, d.updated_at FROM documents d" + where + order
        )
        body_params = list(params)
        if body_max_chars is None:
            body_column = "r.body"
        else:
            body_column = "substr(r.body, 1, ?) AS body"
            body_params.insert(0, max(0, int(body_max_chars)))
        body_q = (
            f"SELECT d.id, {body_column}, length(r.body) AS body_chars "
            "FROM documents d "
            "JOIN revisions r ON r.doc_id=d.id AND r.version=d.version" + where + order
        )
        batch_size = max(1, int(batch_size))
        metadata_cursor = conn.execute(metadata_q, params)
        body_cursor = conn.execute(body_q, body_params)
        while rows := metadata_cursor.fetchmany(batch_size):
            tags_by = self._tags_for_ids(conn, [r["id"] for r in rows])
            for r in rows:
                body_row = body_cursor.fetchone()
                if body_row is None or body_row["id"] != r["id"]:
                    raise RuntimeError("Corpus metadata and body cursors lost alignment.")
                yield {
                    "path": r["path"],
                    "title": r["title"] or r["path"],
                    "folder": r["folder"] or "",
                    "updated_at": r["updated_at"],
                    "tags": tags_by.get(r["id"], []),
                    "body": body_row["body"],
                    "body_chars": body_row["body_chars"],
                }

    def _corpus_count(
        self, folder: str | None = None, conn: sqlite3.Connection | None = None
    ) -> int:
        if conn is None:
            with self.db.reader() as read_conn:
                return self._corpus_count(folder, conn=read_conn)
        q = "SELECT COUNT(*) FROM documents d WHERE d.is_deleted=0"
        params: list = []
        if folder:
            f = folder.strip("/")
            q += " AND (d.folder=? OR d.folder LIKE ?)"
            params += [f, f + "/%"]
        return int(conn.execute(q, params).fetchone()[0])

    @staticmethod
    def _one_line(value: object) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _md_label(cls, value: object) -> str:
        return cls._one_line(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

    @staticmethod
    def _doc_description(body: str, max_chars: int = 120, body_chars: int | None = None) -> str:
        """A one-line description for the llms.txt index: a frontmatter
        ``description``/``summary`` if present, else the first non-empty body line
        with markdown markers stripped (single line, never HTML)."""
        meta, off = parse_frontmatter(body)
        if (
            body_chars is not None
            and len(body) < body_chars
            and not off
            and re.match(r"^---[ \t]*\n", body)
        ):
            return ""
        for key in ("description", "summary"):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())[:max_chars]
        for line in body[off:].splitlines():
            s = line.strip()
            # Skip blank lines and headings — a heading is ~the title we already show
            # as the link text, so the description should be the first prose line.
            if not s or s.startswith("#"):
                continue
            s = s.lstrip(">").strip().lstrip("-*").strip()
            if s:
                return " ".join(s.split())[:max_chars]
        return ""

    @staticmethod
    def _corpus_body_prefix(body: str, body_chars: int) -> tuple[str, bool]:
        """Strip complete frontmatter without exposing a prefix cut inside YAML."""
        prefix_truncated = len(body) < body_chars
        _meta, offset = parse_frontmatter(body)
        if offset:
            return body[offset:], prefix_truncated
        if prefix_truncated and re.match(r"^---[ \t]*\n", body):
            return "", True
        return body, prefix_truncated

    def _doc_raw_url(self, path: str, base_url: str = "") -> str:
        enc = quote(path)
        return f"{base_url.rstrip('/')}/doc/{enc}/raw" if base_url else f"/doc/{enc}/raw"

    def llms_index(self, *, site_title: str, base_url: str = "") -> str:
        """Render the vault as an ``llms.txt`` index (the emerging agent-facing site
        map, https://llmstxt.org/): an H1 title, a one-line blockquote summary, then
        an H2 section per folder listing each document as a markdown link to its raw
        (.md) source plus a short description — so any LLM, not just an MCP client,
        can discover what the knowledge base holds."""
        with self._corpus_read_snapshot() as conn:
            total = self._corpus_count(conn=conn)
            docs = self._iter_corpus_docs(
                conn=conn, body_max_chars=_CORPUS_DESCRIPTION_PREFIX_CHARS
            )
            lines = [
                f"# {self._md_label(site_title)}",
                "",
                f"> 마크다운 지식베이스 — 문서 {total}개. "
                "각 항목은 원문(.md) 링크이며, 전체 본문은 /llms-full.txt 로 한 번에 가져올 수 있습니다.",
                "",
            ]
            for folder, group in groupby(docs, key=lambda d: d["folder"]):
                lines.append(f"## {self._md_label(folder or '루트')}")
                for d in group:
                    desc = self._one_line(
                        self._doc_description(d["body"], body_chars=d["body_chars"])
                    )
                    url = self._doc_raw_url(d["path"], base_url)
                    title = self._md_label(d["title"])
                    lines.append(f"- [{title}]({url})" + (f": {desc}" if desc else ""))
                lines.append("")
            return "\n".join(lines).rstrip() + "\n"

    def llms_full(self, *, site_title: str, max_chars: int = 2_000_000) -> dict:
        """Render the whole vault as one concatenated markdown document
        (``llms-full.txt``): each document's full body, prefixed by a path/tags/updated
        header and separated by a horizontal rule, so an agent can ingest the entire
        corpus in a single request. Emission stops once ``max_chars`` of content is
        reached (``truncated=True``), bounding the response for very large vaults."""
        limit = max(0, int(max_chars))
        with self._corpus_read_snapshot() as conn:
            total = self._corpus_count(conn=conn)
            parts = [f"# {self._md_label(site_title)}\n\n> 전체 코퍼스 export — 문서 {total}개.\n"]
            included = 0
            truncated = False

            def marker(count: int) -> str:
                return (
                    f"\n---\n\n> [truncated] {count}/{total} 문서만 포함되었습니다. "
                    "나머지는 /llms.txt 색인이나 개별 문서로 가져오세요.\n"
                )

            size = len(parts[0])
            if size <= limit:
                for d in self._iter_corpus_docs(conn=conn, body_max_chars=limit - size):
                    body, body_prefix_truncated = self._corpus_body_prefix(
                        d["body"], d["body_chars"]
                    )
                    body = body.strip()
                    header = (
                        f"---\n\n# {self._md_label(d['title'])}\n\n"
                        f"- 경로: `{d['path']}`\n"
                        + (f"- 태그: {', '.join(d['tags'])}\n" if d["tags"] else "")
                        + f"- 수정: {d['updated_at']}\n"
                    )
                    block = header + "\n" + body + "\n"
                    separator = "\n"
                    candidate = separator + block
                    if not body_prefix_truncated and size + len(candidate) <= limit:
                        parts.append(candidate)
                        size += len(candidate)
                        included += 1
                        continue

                    remaining = limit - size
                    partial_marker = marker(included + 1)
                    block_budget = remaining - len(partial_marker)
                    if block_budget > len(separator):
                        parts.append(candidate[:block_budget])
                        included += 1
                        parts.append(partial_marker)
                    else:
                        parts.append(marker(included)[:remaining])
                    truncated = True
                    break
            else:
                truncated = True

            text = "".join(parts)
            return {
                "text": text[:limit],
                "included": included,
                "total": total,
                "truncated": truncated or len(text) > limit,
            }

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
    def create(
        self,
        principal: Principal,
        path: str,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        *,
        embed: bool = True,
    ) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot create documents (read/search only)."
            )
        rel = normalize_rel_path(path)
        # Reject internal namespaces and unsafe existing path components before the
        # canonical DB write commits. Parent directories may be created later by the
        # staged projector, after the row is durably marked pending.
        fp.managed_path(self.vault, rel, namespace="live")
        norm, folder, stem = path_norm(rel), folder_of(rel), basename_stem(rel).lower()
        content = content or ""
        meta = parse_frontmatter(content)[0]
        final_title = (title or derive_title(meta, content, rel)).strip()
        tagset = self._merge_tags(meta, content, tags)
        chash, now = sha256_hex(content), now_iso()

        with self.db.writer() as conn:
            row = conn.execute(
                "SELECT d.id,d.path,d.version,d.is_deleted,"
                "EXISTS(SELECT 1 FROM document_purge_intents p WHERE p.doc_id=d.id) "
                "AS has_purge_intent FROM documents d WHERE d.path_norm=?",
                (norm,),
            ).fetchone()
            if row and not row["is_deleted"]:
                raise self._conflict(
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
                fp.managed_path(self.vault, rel, namespace="live")
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
            self._set_tags(conn, doc_id, tagset)
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

        self._require_projection(int(doc_id))
        if embed:
            self._embed(int(doc_id))
        DOC_WRITES.labels("create").inc()
        self._emit(
            "create",
            rel,
            new_version,
            title=final_title,
            updated_by=principal.username,
            via=principal.via,
        )
        self._bump_nav()
        return self.get(rel)

    def update(
        self,
        principal: Principal,
        path: str,
        base_version: int | None,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        *,
        embed: bool = True,
        idempotency: tuple[str, int, str] | None = None,
    ) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        if base_version is None:
            raise ValidationError("base_version is required for updates.")
        rel = normalize_rel_path(path)
        fp.managed_path(self.vault, rel, namespace="live")
        norm, folder = path_norm(rel), folder_of(rel)
        content = content or ""
        meta = parse_frontmatter(content)[0]
        content_title = derive_content_title(meta, content)
        derived_tags = self._merge_tags(meta, content, tags)
        chash, now = sha256_hex(content), now_iso()

        with self.db.writer() as conn:
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
            current_tags = self._tags_for_ids(conn, [doc_id]).get(doc_id, [])
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
                raise self._conflict(conn, doc_id, rel)
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
            self._set_tags(conn, doc_id, tagset)
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
                scope, uid, key = idempotency
                conn.execute(
                    "INSERT INTO idempotency_keys(scope, user_id, idem_key, doc_id, "
                    "result_version, result_path, created_at) VALUES(?,?,?,?,?,?,?)",
                    (scope, uid, key, doc_id, new_version, rel, now),
                )
                cutoff = (datetime.now(UTC) - timedelta(days=_IDEM_RETENTION_DAYS)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (cutoff,))

        self._require_projection(int(doc_id))
        if content_changed and embed:
            self._embed(int(doc_id))
        DOC_WRITES.labels("update").inc()
        self._emit(
            "update",
            rel,
            new_version,
            title=final_title,
            updated_by=principal.username,
            via=principal.via,
            content_changed=content_changed,
        )
        self._bump_nav()
        return self.get(rel)

    # ---- targeted edits (token-cheap; funnel through the CAS update path) ----
    def get_section(self, path: str, heading: str, occurrence: int = 1) -> dict:
        doc = self.get(path)
        lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
        return {
            "path": doc["path"],
            "heading": heading,
            "occurrence": occurrence,
            "version": doc["version"],
            "tags": doc["tags"],
            "content": "".join(lines[start:end]),
        }

    def outline(self, path: str) -> dict:
        """Flat heading outline of a document: [{level, text, line}] (1-based lines).
        Lets an agent discover exact heading strings before a section read/edit."""
        doc = self.get(path)
        headings: list[dict] = []
        for i, line in enumerate(doc["content"].splitlines()):
            m = _HEADING_RE.match(line)
            if m:
                headings.append(
                    {"level": len(m.group(1)), "text": m.group(2).strip(), "line": i + 1}
                )
        return {"path": doc["path"], "version": doc["version"], "headings": headings}

    def replace_section(
        self,
        principal: Principal,
        path: str,
        heading: str,
        text: str,
        base_version: int | None = None,
        occurrence: int = 1,
    ) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        doc = self.get(path)
        lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
        # Keep the heading line; replace its body up to the next same/higher heading.
        body = "".join(lines[: start + 1]) + _as_block(text) + "".join(lines[end:])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, body)

    def append_section(
        self,
        principal: Principal,
        path: str,
        heading: str,
        text: str,
        base_version: int | None = None,
        occurrence: int = 1,
    ) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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

    def append_to_document(
        self,
        principal: Principal,
        path: str,
        text: str,
        ensure_heading: str | None = None,
        base_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
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
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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
        except (sqlite3.IntegrityError, ConflictError):
            # A concurrent request with the same key committed between our pre-check and
            # our commit. Depending on whether it won before or after our CAS, the loser
            # sees either the key's UNIQUE constraint or the newer document version. Its
            # transaction rolled back in both cases, so replay the original result.
            cached = (
                self._idem_lookup("append", principal.user_id, idempotency_key)
                if idempotency_key
                else None
            )
            if cached is not None:
                return cached
            raise

    def patch(
        self,
        principal: Principal,
        path: str,
        find: str,
        replace: str,
        base_version: int | None = None,
        count: int = 1,
        mode: str = "literal",
        occurrence: int | None = None,
    ) -> dict:
        """Find-and-replace a substring (``mode='literal'``) or a regular expression
        (``mode='regex'``, ``re.MULTILINE``; ``replace`` may use ``\\1`` backrefs).
        ``occurrence`` (1-based) targets a single match deterministically — the way
        out of "appears N times" failures on repetitive content; otherwise ``count``
        bounds how many matches may be replaced (0/None = all)."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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
                pat = regex_lib.compile(find, regex_lib.MULTILINE)
            except regex_lib.error as e:
                raise ValidationError(f"invalid regex: {e}") from None
            try:
                matches = list(
                    islice(
                        pat.finditer(content, timeout=REGEX_TIMEOUT_S),
                        MAX_PATCH_MATCHES + 1,
                    )
                )
                if len(matches) > MAX_PATCH_MATCHES:
                    raise ValidationError(
                        f"Pattern matches more than {MAX_PATCH_MATCHES} times; narrow the pattern."
                    )
                n = len(matches)
                if n == 0:
                    raise NotFoundError("Pattern not found; nothing patched.", path=rel)
                if occurrence is not None:
                    if occurrence > n:
                        raise ValidationError(f"occurrence {occurrence} out of range (1..{n}).")
                    m = matches[occurrence - 1]
                    new_body = content[: m.start()] + m.expand(replace) + content[m.end() :]
                else:
                    if count and n > count:
                        raise ValidationError(
                            f"Pattern matches {n} times (limit {count}); narrow it, pass "
                            f"'occurrence', or raise 'count'."
                        )
                    new_body = pat.sub(replace, content, count=count or 0, timeout=REGEX_TIMEOUT_S)
            except TimeoutError:
                raise ValidationError("regex evaluation timed out; narrow the pattern.") from None
        else:
            occurrences = content.count(find)
            if occurrences == 0:
                raise NotFoundError("Search text not found; nothing patched.", path=rel)
            if occurrence is not None:
                if occurrence > occurrences:
                    raise ValidationError(
                        f"occurrence {occurrence} out of range (1..{occurrences})."
                    )
                idx = -len(find)
                for _ in range(occurrence):
                    idx = content.find(find, idx + len(find))
                new_body = content[:idx] + replace + content[idx + len(find) :]
            else:
                if count and occurrences > count:
                    raise ValidationError(
                        f"Search text appears {occurrences} times (limit {count}); make it more "
                        f"specific, pass 'occurrence', or raise 'count'."
                    )
                new_body = content.replace(find, replace, count if count else -1)

        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, rel, bv, new_body)

    def restore_revision(
        self, principal: Principal, path: str, version: int, base_version: int | None = None
    ) -> dict:
        """Replay a past revision's body as a new edit (one CAS update) — a server-side
        undo. The old body is loaded here and never has to travel through the caller,
        so reverting a large document is a single small call. ``base_version`` defaults
        to the current version; pass it to reject the revert with 'conflict' if the
        document changed since you looked."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        rev = self.revision(path, int(version))
        bv = self.get(rev["path"])["version"] if base_version is None else int(base_version)
        return self.update(principal, rev["path"], bv, rev["content"], title=rev["title"])

    def toggle_task(
        self,
        principal: Principal,
        path: str,
        line: int | None = None,
        *,
        index: int | None = None,
        base_version: int | None = None,
    ) -> dict:
        """Flip a single markdown task checkbox (``- [ ]`` <-> ``- [x]``), then save
        through the CAS update path. Target by 1-based ``line`` or by 0-based
        ``index`` (the Nth checkbox in document order — matches the rendered
        ``data-ti`` attribute used by click-to-toggle in the viewer)."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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

    def patch_tags(
        self,
        principal: Principal,
        path: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        """Add/remove tags by rewriting the frontmatter ``tags`` list (body untouched).
        Returns the document's resulting tags. Tags written inline as ``#hashtags``
        in the body are re-derived on save, so they cannot be removed this way."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        doc = self.get(path)
        current = set(doc["tags"])
        add_set = {str(t).strip().lstrip("#") for t in (add or []) if str(t).strip()}
        remove_set = {str(t).strip().lstrip("#") for t in (remove or []) if str(t).strip()}
        target = sorted((current | add_set) - remove_set)
        if target == sorted(current):  # no net change — stay idempotent, skip the version bump
            return {"path": doc["path"], "version": doc["version"], "tags": sorted(current)}
        new_content = set_frontmatter_tags(doc["content"], target)
        updated = self.update(principal, doc["path"], doc["version"], new_content, tags=target)
        return {"path": updated["path"], "version": updated["version"], "tags": updated["tags"]}

    def merge_tags(self, principal: Principal, sources: list[str], dest: str) -> dict:
        """Vault-wide tag cleanup (editor/admin only): rewrite every document's frontmatter
        ``tags`` so each of ``sources`` becomes ``dest``. Each affected document is updated
        through patch_tags (its own CAS revision), so this is not one transaction. Tags
        written inline as ``#hashtags`` in the body are NOT rewritten (patch_tags only
        manages the frontmatter list) — edit the body for those. Returns the dest, the
        normalized sources, and how many documents were touched."""
        if not principal.can_write:
            raise ForbiddenError(f"Role '{principal.role}' cannot rewrite tags (read/search only).")
        dest = str(dest or "").strip().lstrip("#")
        if not dest:
            raise ValidationError("dest tag must not be empty.")
        src = sorted(
            {str(s).strip().lstrip("#") for s in (sources or []) if str(s).strip()} - {dest}
        )
        if not src:
            raise ValidationError("no source tags to merge (after removing the dest tag).")
        ph = ",".join("?" * len(src))
        with self.db.reader() as conn:
            paths = [
                r["path"]
                for r in conn.execute(
                    f"SELECT DISTINCT d.path FROM tags t JOIN documents d ON d.id=t.doc_id "
                    f"WHERE t.tag IN ({ph}) AND d.is_deleted=0 ORDER BY d.path",
                    src,
                )
            ]
        changed = 0
        for p in paths:
            before = self.get(p)["version"]
            after = self.patch_tags(principal, p, add=[dest], remove=src)
            if (
                after["version"] != before
            ):  # patch_tags is a no-op (no version bump) when there's no net change
                changed += 1
        return {
            "ok": True,
            "dest": dest,
            "sources": src,
            "docs_affected": len(paths),
            "docs_changed": changed,
        }

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

    def set_property(
        self,
        principal: Principal,
        path: str,
        key: str,
        value: str | list[str],
        base_version: int | None = None,
    ) -> dict:
        """Set/replace one frontmatter property (body + other keys untouched), through
        the CAS update path. An empty value removes the key."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        key = self._validate_prop_key(key)
        doc = self.get(path)
        val = self._norm_prop_value(value)
        if not val:
            new_content = remove_frontmatter_property(doc["content"], key)
        else:
            new_content = set_frontmatter_property(
                doc["content"], key, val if isinstance(val, str) or len(val) > 1 else val[0]
            )
        if new_content == doc["content"]:
            return self.get(doc["path"])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, new_content)

    def remove_property(
        self, principal: Principal, path: str, key: str, base_version: int | None = None
    ) -> dict:
        """Remove one frontmatter property (no-op if absent), through CAS update."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
        key = self._validate_prop_key(key)
        doc = self.get(path)
        new_content = remove_frontmatter_property(doc["content"], key)
        if new_content == doc["content"]:
            return self.get(doc["path"])
        bv = doc["version"] if base_version is None else int(base_version)
        return self.update(principal, doc["path"], bv, new_content)

    def replace_properties(
        self,
        principal: Principal,
        path: str,
        props: list[tuple[str, list[str]]],
        base_version: int | None = None,
    ) -> dict:
        """Replace the whole editable property set in one revision: drops omitted keys,
        sets the rest. ``title``/``tags`` and the body are preserved. ``props`` is an
        ordered list of (key, values); empty value-lists drop the key."""
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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
                content = set_frontmatter_property(
                    content, key, vals[0] if len(vals) == 1 else vals
                )
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
        return {
            "from": rel,
            "to": new_rel,
            "dest_exists": self.exists(new_rel),
            "inbound_count": len(inbound),
            "inbound": [b["src_path"] for b in inbound],
        }

    def move(
        self, principal: Principal, path: str, new_path: str, fix_references: bool = False
    ) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot move documents (read/search only)."
            )
        rel, new_rel = normalize_rel_path(path), normalize_rel_path(new_path)
        norm, new_norm = path_norm(rel), path_norm(new_rel)
        if norm == new_norm:
            return self.get(rel)
        fp.managed_path(self.vault, new_rel, namespace="live")
        new_folder, new_stem = folder_of(new_rel), basename_stem(new_rel).lower()
        now = now_iso()
        with self.db.writer() as conn:
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
            source_target = fp.managed_path(self.vault, source_rel, namespace="live")
            source_signature = fp.confined_file_signature(
                self.vault, source_target, missing_ok=True
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
        self._require_projection(doc_id)
        DOC_WRITES.labels("move").inc()
        # Keyed on the OLD path so a viewer of the moved doc can follow it to `to`.
        self._emit(
            "move",
            source_rel,
            new_version,
            to=new_rel,
            updated_by=principal.username,
            via=principal.via,
        )
        result = self.get(new_rel)
        if fix_references:
            # Re-resolution above fixed the GRAPH, but bodies still contain the old
            # link text; rewrite those so the references don't show up broken.
            result = {
                **result,
                "references": self.rename_references(principal, source_rel, new_rel),
            }
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
            raise ForbiddenError(
                f"Role '{principal.role}' cannot modify documents (read/search only)."
            )
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
                self.update(principal, doc["path"], doc["version"], new_body)
            except ConflictError:
                skipped += 1
                continue
            docs_rewritten += 1
            links_rewritten += len(edits)
        return {
            "from": old_rel,
            "to": new_rel,
            "docs_rewritten": docs_rewritten,
            "links_rewritten": links_rewritten,
            "skipped_conflicts": skipped,
        }

    def recent_changes(
        self, limit: int = 20, since: str | None = None, until: str | None = None
    ) -> list[dict]:
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
            return [
                {
                    "path": r["path"],
                    "title": r["title"] or r["path"],
                    "version": r["version"],
                    "folder": r["folder"],
                    "tags": tags_by.get(r["id"], []),
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def daily_note(
        self, principal: Principal, date: str | None = None, *, folder: str = "daily"
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
        if self.exists(rel):
            return {**self.get(rel), "created": False}
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot create the daily note (read/search only)."
            )
        return {**self.create(principal, rel, f"# {date}\n\n", title=date), "created": True}

    def delete(self, principal: Principal, path: str, base_version: int | None = None) -> dict:
        if not principal.can_write:
            raise ForbiddenError(
                f"Role '{principal.role}' cannot delete documents (read/search only)."
            )
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        now = now_iso()
        with self.db.writer() as conn:
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
            fp.managed_path(self.vault, actual_rel, namespace="live")
            doc_id = int(row["id"])
            if base_version is not None and int(base_version) != row["version"]:
                raise self._conflict(conn, doc_id, actual_rel)
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
        self._require_projection(doc_id)
        DOC_WRITES.labels("delete").inc()
        self._emit(
            "delete", actual_rel, new_version, updated_by=principal.username, via=principal.via
        )
        self._bump_nav()
        return {"ok": True, "path": actual_rel, "deleted": True}

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
            return [
                {
                    "path": r["path"],
                    "title": r["title"] or r["path"],
                    "version": r["version"],
                    "folder": r["folder"],
                    "updated_at": r["updated_at"],
                    "deleted_by": self._username(conn, r["updated_by"]),
                }
                for r in rows
            ]

    def restore(self, principal: Principal, path: str) -> dict:
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
        with self.db.writer() as conn:
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
            fp.managed_path(self.vault, actual_rel, namespace="live")
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
        self._require_projection(doc_id)
        self._embed(doc_id)
        DOC_WRITES.labels("restore").inc()
        self._emit(
            "restore",
            actual_rel,
            new_version,
            updated_by=principal.username,
            via=principal.via,
        )
        self._bump_nav()
        return {
            "ok": True,
            "path": actual_rel,
            "version": new_version,
            "restored": True,
        }

    def purge(self, principal: Principal, path: str) -> dict:
        """Durably request and finish permanent deletion of a soft-deleted document."""
        if not principal.can_admin:
            raise ForbiddenError("Only an admin can permanently delete a document.")
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        doc_id: int | None = None
        actual_rel = rel

        for _attempt in range(3):
            with self.db.reader() as conn:
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
                projection = self._project_current(doc_id)
                if (
                    not projection.settled
                    and not projection.current_installed
                    and projection.reason != "purge_pending"
                ):
                    raise ProjectionPendingError(projection)

            retry = False
            with self.db.writer() as conn:
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
                            live = fp.managed_path(self.vault, actual_rel, namespace="live")
                            if not fp.confirm_confined_absence(self.vault, live):
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
                                    )
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
        result = self._finish_purge(doc_id)
        if not result.settled:
            raise ProjectionPendingError(result)
        self._bump_nav()
        return {"ok": True, "path": actual_rel, "purged": True}

    # ---- favorites (per-user pins) --------------------------------------
    def toggle_favorite(self, principal: Principal, path: str) -> dict:
        """Flip whether the current user has pinned this document. Per-user and content-
        neutral: it creates no revision and needs no write permission (a reader may pin
        what they read). Returns the resulting ``favorite`` state."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.writer() as conn:
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

    def set_favorite(self, principal: Principal, path: str, favorite: bool) -> dict:
        """Idempotently set whether the current user has pinned this document (unlike
        toggle_favorite, the resulting state is the one you asked for — friendlier for an
        agent than a flip). Per-user, content-neutral, no write permission required."""
        rel = normalize_rel_path(path)
        norm = path_norm(rel)
        with self.db.writer() as conn:
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

    def is_favorite(self, user_id: int, path: str) -> bool:
        norm = path_norm(normalize_rel_path(path))
        with self.db.reader() as conn:
            r = conn.execute(
                "SELECT 1 FROM favorites f JOIN documents d ON d.id=f.doc_id "
                "WHERE f.user_id=? AND d.path_norm=? AND d.is_deleted=0",
                (user_id, norm),
            ).fetchone()
        return r is not None

    def list_favorites(self, user_id: int) -> list[dict]:
        """The user's pinned documents (live only), title-sorted — for the sidebar
        favourites section and a favourites view."""
        with self.db.reader() as conn:
            rows = conn.execute(
                "SELECT d.path, d.title FROM favorites f JOIN documents d ON d.id=f.doc_id "
                "WHERE f.user_id=? AND d.is_deleted=0 ORDER BY d.title COLLATE NOCASE",
                (user_id,),
            ).fetchall()
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
                f"{', '.join(sorted(ALLOWED_ATTACH_EXTS))}."
            )
        if not data:
            raise ValidationError("Empty upload.")
        if len(data) > ATTACH_MAX_BYTES:
            raise ValidationError(
                f"Attachment too large ({len(data)} bytes; limit {ATTACH_MAX_BYTES})."
            )
        sub = _attachment_subname(name, ext, data)
        target = safe_join(self.vault / ATTACH_DIR, sub)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():  # content-addressed: skip rewrite of an identical file
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        url = "/attachments/" + quote(sub)
        alt = name[: len(name) - len(ext)] or "file"
        return {"path": f"{ATTACH_DIR}/{sub}", "url": url, "markdown": f"![{alt}]({url})"}

    # ---- maintenance ----------------------------------------------------
    def recover_pending(self) -> int:
        """Finish a bounded frontier of pending projections and report issues in logs."""
        report = self._recover_pending_report()
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
            log.info(
                "revision prune: deleted %d revision(s), keeping latest %d per document",
                deletable,
                keep,
            )
        return {"keep": keep, "deletable_revisions": deletable, "applied": bool(apply)}

    @staticmethod
    def _reindex_snapshot_from_row(row: sqlite3.Row) -> ReindexTargetSnapshot:
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

    @classmethod
    def _reindex_target_snapshot(
        cls, conn: sqlite3.Connection, norm: str
    ) -> ReindexTargetSnapshot | None:
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
        return cls._reindex_snapshot_from_row(row)

    @classmethod
    def _reindex_document_snapshot(
        cls, conn: sqlite3.Connection, doc_id: int
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
        return cls._reindex_snapshot_from_row(row)

    def reindex_all(
        self, reembed: bool = False, progress: Callable[[int, int], None] | None = None
    ) -> dict:
        """Adopt only stable external generations behind exact DB and file fences."""
        vault = self.vault.resolve(strict=True)
        log.info("reindex: scanning vault %s (reembed=%s)", vault, reembed)

        recovered_pending = 0

        def recover_one(target_id: int) -> fp.ProjectionResult:
            nonlocal recovered_pending
            try:
                with self.db.reader() as conn:
                    purge_requested = (
                        conn.execute(
                            "SELECT 1 FROM document_purge_intents WHERE doc_id=?",
                            (target_id,),
                        ).fetchone()
                        is not None
                    )
                result = (
                    self._finish_purge(target_id)
                    if purge_requested
                    else self._project_current(target_id)
                )
            except Exception as exc:
                log.exception("reindex: projection recovery failed for document %d", target_id)
                error_path = None
                with suppress(Exception):
                    with self.db.reader() as conn:
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

        initial_recovery = self._recover_pending_report()
        recovered_pending += initial_recovery.recovered

        grouped: dict[str, list[tuple[str, Path]]] = {}
        scan_retry_entries: list[tuple[str, str, Path, str]] = []
        invalid_scan_conflicts: dict[str, dict] = {}
        for path in sorted(vault.rglob("*.md"), key=lambda item: item.as_posix()):
            relative = path.relative_to(vault)
            if relative.parts[0].casefold() in (".trash", ".tmp"):
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
                with self.db.reader() as conn:
                    target_snapshot = self._reindex_target_snapshot(conn, norm)
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
                    with self.db.reader() as conn:
                        target_snapshot = self._reindex_target_snapshot(conn, norm)
                    if had_target and target_snapshot is None:
                        managed_superseded = True

                try:
                    stable = fp.read_stable_markdown(vault, path)
                except fp.StableFileError as exc:
                    if exc.reason == "file_disappeared":
                        with self.db.reader() as conn:
                            latest = self._reindex_target_snapshot(conn, norm)
                        if (
                            managed_superseded
                            or cleanup_removed
                            or (
                                target_snapshot is not None
                                and (latest is None or latest.is_deleted)
                            )
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
                tagset = self._merge_tags(meta, content, None)
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
                    with self.db.writer() as conn:
                        current = self._reindex_target_snapshot(conn, norm)
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
                                    old_target = fp.managed_path(
                                        vault, current.path, namespace="live"
                                    )
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
                                self._set_tags(conn, current.doc_id, tagset)
                                indexing.publish_prepared(
                                    conn, current.doc_id, title, folder, prepared
                                )
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
                                expected = self._expected_cleanup_signature(cleanup)
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
                                        snapshot := self._reindex_target_snapshot(
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
                                    self._reindex_snapshot_from_row(candidate)
                                    for candidate in candidate_rows
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
                                self._set_tags(conn, doc_id, tagset)
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
                if (
                    committed_best is None
                    or committed_priority > outcome_priority[committed_best[0]]
                ):
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
                        if snapshot.doc_id != doc_id
                        and snapshot.doc_id not in ignored_source_owner_ids
                    }
                    with self.db.reader() as conn:
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
                            if (snapshot := self._reindex_document_snapshot(conn, peer_id))
                            is not None
                        }
                    if set(post_peers) != set(expected_peers):
                        source_peer_changed = True
                    else:
                        for peer_id, post_peer in post_peers.items():
                            expected_peer, expected_absent = expected_peers[peer_id]
                            post_absent = inspect_source_absence(post_peer, attempt)
                            with self.db.reader() as conn:
                                verified_peer = self._reindex_document_snapshot(conn, peer_id)
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
                            post_source_target = fp.managed_path(
                                vault, renamed_from, namespace="live"
                            )
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

        final_recovery = self._recover_pending_report()
        recovered_pending += final_recovery.recovered
        for conflict_path, conflict in list(conflicts.items()):
            if conflict["reason"] != "pending_projection":
                continue
            if conflict_path in pending_dependency_paths:
                continue
            with self.db.reader() as conn:
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

        with self.db.reader() as conn:
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
                if (snapshot := self._reindex_document_snapshot(conn, doc_id)) is not None
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
            with self.db.reader() as conn:
                current = self._reindex_document_snapshot(conn, snapshot.doc_id)
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
        embedded = indexing.embed_pending(self.db, self.embedder, progress=progress)
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
        self._bump_nav()
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

    # ---- bulk import ----------------------------------------------------
    def import_from_directory(
        self,
        principal: Principal,
        source_dir: str | Path,
        into: str = "",
        *,
        on_conflict: str = "skip",
        include: tuple[str, ...] = IMPORT_DEFAULT_INCLUDE,
        recurse: bool = True,
        import_attachments: bool = False,
        embed: bool = True,
        dry_run: bool = False,
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
                f"Role '{principal.role}' cannot import documents (read/search only)."
            )
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
            "created": 0,
            "revived": 0,
            "overwritten": 0,
            "skipped": 0,
            "renamed": 0,
            "scanned": 0,
            "embedded": 0,
            "attachments": {"copied": 0, "skipped": 0},
            "plan": [],
            "warnings": [],
            "errors": [],
            "broken_links": [],
            "dry_run": dry_run,
        }
        warn = report["warnings"].append
        claimed: set[str] = set()  # path_norm of targets created/planned this run
        imported: set[str] = set()  # path_norm actually written (broken-link report)
        asset_cache: dict[str, str] = {}  # resolved asset abs-path -> attachment url

        # -- attachment copy (only with import_attachments) -----------------
        def copy_asset(relpath: str, md_abs: Path) -> str | None:
            ref = relpath.split("#", 1)[0].strip()
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
                    warn(
                        f"missing asset {relpath} referenced by {md_abs.name} (left as broken link)"
                    )
                report["attachments"]["skipped"] += 1
                return None
            key = str(candidate)
            if key in asset_cache:
                return asset_cache[key]
            ext = candidate.suffix.lower()
            if ext not in IMPORT_ATTACH_EXTS:
                warn(
                    f"unsupported asset {relpath} ({ext or 'no ext'}) in {md_abs.name}; left as-is"
                )
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
                audit.record_tx(
                    self.db,
                    actor=principal.username,
                    via=principal.via,
                    action="attachment_upload",
                    target=res["path"],
                )
            asset_cache[key] = url
            if newly:
                report["attachments"]["copied"] += 1
                report["plan"].append(
                    {
                        "src": relpath,
                        "target": f"{ATTACH_DIR}/{sub}",
                        "action": "attach",
                        "reason": None,
                    }
                )
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
                for n in range(2, _IMPORT_RENAME_MAX_SUFFIX + 1):
                    cand = f"{base}-{n}.md"
                    cnorm = path_norm(cand)
                    if cnorm in claimed:
                        continue
                    if conn.execute(
                        "SELECT 1 FROM documents WHERE path_norm=?", (cnorm,)
                    ).fetchone():
                        continue
                    return cand
            raise ValidationError(
                f"no free rename variant for {target_rel} ({_IMPORT_RENAME_MAX_SUFFIX} tried)."
            )

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
                    audit.record_tx(
                        self.db,
                        actor=principal.username,
                        via=principal.via,
                        action="doc_import_skip",
                        target=source_rel,
                        outcome="skipped",
                        detail="file too large",
                    )
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
                    (norm,),
                ).fetchone()
            in_batch = norm in claimed
            live = bool(row and not row["is_deleted"])

            # Idempotent re-run: identical content already live -> no-op skip.
            if live and row["content_hash"] == chash:
                report["plan"].append(
                    {
                        "src": source_rel,
                        "target": target_rel,
                        "action": "skip",
                        "reason": "unchanged",
                    }
                )
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
                    warn(
                        f"case collision: {source_rel} maps to an already-imported path "
                        f"({target_rel}); applying on_conflict={on_conflict}"
                    )
                if on_conflict == "skip":
                    report["plan"].append(
                        {
                            "src": source_rel,
                            "target": target_rel,
                            "action": "skip",
                            "reason": "exists",
                        }
                    )
                    report["skipped"] += 1
                    if not dry_run:
                        audit.record_tx(
                            self.db,
                            actor=principal.username,
                            via=principal.via,
                            action="doc_import_skip",
                            target=target_rel,
                            outcome="conflict",
                            detail="exists",
                        )
                    return
                if on_conflict == "overwrite":
                    action = "overwrite"
                    base_version = row["version"] if live else None
                else:
                    final_rel, action = free_variant(target_rel), "rename"

            report["plan"].append(
                {"src": source_rel, "target": final_rel, "action": action, "reason": reason}
            )
            claimed.add(path_norm(final_rel))

            if dry_run:
                report[
                    {
                        "create": "created",
                        "revive": "revived",
                        "overwrite": "overwritten",
                        "rename": "renamed",
                    }[action]
                ] += 1
                if embed:  # predict the post-commit embed the real run would do
                    report["embedded"] += 1
                return

            try:
                if action == "overwrite":
                    self.update(principal, final_rel, base_version, content, embed=embed)
                    report["overwritten"] += 1
                else:  # create / revive / rename all create() at final_rel
                    self.create(principal, final_rel, content, embed=embed)
                    report[
                        "created"
                        if action == "create"
                        else "revived"
                        if action == "revive"
                        else "renamed"
                    ] += 1
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

    def _walk_import_files(
        self, src: Path, include: tuple[str, ...], recurse: bool, warn: Callable[[str], None]
    ) -> Iterator[tuple[Path, str]]:
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
                    d
                    for d in dirnames
                    if d not in IMPORT_EXCLUDED_DIRS
                    and d != ATTACH_DIR
                    and not (base / d).is_symlink()
                )
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
