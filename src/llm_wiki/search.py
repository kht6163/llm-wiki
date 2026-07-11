"""Hybrid search: FTS5 BM25 + sqlite-vec cosine KNN fused with Reciprocal Rank
Fusion (RRF). RRF is rank-based so the incomparable BM25 / distance scales never
need normalizing.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass

from .embedding import Embedder
from .embedding_contract import (
    EMBEDDING_FLOAT32_MAX,
    EmbeddingBinding,
    EmbeddingBindingChanged,
)
from .markdown_utils import heading_slug
from .metrics import SEARCH_LATENCY, SEARCH_QUERIES
from .util import clamp_int

_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


@dataclass(frozen=True)
class FusionParams:
    """Tunable knobs for hybrid retrieval + RRF fusion. The over-fetch/rrf defaults match
    the values that were hardcoded before they were promoted to config (see Settings.rrf_k
    etc.). The ``*_boost``/``proximity_weight`` knobs add a light rerank layer on top of the
    rank-only RRF (see ``_rerank_boost``); they're expressed in RRF-score units (≈1/rrf_k)."""
    rrf_k: int = 60            # RRF constant: larger flattens per-leg rank influence
    candidate_factor: int = 4  # BM25/candidate over-fetch: k = max(top_k*factor, min)
    candidate_min: int = 40
    vector_factor: int = 3     # vector over-fetch: k_vec = min(k*factor, cap)
    vector_cap: int = 600
    title_exact_boost: float = 0.05    # added when the query equals the document title
    title_prefix_boost: float = 0.015  # added when the title starts with the query
    proximity_weight: float = 0.0      # added: weight * cosine-sim of matched chunk (0 = off)


DEFAULT_FUSION = FusionParams()


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
    chunk_ordinal: int | None = None  # ordinal of the matched chunk (None for BM25-only)
    chunk_id: int | None = None       # stable chunk id; feed to read_chunk for full passage
    folder: str = ""                  # the document's folder, so callers can group/filter
    tags: list[str] | None = None     # the document's tags, to avoid a follow-up read
    updated_at: str | None = None     # last-modified timestamp, for recency sort/filter
    backlinks_count: int | None = None   # how many docs link TO this hit (popularity signal)
    outlinks_count: int | None = None    # how many links this hit points OUT to
    content_length: int | None = None    # char length of the doc's latest body (triage short vs long)
    section_depth: int | None = None     # nesting depth of the matched heading (1=top-level; None if none)
    char_start: int | None = None        # matched chunk's start offset in the (frontmatter-stripped) body; None if the hit has no chunk
    char_end: int | None = None          # matched chunk's end offset; pair with char_start for the range
    context_preview: str | None = None   # leading plain-text lines of the matched chunk (no <mark>); None if no chunk

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QueryFilters:
    """In-query operators parsed out of a search string by ``parse_query_filters``.
    ``title_contains`` each must be a (case-insensitive) substring of the title;
    ``path_specs`` each is ``(pattern, is_glob)`` matched against the path (glob when it
    holds ``*``/``?``, else a substring); ``has`` are structural predicates
    (link/backlink/tag). Empty everywhere = no refinement (the default)."""
    title_contains: tuple[str, ...] = ()
    path_specs: tuple[tuple[str, bool], ...] = ()
    has: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.title_contains or self.path_specs or self.has)


_NO_FILTERS = QueryFilters()
_HAS_VALUES = ("link", "backlink", "tag")
# title:/path:/has: operator + its value (a "quoted phrase" or a single bareword).
_OP_RE = re.compile(r'\b(title|path|has):("(?:[^"\\]|\\.)*"|\S+)')


