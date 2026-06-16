"""Hybrid search: FTS5 BM25 + sqlite-vec cosine KNN fused with Reciprocal Rank
Fusion (RRF). RRF is rank-based so the incomparable BM25 / distance scales never
need normalizing.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .embedding import Embedder
from .markdown_utils import heading_slug
from .metrics import SEARCH_QUERIES

RRF_K = 60
_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


@dataclass
class SearchResult:
    path: str
    title: str
    score: float
    snippet: str
    heading: str | None
    version: int
    heading_path: str | None = None   # "H1 > H2" breadcrumb of the matched section
    anchor: str | None = None         # heading slug for a #fragment deep-link

    def to_dict(self) -> dict:
        return asdict(self)


def _fts_match(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: quote each token so user input can't
    inject FTS operators. Tokens are implicitly AND-ed."""
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def _bm25(conn, match: str, limit: int, *, folder: str | None = None,
         tags: list[str] | None = None) -> list[tuple[int, float]]:
    # Push folder/tag/is_deleted filtering into the query so the LIMIT picks k docs
    # that ALREADY satisfy the filter. Filtering *after* a fixed top-k (the old
    # behavior) silently dropped a folder's matches whenever they ranked below the
    # global window — zero results on a large corpus exactly when a filter is useful.
    sql = ["SELECT f.rowid AS doc_id, bm25(documents_fts, 2.0, 1.0) AS rank "
           "FROM documents_fts f JOIN documents d ON d.id=f.rowid "
           "WHERE documents_fts MATCH ? AND d.is_deleted=0"]
    params: list = [match]
    if folder:
        fnorm = folder.strip("/")
        sql.append(" AND (d.folder=? OR d.folder LIKE ?)")
        params += [fnorm, fnorm + "/%"]
    for t in tags or []:
        sql.append(" AND d.id IN (SELECT doc_id FROM tags WHERE tag=?)")
        params.append(t)
    sql.append(" ORDER BY rank LIMIT ?")
    params.append(limit)
    rows = conn.execute("".join(sql), params).fetchall()
    return [(r["doc_id"], r["rank"]) for r in rows]


def _vector(conn, embedder: Embedder, query: str, limit: int) -> list[tuple[int, tuple]]:
    qv = embedder.embed_query(query)
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vectors "
        "WHERE embedding MATCH ? AND k=? ORDER BY distance",
        (Embedder.serialize(qv), limit),
    ).fetchall()
    if not rows:
        return []
    # Resolve all matched chunks in one query instead of one SELECT per hit.
    ids = [r["chunk_id"] for r in rows]
    ph = ",".join("?" * len(ids))
    chunk_map = {
        c["id"]: c
        for c in conn.execute(
            f"SELECT id, doc_id, heading, text, heading_path FROM chunks WHERE id IN ({ph})", ids
        )
    }
    best: dict[int, tuple] = {}  # doc_id -> (distance, heading, text, heading_path)
    for r in rows:
        ch = chunk_map.get(r["chunk_id"])
        if not ch:
            continue
        d = ch["doc_id"]
        if d not in best or r["distance"] < best[d][0]:
            best[d] = (r["distance"], ch["heading"], ch["text"], ch["heading_path"])
    return sorted(best.items(), key=lambda kv: kv[1][0])


def _rank(conn, embedder: Embedder, query: str, *, mode: str, k: int,
          folder: str | None, tags: list[str] | None):
    """Core retrieval+RRF fusion shared by ``search_page`` and ``assemble_context``.

    Returns ``(scored, vec_info, match)`` where ``scored`` is ``[(doc_id, rrf_score)]``
    sorted best-first, ``vec_info`` maps doc_id -> ``(distance, heading, text)`` for the
    document's closest vector chunk, and ``match`` is the FTS MATCH expression (or None).
    BM25 pre-filters folder/tags in SQL; the vector leg can't, so callers still re-filter.
    """
    match = _fts_match(query) if mode in ("hybrid", "bm25") else None
    bm_list = _bm25(conn, match, k, folder=folder, tags=tags) if match else []
    # vec0 KNN can't pre-filter by folder/tag, and collapsing chunk hits to docs
    # yields fewer distinct docs than chunks fetched. Over-fetch chunks so the
    # post-dedup/post-filter set still holds ~k distinct docs.
    k_vec = min(k * 3, 600)
    vec_list = _vector(conn, embedder, query, k_vec) if mode in ("hybrid", "vector") else []

    bm_rank = {doc_id: i + 1 for i, (doc_id, _) in enumerate(bm_list)}
    vec_rank = {doc_id: i + 1 for i, (doc_id, _) in enumerate(vec_list)}
    vec_info = {doc_id: info for doc_id, info in vec_list}

    if mode == "bm25":
        ids = [doc_id for doc_id, _ in bm_list]
    elif mode == "vector":
        ids = [doc_id for doc_id, _ in vec_list]
    else:
        ids = list(set(bm_rank) | set(vec_rank))

    scored = []
    for did in ids:
        s = 0.0
        if did in bm_rank:
            s += 1.0 / (RRF_K + bm_rank[did])
        if did in vec_rank:
            s += 1.0 / (RRF_K + vec_rank[did])
        scored.append((did, s))
    scored.sort(key=lambda x: -x[1])
    return scored, vec_info, match


