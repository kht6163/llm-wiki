"""Search, related documents, RAG, and llms.txt export for DocumentService.

Extracted so DocumentService stays a thin coordinator. Public entry points remain
on DocumentService (each delegates here).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import groupby
from typing import TYPE_CHECKING
from urllib.parse import quote

from .. import search
from ..embedding_contract import EmbeddingBindingChanged
from ..markdown_utils import parse_frontmatter
from ..util import clamp_int, normalize_rel_path, path_norm
from .errors import EmbeddingUnavailableError, NotFoundError, ValidationError

if TYPE_CHECKING:
    pass


def search_page(
    svc,
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
    from . import documents as documents_mod
    try:
        return search.search_page(
            svc.db,
            svc.embedder,
            query,
            mode=mode,
            top_k=top_k,
            folder=folder,
            tags=tags,
            since=since,
            until=until,
            params=svc.search_params,
            offset=offset,
            parsed_query=parsed_query,
            candidate_k=candidate_k,
        )
    except EmbeddingBindingChanged as exc:
        raise EmbeddingUnavailableError(documents_mod._EMBEDDING_UNAVAILABLE_MESSAGE) from exc

def search_workbench_page(
    svc,
    query: str,
    *,
    mode: str = "hybrid",
    page: int = 1,
    per_page: int = 20,
    folder: str | None = None,
    tags: list[str] | None = None,
) -> object:
    """Return stable web pagination while preserving the legacy search API."""
    from . import documents as documents_mod
    page = max(1, int(page))
    per_page = clamp_int(per_page, 1, 50)
    mode = mode if mode in ("hybrid", "bm25", "vector") else "hybrid"
    normalized_folder = folder or ""
    normalized_tags = tuple(tag.strip() for tag in tags or () if tag.strip())
    parsed = search.parse_query(query)
    offset = (page - 1) * per_page
    available = max(0, documents_mod.SEARCH_WORKBENCH_MAX_RESULTS - offset)
    if available:
        items, has_next = search_page(svc,
            query,
            mode=mode,
            top_k=min(per_page, available),
            folder=normalized_folder or None,
            tags=list(normalized_tags) or None,
            offset=offset,
            parsed_query=parsed,
            candidate_k=documents_mod.SEARCH_WORKBENCH_MAX_RESULTS,
        )
    else:
        items, has_next = [], False
    frozen_items = tuple(items)
    bounded = offset >= documents_mod.SEARCH_WORKBENCH_MAX_RESULTS or (
        len(frozen_items) == available and available <= per_page
    )
    if bounded:
        has_next = False
    if has_next or bounded or mode != "bm25" or (page > 1 and not frozen_items):
        total_or_more = None
    else:
        total_or_more = offset + len(frozen_items)
    filters = documents_mod.SearchFilters(
        query=query,
        mode=mode,
        folder=normalized_folder,
        tags=normalized_tags,
        normalized=tuple(
            documents_mod.NormalizedSearchFilter(operator, value)
            for operator, value in parsed.filters.normalized
        ),
    )
    return documents_mod.SearchPage(
        items=frozen_items,
        total_or_more=total_or_more,
        page=page,
        per_page=per_page,
        has_prev=page > 1,
        has_next=has_next,
        bounded=bounded,
        filters=filters,
    )

def embedding_enabled(svc) -> bool:
    return bool(getattr(svc.embedder, "enabled", True))

def embedding_status(svc) -> dict:
    """Ops snapshot: whether embeddings are on and how large the backlog is."""
    with svc.db.reader() as conn:
        dirty = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE vector_dirty=1 AND is_deleted=0"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE file_state='pending' AND is_deleted=0"
        ).fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0]
    worker = None
    if svc.embed_worker is not None:
        worker = svc.embed_worker.status()
    return {
        "enabled": embedding_enabled(svc),
        "model": getattr(svc.embedder, "model_name", None),
        "model_loaded": bool(getattr(svc.embedder, "is_loaded", False)),
        "documents": int(docs),
        "vector_dirty": int(dirty),
        "pending_projection": int(pending),
        "embed_worker": worker,
    }

def related(svc, path: str, limit: int = 8) -> dict:
    """Documents semantically similar to this one (via the shared chunk-vector
    index). Empty list when the document has no embeddings yet."""
    from . import documents as documents_mod
    if not embedding_enabled(svc):
        rel = normalize_rel_path(path)
        if not svc.exists(rel):
            raise NotFoundError("No document at this path.", path=rel)
        return {"path": rel, "related": [], "embedding_enabled": False}
    rel = normalize_rel_path(path)
    norm = path_norm(rel)
    try:
        expected = svc.db.expected_embedding_binding()
        with svc.db.embedding_read_snapshot(expected) as conn:
            d = conn.execute(
                "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0",
                (norm,),
            ).fetchone()
            if not d:
                raise NotFoundError("No document at this path.", path=rel)
            items = search._related_documents(conn, d["id"], k=limit)
    except EmbeddingBindingChanged as exc:
        raise EmbeddingUnavailableError(documents_mod._EMBEDDING_UNAVAILABLE_MESSAGE) from exc
    return {"path": rel, "related": items}

def assemble_context(
    svc,
    question: str,
    *,
    max_chars: int = 6000,
    max_sources: int = 8,
    mode: str = "hybrid",
    folder: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Retrieve + assemble citation-tagged context for a question (RAG primitive)."""
    from . import documents as documents_mod
    if not question or not question.strip():
        raise ValidationError("question must not be empty.")
    try:
        return search.assemble_context(
            svc.db,
            svc.embedder,
            question,
            max_chars=max_chars,
            max_sources=max_sources,
            mode=mode,
            folder=folder,
            tags=tags,
            params=svc.search_params,
        )
    except EmbeddingBindingChanged as exc:
        raise EmbeddingUnavailableError(documents_mod._EMBEDDING_UNAVAILABLE_MESSAGE) from exc