def parse_query_filters(query: str) -> tuple[str, QueryFilters]:
    """Split a raw search string into ``(free_text, QueryFilters)`` by lifting out the
    ``title:`` / ``path:`` / ``has:`` operators so an agent can express a precise query
    in ONE call instead of post-filtering a broad result set. ``title:`` and ``path:``
    take a value (quote it for spaces: ``title:"design system"``); a ``path:`` value with
    ``*``/``?`` is a glob, otherwise a substring. ``has:`` takes one of link|backlink|tag.
    Raises ``ValidationError`` on an unknown ``has:`` value so the vocabulary is learnable.
    The returned free text is what feeds FTS/vector retrieval (operators removed)."""
    from .services.errors import ValidationError  # local import: avoid services<->search cycle

    titles: list[str] = []
    paths: list[tuple[str, bool]] = []
    has: list[str] = []

    def _take(m: re.Match) -> str:
        key, raw = m.group(1), m.group(2)
        val = (raw[1:-1].replace('\\"', '"') if raw.startswith('"') else raw).strip()
        if not val:
            return ""
        if key == "title":
            titles.append(val)
        elif key == "path":
            paths.append((val, any(c in val for c in "*?")))
        else:  # has:
            v = val.lower()
            if v not in _HAS_VALUES:
                raise ValidationError(
                    f"has: must be one of {list(_HAS_VALUES)} (got {val!r}).")
            has.append(v)
        return " "

    cleaned = " ".join(_OP_RE.sub(_take, query or "").split())
    return cleaned, QueryFilters(tuple(titles), tuple(paths), tuple(has))


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards in a literal so it matches verbatim (with ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _glob_to_like(s: str) -> str:
    """Translate a user glob to a LIKE pattern: literal LIKE-specials are escaped first,
    then ``*`` -> ``%`` and ``?`` -> ``_`` (anchored, since the user used wildcards)."""
    return _like_escape(s).replace("*", "%").replace("?", "_")


def _filter_sql(f: QueryFilters) -> tuple[str, list]:
    """SQL WHERE fragment (+params) for the in-query operators, referencing the ``d``
    (documents) alias. The SINGLE source of truth for the predicate: used both to
    pre-filter the BM25 candidate window (recall — so the LIMIT picks docs that already
    satisfy the operators) and to compute the authoritative allowed-id set that
    re-filters the vector leg, so the two legs can't diverge."""
    sql: list[str] = []
    params: list = []
    for term in f.title_contains:
        sql.append(" AND LOWER(COALESCE(d.title,'')) LIKE ? ESCAPE '\\'")
        params.append("%" + _like_escape(term.lower()) + "%")
    for pat, is_glob in f.path_specs:
        sql.append(" AND LOWER(d.path) LIKE ? ESCAPE '\\'")
        params.append(_glob_to_like(pat.lower()) if is_glob
                      else "%" + _like_escape(pat.lower()) + "%")
    for h in f.has:
        if h == "link":
            sql.append(" AND d.id IN (SELECT src_doc_id FROM links)")
        elif h == "backlink":
            sql.append(" AND d.id IN (SELECT dst_doc_id FROM links WHERE is_resolved=1)")
        elif h == "tag":
            sql.append(" AND d.id IN (SELECT doc_id FROM tags)")
    return "".join(sql), params


def _filtered_ids(conn, ids: list[int], f: QueryFilters) -> set[int] | None:
    """The subset of ``ids`` satisfying the in-query operators, in one batched query
    (None when no operators are active — the caller then skips the membership check).
    Re-applies the same ``_filter_sql`` predicate to BOTH legs' candidates so the vector
    leg (which can't pre-filter in SQL) is held to the operators just like BM25."""
    if not ids or not f.active:
        return None
    frag, fparams = _filter_sql(f)
    out: set[int] = set()
    for i in range(0, len(ids), 400):
        batch = ids[i:i + 400]
        ph = ",".join("?" * len(batch))
        for r in conn.execute(
            f"SELECT d.id FROM documents d WHERE d.id IN ({ph}){frag}", list(batch) + fparams
        ):
            out.add(r["id"])
    return out


def _fts_match(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression: quote each token so user input can't
    inject FTS operators. Tokens are implicitly AND-ed."""
    toks = _TOKEN_RE.findall(query or "")
    if not toks:
        return None
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


def _bm25(conn, match: str, limit: int, *, folder: str | None = None,
         tags: list[str] | None = None, filters: QueryFilters = _NO_FILTERS,
         ) -> list[tuple[int, float]]:
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
    # In-query operators (title:/path:/has:) ride the same push-down so a selective
    # operator can't fall out of the BM25 window on a large corpus.
    if filters.active:
        frag, fparams = _filter_sql(filters)
        sql.append(frag)
        params += fparams
    sql.append(" ORDER BY rank LIMIT ?")
    params.append(limit)
    rows = conn.execute("".join(sql), params).fetchall()
    return [(r["doc_id"], r["rank"]) for r in rows]


def _prepare_query_vector(
    db, embedder: Embedder, query: str
) -> tuple[EmbeddingBinding, bytes]:
    """Capture the process binding, then encode outside the SQLite read snapshot."""
    expected = db.expected_embedding_binding()
    actual_identity = (embedder.model_name, embedder.pipeline)
    expected_identity = (expected.model, expected.pipeline)
    if actual_identity != expected_identity:
        raise EmbeddingBindingChanged(
            f"Process embedder {actual_identity} does not match expected binding "
            f"{expected_identity}."
        )
    try:
        values = [float(value) for value in embedder.embed_query(query)]
    except (TypeError, ValueError) as exc:
        raise ValueError("query embedding must contain numeric values") from exc
    if len(values) != expected.dim:
        raise ValueError(
            "query embedding dimension does not match binding: "
            f"expected {expected.dim}, got {len(values)}"
        )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("query embedding must contain only finite values")
    if any(abs(value) > EMBEDDING_FLOAT32_MAX for value in values):
        raise ValueError("query embedding values must fit the float32 range")
    return expected, Embedder.serialize(values)


def _vector(conn, query_vector: bytes, limit: int) -> list[tuple[int, dict]]:
    """Vector KNN collapsed to one best chunk per document. Each value is a dict
    with the matched chunk's distance/heading/text/heading_path, its stable
    ``chunk_id`` and ``ordinal`` (a chunk address for read_chunk), and its
    ``char_start``/``char_end`` offsets (the exact body range, for deep-linking)."""
    rows = conn.execute(
        "SELECT chunk_id, distance FROM chunk_vectors "
        "WHERE embedding MATCH ? AND k=? ORDER BY distance",
        (query_vector, limit),
    ).fetchall()
    if not rows:
        return []
    # Resolve all matched chunks in one query instead of one SELECT per hit.
    ids = [r["chunk_id"] for r in rows]
    ph = ",".join("?" * len(ids))
    chunk_map = {
        c["id"]: c
        for c in conn.execute(
            f"SELECT id, doc_id, ordinal, heading, text, heading_path, char_start, char_end "
            f"FROM chunks WHERE id IN ({ph})", ids
        )
    }
    best: dict[int, dict] = {}  # doc_id -> matched-chunk info
    for r in rows:
        ch = chunk_map.get(r["chunk_id"])
        if not ch:
            continue
        d = ch["doc_id"]
        if d not in best or r["distance"] < best[d]["distance"]:
            best[d] = {
                "distance": r["distance"], "heading": ch["heading"], "text": ch["text"],
                "heading_path": ch["heading_path"], "chunk_id": ch["id"], "ordinal": ch["ordinal"],
                "char_start": ch["char_start"], "char_end": ch["char_end"],
            }
    return sorted(best.items(), key=lambda kv: kv[1]["distance"])


def _rerank_boost(title: str | None, vi: dict | None, q_norm: str,
                  params: FusionParams) -> float:
    """Small additive signals layered on top of pure RRF so the fusion can see what
    rank-only fusion can't: an exact (or prefix) title match, and how close the matched
    vector chunk actually is. Returns a boost in RRF-score units (≈1/rrf_k). With the
    defaults only the title boosts fire (proximity_weight=0), and only for the rare doc
    whose title matches the query — so ordinary results keep their RRF order."""
    boost = 0.0
    if q_norm and (params.title_exact_boost or params.title_prefix_boost):
        tn = " ".join((title or "").lower().split())
        if tn:
            if tn == q_norm:
                boost += params.title_exact_boost
            elif tn.startswith(q_norm):
                boost += params.title_prefix_boost
    if params.proximity_weight and vi is not None:
        sim = 1.0 - float(vi["distance"])  # cosine distance -> similarity
        if sim > 0:
            boost += params.proximity_weight * sim
    return boost


def _rank(conn, query: str, *, mode: str, k: int,
          folder: str | None, tags: list[str] | None,
          query_vector: bytes | None = None,
          filters: QueryFilters = _NO_FILTERS,
          params: FusionParams = DEFAULT_FUSION):
    """Core retrieval+RRF fusion shared by ``search_page`` and ``assemble_context``.

    Returns ``(scored, vec_info, match)`` where ``scored`` is ``[(doc_id, rrf_score)]``
    sorted best-first, ``vec_info`` maps doc_id -> a dict describing the document's
    closest vector chunk (``distance``/``heading``/``text``/``heading_path``/``chunk_id``/
    ``ordinal``), and ``match`` is the FTS MATCH expression (or None).
    BM25 pre-filters folder/tags in SQL; the vector leg can't, so callers still re-filter.
    """
    match = _fts_match(query) if mode in ("hybrid", "bm25") else None
    bm_list = _bm25(conn, match, k, folder=folder, tags=tags, filters=filters) if match else []
    # vec0 KNN can't pre-filter by folder/tag, and collapsing chunk hits to docs
    # yields fewer distinct docs than chunks fetched. Over-fetch chunks so the
    # post-dedup/post-filter set still holds ~k distinct docs.
    k_vec = min(k * params.vector_factor, params.vector_cap)
    if mode in ("hybrid", "vector"):
        if query_vector is None:
            raise RuntimeError("vector search requires a prepared query embedding")
        vec_list = _vector(conn, query_vector, k_vec)
    else:
        vec_list = []

    bm_rank = {doc_id: i + 1 for i, (doc_id, _) in enumerate(bm_list)}
    vec_rank = {doc_id: i + 1 for i, (doc_id, _) in enumerate(vec_list)}
    vec_info = {doc_id: info for doc_id, info in vec_list}

    if mode == "bm25":
        ids = [doc_id for doc_id, _ in bm_list]
    elif mode == "vector":
        ids = [doc_id for doc_id, _ in vec_list]
    else:
        ids = list(set(bm_rank) | set(vec_rank))

    # Title-match reranking needs each candidate's title; fetch them in one query
    # (only when a title boost is actually enabled).
    q_norm = " ".join((query or "").lower().split())
    titles: dict[int, str] = {}
    if ids and (params.title_exact_boost or params.title_prefix_boost):
        idlist = list(ids)
        ph = ",".join("?" * len(idlist))
        titles = {r["id"]: (r["title"] or "") for r in conn.execute(
            f"SELECT id, title FROM documents WHERE id IN ({ph})", idlist)}

    scored = []
    for did in ids:
        s = 0.0
        if did in bm_rank:
            s += 1.0 / (params.rrf_k + bm_rank[did])
        if did in vec_rank:
            s += 1.0 / (params.rrf_k + vec_rank[did])
        s += _rerank_boost(titles.get(did), vec_info.get(did), q_norm, params)
        scored.append((did, s))
    scored.sort(key=lambda x: -x[1])
    return scored, vec_info, match


def _chunk_match_for_tokens(conn, doc_id: int, q_tokens: list[str]) -> dict | None:
    """The chunk where the query tokens most land (heading/heading_path/text + its
    char_start/char_end offsets) — used to give a BM25-only hit the same section anchor,
    char range, and context preview the vector leg would have supplied (it didn't run for
    this doc). Mirrors RAG's passage selection. None if the doc has no indexed chunks."""
    rows = conn.execute(
        "SELECT ordinal, heading, heading_path, text, char_start, char_end "
        "FROM chunks WHERE doc_id=? ORDER BY ordinal",
        (doc_id,),
    ).fetchall()
    if not rows:
        return None
    # _best_token_chunk always returns an ordinal drawn from `rows`, so this lookup is
    # total — make that invariant explicit instead of a scan with an unreachable fallback.
    r = {row["ordinal"]: row for row in rows}[_best_token_chunk(rows, q_tokens)]
    return {"heading": r["heading"], "heading_path": r["heading_path"],
            "text": r["text"], "char_start": r["char_start"], "char_end": r["char_end"]}


def _context_preview(text: str | None, *, max_lines: int = 4, max_chars: int = 320) -> str | None:
    """A short, readable plain-text preview of the matched chunk: its leading non-empty
    lines, joined and capped. Distinct from ``snippet`` (FTS-centered, ``<mark>``-annotated,
    ~12 tokens): this is a run of surrounding prose so an agent can judge a hit's relevance
    without a ``read_chunk`` round-trip. None for empty/blank chunk text."""
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    preview = " ".join(lines[:max_lines])
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "…"
    return preview


def _tags_for_doc_ids(conn, ids: list[int]) -> dict[int, list[str]]:
    """Tags for many documents in ONE query (doc_id -> sorted tags). Replaces a
    per-document SELECT both when filtering vector candidates by tag and when
    attaching tags to a result page."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    out: dict[int, list[str]] = {}
    for row in conn.execute(f"SELECT doc_id, tag FROM tags WHERE doc_id IN ({ph}) ORDER BY tag", ids):
        out.setdefault(row["doc_id"], []).append(row["tag"])
    return out


def _docs_meta_for_ids(conn, ids: list[int]) -> dict[int, dict]:
    """Document metadata (id/path/title/version/folder/is_deleted) for many ids in ONE
    query (id -> row). Replaces the per-candidate ``SELECT ... WHERE id=?`` that ran
    once per scored candidate in the result-assembly loop (an N+1 that grew with how
    many candidates the filters discarded). The candidate set is already materialized
    in ``scored`` and the tag-filter path already batches over the same ids, so this is
    one extra small query, not a behavioural change."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    return {r["id"]: r for r in conn.execute(
        f"SELECT id, path, title, version, folder, updated_at, is_deleted "
        f"FROM documents WHERE id IN ({ph})", ids)}