def _passes_filters(d, folder: str | None, tags: list[str] | None, conn) -> bool:
    """Re-apply folder/tag filters to a vector-matched doc (the BM25 leg already
    filtered in SQL; the vec leg returns whatever the corpus held)."""
    if folder:
        f = folder.strip("/")
        if not (d["folder"] == f or d["folder"].startswith(f + "/")):
            return False
    if tags:
        doctags = {t[0] for t in conn.execute("SELECT tag FROM tags WHERE doc_id=?", (d["id"],))}
        if not set(tags).issubset(doctags):
            return False
    return True


def search_page(
    db, embedder: Embedder, query: str, *,
    mode: str = "hybrid", top_k: int = 10,
    folder: str | None = None, tags: list[str] | None = None,
) -> tuple[list[SearchResult], bool]:
    """Run a search and report truncation. Returns ``(results, truncated)`` where
    ``truncated`` is True only when at least one more qualifying document existed
    beyond ``top_k`` — so a corpus of exactly ``top_k`` matches reports False (no
    misleading 'raise top_k' signal). ``results`` is capped at ``top_k``."""
    if mode not in ("hybrid", "bm25", "vector"):
        mode = "hybrid"
    SEARCH_QUERIES.labels(mode).inc()
    top_k = max(1, min(int(top_k), 50))
    want = top_k + 1  # over-collect by one survivor so truncation is exact, not len>=cap
    k = max(top_k * 4, 40)

    with db.reader() as conn:
        scored, vec_info, match = _rank(conn, embedder, query, mode=mode, k=k,
                                        folder=folder, tags=tags)

        results: list[SearchResult] = []
        for did, score in scored:
            d = conn.execute(
                "SELECT id, path, title, version, folder, is_deleted FROM documents WHERE id=?",
                (did,),
            ).fetchone()
            if not d or d["is_deleted"]:
                continue
            if not _passes_filters(d, folder, tags, conn):
                continue

            heading = None
            heading_path = None
            snippet = ""
            if match:
                srow = conn.execute(
                    "SELECT snippet(documents_fts, 1, '<mark>', '</mark>', ' … ', 12) "
                    "FROM documents_fts WHERE rowid=? AND documents_fts MATCH ?",
                    (did, match),
                ).fetchone()
                if srow and srow[0]:
                    snippet = srow[0]
            if did in vec_info:
                heading = vec_info[did][1]
                heading_path = vec_info[did][3]
                if not snippet:
                    snippet = vec_info[did][2][:240]

            results.append(SearchResult(
                path=d["path"], title=d["title"] or d["path"],
                score=round(score, 6), snippet=snippet, heading=heading, version=d["version"],
                heading_path=heading_path,
                anchor=heading_slug(heading) if heading else None,
            ))
            if len(results) >= want:
                break
    return results[:top_k], len(results) > top_k


def search(
    db, embedder: Embedder, query: str, *,
    mode: str = "hybrid", top_k: int = 10,
    folder: str | None = None, tags: list[str] | None = None,
) -> list[SearchResult]:
    """Backward-compatible list API: results only, no truncation flag."""
    return search_page(db, embedder, query, mode=mode, top_k=top_k,
                       folder=folder, tags=tags)[0]


