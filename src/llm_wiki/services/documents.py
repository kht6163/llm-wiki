"""Document service: the optimistic-concurrency write pipeline, revisions, search
index maintenance, link graph, and external-edit reconciliation.

Canonicity: the DB owns version/identity/metadata and the latest revision body is
the durable source of truth for content; the .md file is an atomically-written
projection of it. On a crash between commit and file write, the file is re-projected
from the latest revision (see ``recover_pending``).
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlencode

from .. import file_projection as fp
from .. import indexing, search
from ..db import Database
from ..embedding import Embedder
from ..markdown_utils import (
    extract_tags,
    heading_slug,
)
from ..merge import three_way_merge
from ..util import (
    PathError,
    clamp_int,
    normalize_rel_path,
    path_norm,
)
from . import doc_edit as _doc_edit
from .doc_projection import (  # noqa: F401 — re-exported for stable import paths
    CleanupIssue,
    ProjectionPendingError,
    ProjectionSnapshot,
    PurgeIntentSnapshot,
    RecoveryReport,
    ReindexTargetSnapshot,
)
from . import doc_import as _doc_import
from .auth import ROLE_RANK, Principal
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

log = logging.getLogger("llm_wiki.documents")

_EMBEDDING_UNAVAILABLE_MESSAGE = (
    "Embedding search is temporarily unavailable because this service is using "
    "an outdated embedding generation. Restart the service and retry."
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CORPUS_DESCRIPTION_PREFIX_CHARS = 16 * 1024
SEARCH_WORKBENCH_MAX_RESULTS = 600

# Uploaded images/files live under this vault subdir (excluded from the .md scan).
ATTACH_DIR = "_attachments"
ATTACH_MAX_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}

# Document templates live as plain .md files under this vault subdir. They are
# listable via list_templates() but not indexed as normal wiki documents.
TEMPLATES_DIR = "_templates"
TEMPLATE_PREVIEW_CHARS = 200

# Re-exported import constants (defined in doc_import; tests monkeypatch these names).
IMPORT_ATTACH_EXTS = _doc_import.IMPORT_ATTACH_EXTS
IMPORT_DEFAULT_INCLUDE = _doc_import.IMPORT_DEFAULT_INCLUDE
IMPORT_EXCLUDED_DIRS = _doc_import.IMPORT_EXCLUDED_DIRS
IMPORT_MAX_BYTES = _doc_import.IMPORT_MAX_BYTES
IMPORT_MD_EXTS = _doc_import.IMPORT_MD_EXTS
_IMPORT_RENAME_MAX_SUFFIX = _doc_import._IMPORT_RENAME_MAX_SUFFIX

# Re-exported edit constants (defined in doc_edit; tests may monkeypatch these).
REGEX_TIMEOUT_S = _doc_edit.REGEX_TIMEOUT_S
MAX_PATCH_MATCHES = _doc_edit.MAX_PATCH_MATCHES

# How long an idempotency key stays replayable. Retries happen within seconds, so a
# week is generous; older rows are swept opportunistically on each keyed write to
# bound the ledger's growth.
_IDEM_RETENTION_DAYS = 7
_IDEM_KEY_MAX_CHARS = 200














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




def _attachment_subname(name: str, ext: str, data: bytes) -> str:
    """Content-addressed ``<stem>-<sha8><ext>`` filename for a stored attachment.
    Shared by interactive uploads and the bulk importer so both name files the same
    way (and so an importer dry-run can predict the exact target without writing)."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name[: len(name) - len(ext)]).strip("-_.") or "file"
    digest = hashlib.sha256(data).hexdigest()[:8]
    return f"{stem}-{digest}{ext}"












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
        if not self.embedding_enabled():
            return
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












    # ---- projection lifecycle (delegated to doc_projection) ----
    def _projection_snapshot(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._projection_snapshot(self, *args, **kwargs)

    def _projection_token_state(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._projection_token_state(self, *args, **kwargs)

    def _install_projection_target(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._install_projection_target(self, *args, **kwargs)

    def _mark_projection_clean(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._mark_projection_clean(self, *args, **kwargs)

    @staticmethod
    def _expected_cleanup_signature(row):
        from . import doc_projection
        return doc_projection._expected_cleanup_signature(row)

    def _process_cleanup_batch(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._process_cleanup_batch(self, *args, **kwargs)

    def _purge_intent_snapshot(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._purge_intent_snapshot(self, *args, **kwargs)

    def _process_purge_cleanup_batch(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._process_purge_cleanup_batch(self, *args, **kwargs)

    def _finish_purge(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._finish_purge(self, *args, **kwargs)

    def _project_current(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._project_current(self, *args, **kwargs)

    def _require_projection(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._require_projection(self, *args, **kwargs)

    def _recover_pending_report(self, *args, **kwargs):
        from . import doc_projection
        return doc_projection._recover_pending_report(self, *args, **kwargs)

    def _fence_principal(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        *,
        require_write: bool = False,
        require_admin: bool = False,
    ) -> None:
        """Re-authorize a previously resolved identity inside the writer transaction.

        Authentication and the eventual write are separated by request scheduling.  A
        password change, deactivation, role downgrade, or API-key revocation in that
        window must win over the stale in-memory ``Principal``.
        """
        # The local import command deliberately uses an unattributed, trusted CLI
        # principal: its audit actor is the OS user and there is no wiki-user row to
        # re-read.  Preserve that explicit host-only path while keeping every web/MCP
        # write fenced against the current credential state.
        if principal.via == "cli" and principal.user_id is None:
            if require_admin and principal.role != "admin":
                raise ForbiddenError("Current CLI role is not admin.")
            if require_write and not principal.can_write:
                raise ForbiddenError("Current CLI role is read-only.")
            return
        row = conn.execute(
            "SELECT username, role, is_active, credential_version FROM users WHERE id=?",
            (principal.user_id,),
        ).fetchone()
        if (
            row is None
            or not row["is_active"]
            or int(row["credential_version"]) != int(principal.credential_version)
        ):
            raise ForbiddenError("Credentials changed or the account is no longer active.")
        role = str(row["role"])
        if require_admin and role != "admin":
            raise ForbiddenError("Current account role is not admin.")
        if require_write and ROLE_RANK.get(role, 0) < ROLE_RANK["editor"]:
            raise ForbiddenError("Current account role is read-only.")
        if principal.via == "mcp":
            if principal.api_key_id is None:
                raise ForbiddenError("The API key identity is no longer valid.")
            key = conn.execute(
                "SELECT scope FROM api_keys WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (principal.api_key_id, principal.user_id),
            ).fetchone()
            if key is None:
                raise ForbiddenError("The API key was revoked before the write committed.")
            if require_write and str(key["scope"]) != "readwrite":
                raise ForbiddenError("The API key is read-only.")


    # ---- idempotency ----------------------------------------------------
    def _idem_lookup(
        self, scope: str, user_id: int, key: str, request_hash: str
    ) -> dict | None:
        """Return the cached result of a previously-applied write with this
        (scope, user, key), or None if the key is new. Lets a client safely retry
        a write whose response was lost without applying it twice."""
        with self.db.reader() as conn:
            row = conn.execute(
                "SELECT result_path, result_version, request_hash FROM idempotency_keys "
                "WHERE scope=? AND user_id=? AND idem_key=?",
                (scope, user_id, key),
            ).fetchone()
        if row is None:
            return None
        if not row["request_hash"] or not hmac.compare_digest(
            str(row["request_hash"]), request_hash
        ):
            raise ConflictError(
                "The idempotency key was already used for a different request.",
                idempotency_key_reused=True,
            )
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
        with self.db.read_snapshot() as conn:
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
        self, principal: Principal, path: str, base_version: int, mine: str, mine_title: str
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
        with self.db.read_snapshot() as conn:
            row = conn.execute(
                "SELECT d.id,d.version,d.title AS current_title,d.is_deleted,d.updated_at,"
                "u.username AS updated_by,current.body AS current_body,"
                "current.via AS current_via,current.title AS revision_current_title,"
                "base.body AS base_body,base.title AS base_title "
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
            current_title = row["revision_current_title"]
            if current_title != row["current_title"]:
                raise RuntimeError("current document title revision is missing or corrupt")
            base_title = row["base_title"]

        title_conflict = False
        merged_title = None
        if base is None:
            if mine_title == current_title:
                merged_title = mine_title
            else:
                title_conflict = True
        elif mine_title == base_title:
            merged_title = current_title
        elif current_title == base_title or mine_title == current_title:
            merged_title = mine_title
        else:
            title_conflict = True

        preview = {
            "base_version": requested_version,
            "current_version": current_version,
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "current_via": row["current_via"],
            "base": base,
            "base_title": base_title,
            "mine": mine,
            "mine_title": mine_title,
            "current": current,
            "current_title": current_title,
            "merged_title": merged_title,
            "title_conflict": title_conflict,
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
                "merged_start": len(result.text[: cast(int, hunk.merged_start)].encode("utf-16-le"))
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
        with self.db.read_snapshot() as conn:
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
        with self.db.read_snapshot() as conn:
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


    @staticmethod
    def _tag_filter(tag=None, tags=None) -> list[str]:
        """Normalize the single ``tag`` and the multi ``tags`` arguments into one
        de-duplicated, order-stable list of tags that must ALL be present (AND)."""
        out: list[str] = []
        for t in ([tag] if tag else []) + list(tags or []):
            if t and t not in out:
                out.append(t)
        return out











    # ---- nav / folders / favorites / templates (delegated to doc_nav) ----
    def folders(self) -> list[str]:
        from . import doc_nav
        return doc_nav.folders(self)

    def folder_counts(self) -> list[tuple[str, int]]:
        from . import doc_nav
        return doc_nav.folder_counts(self)

    def list_folders(self) -> list[str]:
        from . import doc_nav
        return doc_nav.list_folders(self)

    def tree(self) -> dict:
        from . import doc_nav
        return doc_nav.tree(self)

    def create_folder(self, principal: Principal, path: str) -> dict:
        from . import doc_nav
        return doc_nav.create_folder(self, principal, path)

    def delete_folder(self, principal: Principal, path: str) -> dict:
        from . import doc_nav
        return doc_nav.delete_folder(self, principal, path)

    def tags(self) -> list[dict]:
        from . import doc_nav
        return doc_nav.tags(self)

    def nav_tree(self) -> dict:
        from . import doc_nav
        return doc_nav.nav_tree(self)

    def nav_tags(self) -> list[dict]:
        from . import doc_nav
        return doc_nav.nav_tags(self)

    def list_templates(self) -> list[dict]:
        from . import doc_nav
        return doc_nav.list_templates(self)

    def list_docs(
        self, folder=None, tag=None, limit=100, offset=0, sort="updated_at", tags=None
    ) -> list[dict]:
        from . import doc_nav
        return doc_nav.list_docs(self, folder, tag, limit, offset, sort, tags)

    def count(self, folder=None, tag=None, tags=None) -> int:
        from . import doc_nav
        return doc_nav.count(self, folder, tag, tags)

    def complete(self, q: str, limit: int = 10) -> list[dict]:
        from . import doc_nav
        return doc_nav.complete(self, q, limit)

    def preview(self, path: str, max_chars: int = 240) -> dict:
        from . import doc_nav
        return doc_nav.preview(self, path, max_chars)

    def toggle_favorite(self, principal: Principal, path: str) -> dict:
        from . import doc_nav
        return doc_nav.toggle_favorite(self, principal, path)

    def set_favorite(self, principal: Principal, path: str, favorite: bool) -> dict:
        from . import doc_nav
        return doc_nav.set_favorite(self, principal, path, favorite)

    def is_favorite(self, user_id: int, path: str) -> bool:
        from . import doc_nav
        return doc_nav.is_favorite(self, user_id, path)

    def list_favorites(self, user_id: int) -> list[dict]:
        from . import doc_nav
        return doc_nav.list_favorites(self, user_id)

    def _resolve_template_path(self, name: str):
        from . import doc_nav
        return doc_nav._resolve_template_path(self, name)

    def _load_template_body(self, name: str) -> str:
        from . import doc_nav
        return doc_nav._load_template_body(self, name)

    def attachment_file(self, subpath: str) -> Path:
        """Resolve an uploaded attachment to a real file under the vault, safely.
        Raises PathError for traversal, NotFoundError if missing."""
        try:
            target, _data = fp.read_confined_bytes(
                self.vault, f"{ATTACH_DIR}/{subpath}", max_bytes=ATTACH_MAX_BYTES
            )
        except (FileNotFoundError, fp.ProjectionPathMissing):
            raise NotFoundError("No such attachment.", path=subpath) from None
        except fp.FileProjectionError as exc:
            raise PathError("unsafe attachment path") from exc
        return target

    def attachment_bytes(self, subpath: str) -> tuple[Path, bytes]:
        """Return a stable, confined attachment generation for an HTTP response."""
        try:
            return fp.read_confined_bytes(
                self.vault, f"{ATTACH_DIR}/{subpath}", max_bytes=ATTACH_MAX_BYTES
            )
        except (FileNotFoundError, fp.ProjectionPathMissing):
            raise NotFoundError("No such attachment.", path=subpath) from None
        except fp.FileProjectionError as exc:
            raise PathError("unsafe attachment path") from exc


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










    # ---- llms.txt corpus export (agent-facing site map / full ingest) ----











    # ---- templates ------------------------------------------------------



    # ---- writes ---------------------------------------------------------


    # ---- targeted edits (token-cheap; funnel through the CAS update path) ----











    # ``title``/``tags`` are surfaced and edited through dedicated paths (the heading and
    # the tag list), so the generic property editor leaves them alone to avoid two ways
    # to write the same field.







    # ---- targeted edits (delegated to doc_edit) ----
    def get_section(self, path: str, heading: str, occurrence: int = 1) -> dict:
        from . import doc_edit
        return doc_edit.get_section(self, path, heading, occurrence)

    def outline(self, path: str) -> dict:
        from . import doc_edit
        return doc_edit.outline(self, path)

    def replace_section(
        self,
        principal: Principal,
        path: str,
        heading: str,
        text: str,
        base_version: int | None = None,
        occurrence: int = 1,
    ) -> dict:
        from . import doc_edit
        return doc_edit.replace_section(
            self, principal, path, heading, text, base_version, occurrence
        )

    def append_section(
        self,
        principal: Principal,
        path: str,
        heading: str,
        text: str,
        base_version: int | None = None,
        occurrence: int = 1,
    ) -> dict:
        from . import doc_edit
        return doc_edit.append_section(
            self, principal, path, heading, text, base_version, occurrence
        )

    def append_to_document(
        self,
        principal: Principal,
        path: str,
        text: str,
        ensure_heading: str | None = None,
        base_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        from . import doc_edit
        return doc_edit.append_to_document(
            self, principal, path, text, ensure_heading, base_version, idempotency_key
        )

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
        from . import doc_edit
        return doc_edit.patch(
            self, principal, path, find, replace, base_version, count, mode, occurrence
        )

    def restore_revision(
        self, principal: Principal, path: str, version: int, base_version: int | None = None
    ) -> dict:
        from . import doc_edit
        return doc_edit.restore_revision(self, principal, path, version, base_version)

    def toggle_task(
        self,
        principal: Principal,
        path: str,
        line: int | None = None,
        *,
        index: int | None = None,
        base_version: int | None = None,
    ) -> dict:
        from . import doc_edit
        return doc_edit.toggle_task(
            self, principal, path, line, index=index, base_version=base_version
        )

    def patch_tags(
        self,
        principal: Principal,
        path: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict:
        from . import doc_edit
        return doc_edit.patch_tags(self, principal, path, add, remove)

    def merge_tags(self, principal: Principal, sources: list[str], dest: str) -> dict:
        from . import doc_edit
        return doc_edit.merge_tags(self, principal, sources, dest)

    def rename_tag(self, principal: Principal, old: str, new: str) -> dict:
        from . import doc_edit
        return doc_edit.rename_tag(self, principal, old, new)

    def set_property(
        self,
        principal: Principal,
        path: str,
        key: str,
        value: str | list[str],
        base_version: int | None = None,
    ) -> dict:
        from . import doc_edit
        return doc_edit.set_property(self, principal, path, key, value, base_version)

    def remove_property(
        self, principal: Principal, path: str, key: str, base_version: int | None = None
    ) -> dict:
        from . import doc_edit
        return doc_edit.remove_property(self, principal, path, key, base_version)

    def replace_properties(
        self,
        principal: Principal,
        path: str,
        props: list[tuple[str, list[str]]],
        base_version: int | None = None,
    ) -> dict:
        from . import doc_edit
        return doc_edit.replace_properties(self, principal, path, props, base_version)




    # ---- links / graph (delegated to doc_links) ----
    def backlinks(self, path: str, *, with_context: bool = False) -> dict:
        from . import doc_links
        return doc_links.backlinks(self, path, with_context=with_context)

    def links(self, path: str) -> dict:
        from . import doc_links
        return doc_links.links(self, path)

    def graph(
        self,
        root=None,
        depth=1,
        limit=500,
        include_unresolved=True,
        folder=None,
        tag=None,
        tags=None,
    ) -> dict:
        from . import doc_links
        return doc_links.graph(
            self,
            root=root,
            depth=depth,
            limit=limit,
            include_unresolved=include_unresolved,
            folder=folder,
            tag=tag,
            tags=tags,
        )

    def broken_links(self, limit: int = 200) -> dict:
        from . import doc_links
        return doc_links.broken_links(self, limit)


    # ---- search / related / llms (delegated to doc_search) ----
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
        parsed_query=None,
        candidate_k: int | None = None,
    ):
        from . import doc_search
        return doc_search.search_page(
            self, query, mode=mode, top_k=top_k, folder=folder, tags=tags,
            since=since, until=until, offset=offset, parsed_query=parsed_query,
            candidate_k=candidate_k,
        )

    def search_workbench_page(self, *args, **kwargs):
        from . import doc_search
        return doc_search.search_workbench_page(self, *args, **kwargs)

    def embedding_enabled(self) -> bool:
        from . import doc_search
        return doc_search.embedding_enabled(self)

    def embedding_status(self) -> dict:
        from . import doc_search
        return doc_search.embedding_status(self)

    def related(self, path: str, limit: int = 8) -> dict:
        from . import doc_search
        return doc_search.related(self, path, limit)

    def assemble_context(self, *args, **kwargs):
        from . import doc_search
        return doc_search.assemble_context(self, *args, **kwargs)

    def llms_index(self, *, site_title: str, base_url: str = "") -> str:
        from . import doc_search
        return doc_search.llms_index(self, site_title=site_title, base_url=base_url)

    def llms_full(self, *, site_title: str, max_chars: int = 2_000_000) -> dict:
        from . import doc_search
        return doc_search.llms_full(self, site_title=site_title, max_chars=max_chars)

    # Private corpus helpers remain importable on the service for tests / reindex.
    def _corpus_read_snapshot(self):
        from . import doc_search
        return doc_search._corpus_read_snapshot(self)

    def _iter_corpus_docs(self, *args, **kwargs):
        from . import doc_search
        return doc_search._iter_corpus_docs(self, *args, **kwargs)

    def _corpus_count(self, *args, **kwargs):
        from . import doc_search
        return doc_search._corpus_count(self, *args, **kwargs)

    def _doc_description(self, *args, **kwargs):
        from . import doc_search
        return doc_search._doc_description(*args, **kwargs)

    def _corpus_body_prefix(self, *args, **kwargs):
        from . import doc_search
        return doc_search._corpus_body_prefix(*args, **kwargs)

    def resolve_link(self, target: str, from_path: str | None = None) -> str | None:
        from . import doc_links
        return doc_links.resolve_link(self, target, from_path)

    def move_preview(self, path: str, new_path: str) -> dict:
        from . import doc_links
        return doc_links.move_preview(self, path, new_path)

    def rename_references(self, principal: Principal, old_path: str, new_path: str) -> dict:
        from . import doc_links
        return doc_links.rename_references(self, principal, old_path, new_path)



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






    # ---- favorites (per-user pins) --------------------------------------




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
        with self.db.writer() as conn:
            self._fence_principal(conn, principal, require_write=True)
            try:
                fp.write_confined_bytes(self.vault, f"{ATTACH_DIR}/{sub}", data)
            except fp.FileProjectionError as exc:
                raise PathError("unsafe attachment storage path") from exc
        url = "/attachments/" + quote(sub)
        alt = name[: len(name) - len(ext)] or "file"
        return {"path": f"{ATTACH_DIR}/{sub}", "url": url, "markdown": f"![{alt}]({url})"}

    # ---- maintenance ----------------------------------------------------

    # ---- lifecycle CRUD (delegated to doc_crud) ----
    def create(
        self,
        principal: Principal,
        path: str,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        *,
        embed: bool = True,
        template: str | None = None,
    ) -> dict:
        from . import doc_crud
        return doc_crud.create(
            self, principal, path, content, title, tags, embed=embed, template=template
        )

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
        idempotency: tuple[str, int, str, str] | None = None,
    ) -> dict:
        from . import doc_crud
        return doc_crud.update(
            self,
            principal,
            path,
            base_version,
            content,
            title,
            tags,
            embed=embed,
            idempotency=idempotency,
        )

    def move(
        self, principal: Principal, path: str, new_path: str, fix_references: bool = False
    ) -> dict:
        from . import doc_crud
        return doc_crud.move(self, principal, path, new_path, fix_references)

    def daily_note(
        self, principal: Principal, date: str | None = None, *, folder: str = "daily"
    ) -> dict:
        from . import doc_crud
        return doc_crud.daily_note(self, principal, date, folder=folder)

    def delete(self, principal: Principal, path: str, base_version: int | None = None) -> dict:
        from . import doc_crud
        return doc_crud.delete(self, principal, path, base_version)

    def list_deleted(self, limit: int = 100, offset: int = 0) -> list[dict]:
        from . import doc_crud
        return doc_crud.list_deleted(self, limit, offset)

    def restore(self, principal: Principal, path: str) -> dict:
        from . import doc_crud
        return doc_crud.restore(self, principal, path)

    def purge(self, principal: Principal, path: str) -> dict:
        from . import doc_crud
        return doc_crud.purge(self, principal, path)

    def recover_pending(self) -> int:
        from . import doc_reindex

        return doc_reindex.recover_pending(self)

    def embed_pending(self) -> int:
        from . import doc_reindex

        return doc_reindex.embed_pending(self)

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

    def reindex_all(
        self, reembed: bool = False, progress: Callable[[int, int], None] | None = None
    ) -> dict:
        from . import doc_reindex

        return doc_reindex.reindex_all(self, reembed=reembed, progress=progress)

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
        from . import doc_import

        return doc_import.import_from_directory(
            self,
            principal,
            source_dir,
            into,
            on_conflict=on_conflict,
            include=include,
            recurse=recurse,
            import_attachments=import_attachments,
            embed=embed,
            dry_run=dry_run,
        )