def _doc_lengths_for_ids(conn, ids: list[int]) -> dict[int, int]:
    """Character length of each document's latest-revision body, in ONE batched query
    (doc_id -> length). ``LENGTH()`` is evaluated in SQLite so the body text never
    crosses into Python — only the integer count does. Lets a search caller tell a short
    overview note from a long reference doc (triage which hit to read first) without a
    follow-up read per result. Works for unembedded docs too (revisions, not chunks)."""
    if not ids:
        return {}
    out: dict[int, int] = {}
    for i in range(0, len(ids), 400):
        batch = ids[i:i + 400]
        ph = ",".join("?" * len(batch))
        for r in conn.execute(
            f"SELECT r.doc_id AS doc_id, LENGTH(r.body) AS n FROM revisions r "
            f"JOIN (SELECT doc_id, MAX(version) AS v FROM revisions WHERE doc_id IN ({ph}) "
            f"GROUP BY doc_id) m ON m.doc_id=r.doc_id AND m.v=r.version", batch
        ):
            out[r["doc_id"]] = r["n"]
    return out


def _link_counts_for_ids(conn, ids: list[int]) -> dict[int, tuple[int, int]]:
    """(backlinks, outlinks) counts for many docs in two grouped queries — backlinks =
    resolved links pointing AT the doc, outlinks = links it points OUT to. doc_id ->
    (backlinks, outlinks); missing docs default to (0, 0)."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    back = {r[0]: r[1] for r in conn.execute(
        f"SELECT dst_doc_id, COUNT(*) FROM links WHERE dst_doc_id IN ({ph}) AND is_resolved=1 "
        f"GROUP BY dst_doc_id", ids)}
    out = {r[0]: r[1] for r in conn.execute(
        f"SELECT src_doc_id, COUNT(*) FROM links WHERE src_doc_id IN ({ph}) GROUP BY src_doc_id", ids)}
    return {i: (back.get(i, 0), out.get(i, 0)) for i in ids}


def _passes_filters(d, folder: str | None, tags: list[str] | None, conn,
                    tag_map: dict[int, list[str]] | None = None) -> bool:
    """Re-apply folder/tag filters to a vector-matched doc (the BM25 leg already
    filtered in SQL; the vec leg returns whatever the corpus held). When ``tag_map``
    is supplied (batch-loaded once for all candidates) the per-doc tag query is skipped."""
    if folder:
        f = folder.strip("/")
        if not (d["folder"] == f or d["folder"].startswith(f + "/")):
            return False
    if tags:
        if tag_map is not None:
            doctags = set(tag_map.get(d["id"], []))
        else:
            doctags = {t[0] for t in conn.execute("SELECT tag FROM tags WHERE doc_id=?", (d["id"],))}
        if not set(tags).issubset(doctags):
            return False
    return True


def search_page(
    db, embedder: Embedder, query: str, *,
    mode: str = "hybrid", top_k: int = 10,
    folder: str | None = None, tags: list[str] | None = None,
    since: str | None = None, until: str | None = None,
    params: FusionParams = DEFAULT_FUSION,
) -> tuple[list[SearchResult], bool]:
    """Run a search and report truncation. Returns ``(results, truncated)`` where
    ``truncated`` is True only when at least one more qualifying document existed
    beyond ``top_k`` — so a corpus of exactly ``top_k`` matches reports False (no
    misleading 'raise top_k' signal). ``results`` is capped at ``top_k``.
    ``since``/``until`` (ISO-8601) bound the hits by ``updated_at`` (recency filter).
    The query may carry ``title:``/``path:``/``has:`` operators (see ``parse_query_filters``)
    which refine the text search in one call; an operator-only query (no search terms left)
    is rejected with ``validation``."""
    if mode not in ("hybrid", "bm25", "vector"):
        mode = "hybrid"
    SEARCH_QUERIES.labels(mode).inc()
    t0 = time.perf_counter()
    top_k = clamp_int(top_k, 1, 50)
    want = top_k + 1  # over-collect by one survivor so truncation is exact, not len>=cap
    k = max(top_k * params.candidate_factor, params.candidate_min)
    text, filters = parse_query_filters(query)
    if not text:
        from .services.errors import ValidationError  # local: avoid services<->search cycle
        raise ValidationError(
            "Provide search terms; operators (title:/path:/has:) only refine a text query.")
    q_tokens = [t.lower() for t in _TOKEN_RE.findall(text)]

    expected: EmbeddingBinding | None = None
    query_vector: bytes | None = None
    if mode in ("hybrid", "vector"):
        expected, query_vector = _prepare_query_vector(db, embedder, text)

    read_context = (
        db.embedding_read_snapshot(expected) if expected is not None else db.reader()
    )
    with read_context as conn:
        scored, vec_info, match = _rank(
            conn, text, mode=mode, k=k, folder=folder, tags=tags,
            query_vector=query_vector, filters=filters, params=params,
        )

        # Batch-load the candidates' metadata (and, when a tag filter is active, their
        # tags) once up front instead of one SELECT per doc inside the loop — the loop
        # otherwise issued an N+1 that grew with how many candidates the filters discard.
        scored_ids = [d for d, _ in scored]
        doc_meta = _docs_meta_for_ids(conn, scored_ids)
        filter_tags = _tags_for_doc_ids(conn, scored_ids) if tags else None
        # The vector leg can't push title:/path:/has: into SQL, so compute the operator-
        # satisfying id set once (None when no operators) and gate both legs on it.
        allowed = _filtered_ids(conn, scored_ids, filters)

        results: list[SearchResult] = []
        result_ids: list[int] = []
        for did, score in scored:
            d = doc_meta.get(did)
            if not d or d["is_deleted"]:
                continue
            if allowed is not None and did not in allowed:
                continue
            if not _passes_filters(d, folder, tags, conn, filter_tags):
                continue
            # Recency window (string comparison is valid for our canonical UTC ISO format).
            if since and (d["updated_at"] or "") < since:
                continue
            if until and (d["updated_at"] or "") > until:
                continue

            heading = None
            heading_path = None
            char_start = char_end = None
            context_preview = None
            snippet = ""
            vi = vec_info.get(did)
            if match:
                srow = conn.execute(
                    "SELECT snippet(documents_fts, 1, '<mark>', '</mark>', ' … ', 12) "
                    "FROM documents_fts WHERE rowid=? AND documents_fts MATCH ?",
                    (did, match),
                ).fetchone()
                if srow and srow[0]:
                    snippet = srow[0]
            if vi is not None:
                heading = vi["heading"]
                heading_path = vi["heading_path"]
                char_start, char_end = vi["char_start"], vi["char_end"]
                context_preview = _context_preview(vi["text"])
                if not snippet:
                    snippet = vi["text"][:240]
            elif match:
                # BM25-only hit (vector leg skipped, or this doc is unembedded): the
                # section anchor/char range would otherwise be None. Derive them from the
                # chunk the query tokens land in, so deep-linking and a context preview
                # work in every search mode.
                cm = _chunk_match_for_tokens(conn, did, q_tokens)
                if cm:
                    heading, heading_path = cm["heading"], cm["heading_path"]
                    char_start, char_end = cm["char_start"], cm["char_end"]
                    context_preview = _context_preview(cm["text"])

            results.append(SearchResult(
                path=d["path"], title=d["title"] or d["path"],
                score=round(score, 6), snippet=snippet, heading=heading, version=d["version"],
                heading_path=heading_path,
                anchor=heading_slug(heading) if heading else None,
                chunk_ordinal=vi["ordinal"] if vi is not None else None,
                chunk_id=vi["chunk_id"] if vi is not None else None,
                folder=d["folder"] or "",
                updated_at=d["updated_at"],
                section_depth=(heading_path.count(" > ") + 1) if heading_path else None,
                char_start=char_start, char_end=char_end, context_preview=context_preview,
            ))
            result_ids.append(did)
            if len(results) >= want:
                break

        # Attach each result's tags, link counts, and body length (one batched query each)
        # so an agent can group/filter and triage short-vs-long without a follow-up read.
        if result_ids:
            tagmap = _tags_for_doc_ids(conn, result_ids)
            linkmap = _link_counts_for_ids(conn, result_ids)
            lenmap = _doc_lengths_for_ids(conn, result_ids)
            for r, did in zip(results, result_ids, strict=True):
                r.tags = tagmap.get(did, [])
                r.backlinks_count, r.outlinks_count = linkmap.get(did, (0, 0))
                r.content_length = lenmap.get(did)
    SEARCH_LATENCY.labels(mode).observe(time.perf_counter() - t0)
    return results[:top_k], len(results) > top_k


def search(
    db, embedder: Embedder, query: str, *,
    mode: str = "hybrid", top_k: int = 10,
    folder: str | None = None, tags: list[str] | None = None,
    params: FusionParams = DEFAULT_FUSION,
) -> list[SearchResult]:
    """Backward-compatible list API: results only, no truncation flag."""
    return search_page(db, embedder, query, mode=mode, top_k=top_k,
                       folder=folder, tags=tags, params=params)[0]


def related_documents(db, source_doc_id: int, *, k: int = 8,
                      max_src_chunks: int = 12) -> list[dict]:
    """Run related-document vector reads in one binding-verified snapshot."""
    expected = db.expected_embedding_binding()
    with db.embedding_read_snapshot(expected) as conn:
        return _related_documents(
            conn, source_doc_id, k=k, max_src_chunks=max_src_chunks
        )


def _related_documents(conn, source_doc_id: int, *, k: int = 8,
                       max_src_chunks: int = 12) -> list[dict]:
    """Documents most semantically similar to ``source_doc_id``, via the chunk
    vectors already in the index (no model forward pass — the stored source vectors
    are themselves the KNN queries). For each of the source's leading chunks we run a
    KNN and keep, per other document, its single closest chunk distance; results are
    ranked by that best distance. Returns ``[{path, title, folder, score}]`` where
    ``score`` is cosine similarity (``1 - distance``), best first. Empty when the
    source has no vectors yet (e.g. not embedded)."""
    k = clamp_int(k, 1, 50)
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
    # Batch-load metadata for all candidate docs in one query rather than a SELECT per
    # neighbour (the candidate set can be 100s of docs on a large vault).
    ids = [did for did, _ in ordered]
    ph = ",".join("?" * len(ids))
    meta = {m["id"]: m for m in conn.execute(
        f"SELECT id, path, title, folder, is_deleted FROM documents WHERE id IN ({ph})", ids)}
    out: list[dict] = []
    for did, dist in ordered:
        d = meta.get(did)
        if not d or d["is_deleted"]:
            continue
        out.append({"path": d["path"], "title": d["title"] or d["path"],
                    "folder": d["folder"], "score": round(1.0 - dist, 4)})
        if len(out) >= k:
            break
    return out


def _trim_to_budget(text: str, limit: int) -> tuple[str, bool]:
    """Trim ``text`` to <= ``limit`` chars at a natural boundary (paragraph > line >
    sentence > word) instead of mid-token, and never leave a half-open code fence.
    Returns ``(trimmed, was_truncated)``."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    for sep in ("\n\n", "\n", ". ", "。", " "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:  # don't discard more than half just to hit a boundary
            cut = cut[:idx]
            break
    cut = cut.rstrip()
    if cut.count("```") % 2 == 1:  # balance an unclosed fence so the snippet stays valid
        cut += "\n```"
    return cut, True