def related_documents(conn, source_doc_id: int, *, k: int = 8,
                      max_src_chunks: int = 12) -> list[dict]:
    """Documents most semantically similar to ``source_doc_id``, via the chunk
    vectors already in the index (no model forward pass — the stored source vectors
    are themselves the KNN queries). For each of the source's leading chunks we run a
    KNN and keep, per other document, its single closest chunk distance; results are
    ranked by that best distance. Returns ``[{path, title, folder, score}]`` where
    ``score`` is cosine similarity (``1 - distance``), best first. Empty when the
    source has no vectors yet (e.g. not embedded)."""
    k = max(1, min(int(k), 50))
    rows = conn.execute(
        "SELECT v.embedding AS emb FROM chunk_vectors v JOIN chunks c ON c.id=v.chunk_id "
        "WHERE c.doc_id=? ORDER BY c.ordinal LIMIT ?",
        (source_doc_id, max(1, int(max_src_chunks))),
    ).fetchall()
    if not rows:
        return []
    # Over-fetch per query: the source's own chunks dominate its neighbors and are
    # dropped, so fetch well beyond k to still surface k distinct OTHER docs.
    k_each = min(k * 4 + 10, 200)
    best: dict[int, float] = {}  # other doc_id -> closest chunk distance
    for r in rows:
        hits = conn.execute(
            "SELECT chunk_id, distance FROM chunk_vectors WHERE embedding MATCH ? AND k=? "
            "ORDER BY distance",
            (r["emb"], k_each),
        ).fetchall()
        if not hits:
            continue
        ids = [h["chunk_id"] for h in hits]
        ph = ",".join("?" * len(ids))
        doc_of = {c["id"]: c["doc_id"] for c in conn.execute(
            f"SELECT id, doc_id FROM chunks WHERE id IN ({ph})", ids)}
        for h in hits:
            did = doc_of.get(h["chunk_id"])
            if did is None or did == source_doc_id:
                continue
            if did not in best or h["distance"] < best[did]:
                best[did] = h["distance"]
    if not best:
        return []
    ordered = sorted(best.items(), key=lambda kv: kv[1])
    out: list[dict] = []
    for did, dist in ordered:
        d = conn.execute(
            "SELECT path, title, folder, is_deleted FROM documents WHERE id=?", (did,)
        ).fetchone()
        if not d or d["is_deleted"]:
            continue
        out.append({"path": d["path"], "title": d["title"] or d["path"],
                    "folder": d["folder"], "score": round(1.0 - dist, 4)})
        if len(out) >= k:
            break
    return out


def assemble_context(
    db, embedder: Embedder, question: str, *,
    max_chars: int = 6000, max_sources: int = 8, mode: str = "hybrid",
    folder: str | None = None, tags: list[str] | None = None,
) -> dict:
    """Retrieve and assemble citation-tagged context for a question — a one-call RAG
    primitive for LLM clients. Ranks documents with the same hybrid retriever as
    search, then for each top document includes its passage most relevant to the
    question (the vector-matched chunk, or the lead chunk as a fallback), in rank
    order, until ``max_chars`` or ``max_sources`` is reached. Returns ``context``
    (the assembled text with ``[n]`` markers), the ``sources`` those markers cite,
    and ``truncated`` (more relevant content existed beyond the budget)."""
    if mode not in ("hybrid", "bm25", "vector"):
        mode = "hybrid"
    max_chars = max(200, min(int(max_chars), 24000))
    max_sources = max(1, min(int(max_sources), 20))
    k = max(max_sources * 4, 40)

    sources: list[dict] = []
    parts: list[str] = []
    total = 0
    truncated = False
    with db.reader() as conn:
        scored, vec_info, _match = _rank(conn, embedder, question, mode=mode, k=k,
                                         folder=folder, tags=tags)
        for did, score in scored:
            if len(sources) >= max_sources:
                truncated = True
                break
            d = conn.execute(
                "SELECT id, path, title, version, folder, is_deleted FROM documents WHERE id=?",
                (did,),
            ).fetchone()
            if not d or d["is_deleted"] or not _passes_filters(d, folder, tags, conn):
                continue

            if did in vec_info:
                heading, text = vec_info[did][1], vec_info[did][2]
            else:  # BM25-only match: no per-chunk vector rank — fall back to the lead chunk
                ch = conn.execute(
                    "SELECT heading, text FROM chunks WHERE doc_id=? ORDER BY ordinal LIMIT 1",
                    (did,),
                ).fetchone()
                heading, text = (ch["heading"], ch["text"]) if ch else (None, "")
            text = (text or "").strip()
            if not text:
                continue

            remaining = max_chars - total
            if remaining <= 0:
                truncated = True
                break
            piece = text if len(text) <= remaining else text[:remaining]
            if len(piece) < len(text):
                truncated = True
            n = len(sources) + 1
            cite = f"[{n}] {d['path']}" + (f" › {heading}" if heading else "")
            parts.append(f"{cite}\n{piece}")
            total += len(piece)
            sources.append({
                "n": n, "path": d["path"], "title": d["title"] or d["path"],
                "heading": heading, "version": d["version"],
                "score": round(score, 6), "chars": len(piece),
            })

    context = "\n\n".join(parts)
    return {"question": question, "context": context, "char_count": len(context),
            "count": len(sources), "truncated": truncated, "sources": sources}