@contextmanager
def _corpus_read_snapshot(svc) -> Iterator[sqlite3.Connection]:
    """Keep one read snapshot for an export without owning caller transactions."""
    with svc.db.reader() as conn:
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
    svc,
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
        tags_by = svc._tags_for_ids(conn, [r["id"] for r in rows])
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
    svc, folder: str | None = None, conn: sqlite3.Connection | None = None
) -> int:
    if conn is None:
        with svc.db.reader() as read_conn:
            return _corpus_count(svc, folder, conn=read_conn)
    q = "SELECT COUNT(*) FROM documents d WHERE d.is_deleted=0"
    params: list = []
    if folder:
        f = folder.strip("/")
        q += " AND (d.folder=? OR d.folder LIKE ?)"
        params += [f, f + "/%"]
    return int(conn.execute(q, params).fetchone()[0])

def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())

def _md_label(value: object) -> str:
    return _one_line(value).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

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

def _corpus_body_prefix(body: str, body_chars: int) -> tuple[str, bool]:
    """Strip complete frontmatter without exposing a prefix cut inside YAML."""
    prefix_truncated = len(body) < body_chars
    _meta, offset = parse_frontmatter(body)
    if offset:
        return body[offset:], prefix_truncated
    if prefix_truncated and re.match(r"^---[ \t]*\n", body):
        return "", True
    return body, prefix_truncated

def _doc_raw_url(svc, path: str, base_url: str = "") -> str:
    enc = quote(path)
    return f"{base_url.rstrip('/')}/doc/{enc}/raw" if base_url else f"/doc/{enc}/raw"

def llms_index(svc, *, site_title: str, base_url: str = "") -> str:
    """Render the vault as an ``llms.txt`` index (the emerging agent-facing site
    map, https://llmstxt.org/): an H1 title, a one-line blockquote summary, then
    an H2 section per folder listing each document as a markdown link to its raw
    (.md) source plus a short description — so any LLM, not just an MCP client,
    can discover what the knowledge base holds."""
    from . import documents as documents_mod
    with _corpus_read_snapshot(svc) as conn:
        total = _corpus_count(svc, conn=conn)
        docs = _iter_corpus_docs(svc,
            conn=conn, body_max_chars=documents_mod._CORPUS_DESCRIPTION_PREFIX_CHARS
        )
        lines = [
            f"# {_md_label(site_title)}",
            "",
            f"> 마크다운 지식베이스 — 문서 {total}개. "
            "각 항목은 원문(.md) 링크이며, 전체 본문은 /llms-full.txt 로 한 번에 가져올 수 있습니다.",
            "",
        ]
        for folder, group in groupby(docs, key=lambda d: d["folder"]):
            lines.append(f"## {_md_label(folder or '루트')}")
            for d in group:
                desc = _one_line(_doc_description(d["body"], body_chars=d["body_chars"])
                )
                url = _doc_raw_url(svc, d["path"], base_url)
                title = _md_label(d["title"])
                lines.append(f"- [{title}]({url})" + (f": {desc}" if desc else ""))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

def llms_full(svc, *, site_title: str, max_chars: int = 2_000_000) -> dict:
    """Render the whole vault as one concatenated markdown document
    (``llms-full.txt``): each document's full body, prefixed by a path/tags/updated
    header and separated by a horizontal rule, so an agent can ingest the entire
    corpus in a single request. Emission stops once ``max_chars`` of content is
    reached (``truncated=True``), bounding the response for very large vaults."""
    limit = max(0, int(max_chars))
    with _corpus_read_snapshot(svc) as conn:
        total = _corpus_count(svc, conn=conn)
        parts = [f"# {_md_label(site_title)}\n\n> 전체 코퍼스 export — 문서 {total}개.\n"]
        included = 0
        truncated = False

        def marker(count: int) -> str:
            return (
                f"\n---\n\n> [truncated] {count}/{total} 문서만 포함되었습니다. "
                "나머지는 /llms.txt 색인이나 개별 문서로 가져오세요.\n"
            )

        size = len(parts[0])
        if size <= limit:
            for d in _iter_corpus_docs(svc, conn=conn, body_max_chars=limit - size):
                body, body_prefix_truncated = _corpus_body_prefix(d["body"], d["body_chars"]
                )
                body = body.strip()
                header = (
                    f"---\n\n# {_md_label(d['title'])}\n\n"
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