def _best_token_chunk(rows, q_tokens: list[str]) -> int:
    """Ordinal of the chunk containing the most query tokens (lead chunk as fallback) —
    used when a doc matched on BM25 only, so its citation comes from where the query
    actually appears rather than blindly from the first chunk."""
    if not q_tokens:
        return rows[0]["ordinal"]
    best_ord, best_score = rows[0]["ordinal"], -1
    for r in rows:
        low = (r["text"] or "").lower()
        score = sum(low.count(tok) for tok in q_tokens)
        if score > best_score:
            best_score, best_ord = score, r["ordinal"]
    return best_ord


def _passage_for_doc(conn, doc_id: int, vi: dict | None, q_tokens: list[str],
                     budget: int, *, max_chunks: int = 5) -> tuple[str | None, str, bool] | None:
    """Build a citation passage for one document: start at the most relevant chunk (the
    vector-matched one, else the best token-overlap chunk) and expand to neighbours
    (after, then before) in ordinal order while ``budget`` allows, so a passage that
    straddles a chunk boundary isn't cut in half. The joined text is boundary-trimmed
    to ``budget``. Returns ``(heading, text, truncated)`` or None if the doc has no
    usable chunk text."""
    rows = conn.execute(
        "SELECT ordinal, heading, text FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)
    ).fetchall()
    if not rows:
        return None
    by_ord = {r["ordinal"]: r for r in rows}
    if vi is not None and vi["ordinal"] in by_ord:
        center = vi["ordinal"]
    else:
        center = _best_token_chunk(rows, q_tokens)
    heading = by_ord[center]["heading"]

    picked = [center]
    used = len((by_ord[center]["text"] or "").strip())
    lo = hi = center
    while len(picked) < max_chunks:
        progressed = False
        nxt = by_ord.get(hi + 1)
        if nxt is not None:
            t = (nxt["text"] or "").strip()
            if used + len(t) + 2 <= budget:
                hi += 1
                picked.append(hi)
                used += len(t) + 2
                progressed = True
        if len(picked) < max_chunks:
            prv = by_ord.get(lo - 1)
            if prv is not None:
                t = (prv["text"] or "").strip()
                if used + len(t) + 2 <= budget:
                    lo -= 1
                    picked.append(lo)
                    used += len(t) + 2
                    progressed = True
        if not progressed:
            break

    raw = "\n\n".join((by_ord[o]["text"] or "").strip() for o in sorted(picked)).strip()
    if not raw:
        return None
    text, trunc = _trim_to_budget(raw, budget)
    return heading, text, trunc


