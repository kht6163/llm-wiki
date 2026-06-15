"""Hybrid search: FTS5 BM25 + sqlite-vec cosine KNN fused with Reciprocal Rank
Fusion (RRF). RRF is rank-based so the incomparable BM25 / distance scales never
need normalizing.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .embedding import Embedder
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

    def to_dict(self) -> dict:
        return asdict(self)


def _fts_match(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: quote each token so user input can't
    inject FTS operators. Tokens are implicitly AND-ed."""
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def _bm25(conn, match: str, limit: int) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT rowid AS doc_id, bm25(documents_fts, 2.0, 1.0) AS rank "
        "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ).fetchall()
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
            f"SELECT id, doc_id, heading, text FROM chunks WHERE id IN ({ph})", ids
        )
    }
    best: dict[int, tuple] = {}  # doc_id -> (distance, heading, text)
    for r in rows:
        ch = chunk_map.get(r["chunk_id"])
        if not ch:
            continue
        d = ch["doc_id"]
        if d not in best or r["distance"] < best[d][0]:
            best[d] = (r["distance"], ch["heading"], ch["text"])
    return sorted(best.items(), key=lambda kv: kv[1][0])


def search(
    db, embedder: Embedder, query: str, *,
    mode: str = "hybrid", top_k: int = 10,
    folder: str | None = None, tags: list[str] | None = None,
) -> list[SearchResult]:
    if mode not in ("hybrid", "bm25", "vector"):
        mode = "hybrid"
    SEARCH_QUERIES.labels(mode).inc()
    top_k = max(1, min(int(top_k), 50))
    k = max(top_k * 4, 40)

    with db.reader() as conn:
        match = _fts_match(query) if mode in ("hybrid", "bm25") else None
        bm_list = _bm25(conn, match, k) if match else []
        vec_list = _vector(conn, embedder, query, k) if mode in ("hybrid", "vector") else []

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

        results: list[SearchResult] = []
        for did, score in scored:
            d = conn.execute(
                "SELECT path, title, version, folder, is_deleted FROM documents WHERE id=?",
                (did,),
            ).fetchone()
            if not d or d["is_deleted"]:
                continue
            if folder:
                f = folder.strip("/")
                if not (d["folder"] == f or d["folder"].startswith(f + "/")):
                    continue
            if tags:
                doctags = {t[0] for t in conn.execute("SELECT tag FROM tags WHERE doc_id=?", (did,))}
                if not set(tags).issubset(doctags):
                    continue

            heading = None
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
                if not snippet:
                    snippet = vec_info[did][2][:240]

            results.append(SearchResult(
                path=d["path"], title=d["title"] or d["path"],
                score=round(score, 6), snippet=snippet, heading=heading, version=d["version"],
            ))
            if len(results) >= top_k:
                break
        return results