def assemble_context(
    db, embedder: Embedder, question: str, *,
    max_chars: int = 6000, max_sources: int = 8, mode: str = "hybrid",
    folder: str | None = None, tags: list[str] | None = None,
    params: FusionParams = DEFAULT_FUSION,
) -> dict:
    """Retrieve and assemble citation-tagged context for a question — a one-call RAG
    primitive for LLM clients. Ranks documents with the same hybrid retriever as
    search, then for each top document includes its most relevant passage (the
    vector-matched chunk, or the best token-overlap chunk for a BM25-only match),
    expanded to neighbouring chunks while the budget allows and boundary-trimmed (never
    mid-word or mid-code-fence), in rank order until ``max_chars`` or ``max_sources``.
    Returns ``context`` (assembled text with ``[n]`` markers), the ``sources`` those
    markers cite, and ``truncated`` (more relevant content existed beyond the budget).
    The question may carry the same ``title:``/``path:``/``has:`` operators as search
    (see ``parse_query_filters``); an operator-only question is rejected with
    ``validation``."""
    if mode not in ("hybrid", "bm25", "vector"):
        mode = "hybrid"
    max_chars = clamp_int(max_chars, 200, 24000)
    max_sources = clamp_int(max_sources, 1, 20)
    k = max(max_sources * params.candidate_factor, params.candidate_min)
    text, filters = parse_query_filters(question)
    if not text:
        from .services.errors import ValidationError  # local: avoid services<->search cycle
        raise ValidationError(
            "Provide a question; operators (title:/path:/has:) only refine a text query.")
    q_tokens = [t.lower() for t in _TOKEN_RE.findall(text)]

    expected: EmbeddingBinding | None = None
    query_vector: bytes | None = None
    if mode in ("hybrid", "vector"):
        expected, query_vector = _prepare_query_vector(db, embedder, text)

    sources: list[dict] = []
    parts: list[str] = []
    total = 0
    truncated = False
    read_context = (
        db.embedding_read_snapshot(expected) if expected is not None else db.reader()
    )
    with read_context as conn:
        scored, vec_info, _match = _rank(
            conn, text, mode=mode, k=k, folder=folder, tags=tags,
            query_vector=query_vector, filters=filters, params=params,
        )
        # One batched metadata (and tag) load for all candidates, not a SELECT per doc.
        scored_ids = [d for d, _ in scored]
        doc_meta = _docs_meta_for_ids(conn, scored_ids)
        filter_tags = _tags_for_doc_ids(conn, scored_ids) if tags else None
        allowed = _filtered_ids(conn, scored_ids, filters)  # gate the vector leg on operators
        for did, score in scored:
            if len(sources) >= max_sources:
                truncated = True
                break
            d = doc_meta.get(did)
            if not d or d["is_deleted"] or not _passes_filters(d, folder, tags, conn, filter_tags):
                continue
            if allowed is not None and did not in allowed:
                continue

            remaining = max_chars - total
            if remaining <= 0:
                truncated = True
                break
            passage = _passage_for_doc(conn, did, vec_info.get(did), q_tokens, remaining)
            if passage is None:
                continue
            heading, text, was_trunc = passage
            if was_trunc:
                truncated = True
            n = len(sources) + 1
            cite = f"[{n}] {d['path']}" + (f" › {heading}" if heading else "")
            parts.append(f"{cite}\n{text}")
            total += len(text)
            sources.append({
                "n": n, "path": d["path"], "title": d["title"] or d["path"],
                "heading": heading, "version": d["version"],
                "score": round(score, 6), "chars": len(text),
            })

    context = "\n\n".join(parts)
    return {"question": question, "context": context, "char_count": len(context),
            "count": len(sources), "truncated": truncated, "sources": sources}
