"""Link graph: resolve wikilinks/markdown links to documents, store edges,
compute backlinks, and build a node/edge graph for the web visualization.
"""
from __future__ import annotations

import sqlite3

from .markdown_utils import Link
from .util import PathError, basename_stem, normalize_rel_path, path_norm


def _link_keys(target: str) -> tuple[str, str, bool]:
    """(dst_path_norm, dst_name, dst_is_path) for a link target. ``dst_is_path``
    is true when the target was written as a path (has '/' or .md) vs a bare note
    name (Obsidian style, resolved by basename)."""
    is_path = ("/" in target) or target.lower().endswith(".md")
    rel = normalize_rel_path(target)
    return path_norm(rel), basename_stem(rel).lower(), is_path


def _resolve(
    conn: sqlite3.Connection, dst_path_norm: str, dst_name: str, is_path: bool, src_folder: str
) -> int | None:
    if is_path:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=? AND is_deleted=0",
            (dst_path_norm,),
        ).fetchone()
        return row["id"] if row else None
    rows = conn.execute(
        "SELECT id, folder FROM documents WHERE is_deleted=0 AND "
        "(path_norm = ? OR path_norm LIKE ? ESCAPE '\\') "
        # Deterministic tiebreaker when several folders hold the same basename and no
        # same-folder match applies: shallowest folder first (root wins), then by path.
        # Without ORDER BY, rows[0] is whatever order SQLite returns, which a VACUUM or
        # a restore can change — making bare-name links resolve differently across runs.
        "ORDER BY folder, path_norm",
        (dst_name + ".md", "%/" + _like_escape(dst_name) + ".md"),
    ).fetchall()
    if not rows:
        return None
    for r in rows:  # prefer a match in the same folder as the source
        if r["folder"] == src_folder:
            return r["id"]
    return rows[0]["id"]


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def store_links(conn: sqlite3.Connection, src_doc_id: int, links: list[Link], src_folder: str) -> None:
    """Replace all outgoing edges for a document with freshly parsed+resolved ones."""
    conn.execute("DELETE FROM links WHERE src_doc_id=?", (src_doc_id,))
    seen: set[tuple] = set()
    for link in links:
        try:
            dst_path_norm, dst_name, is_path = _link_keys(link.target)
        except PathError:
            continue
        key = (dst_path_norm, link.anchor, link.type)
        if key in seen:
            continue
        seen.add(key)
        dst_id = _resolve(conn, dst_path_norm, dst_name, is_path, src_folder)
        conn.execute(
            "INSERT INTO links(src_doc_id, dst_doc_id, dst_path_norm, dst_name, "
            "dst_is_path, link_type, alias, anchor, is_resolved, char_start, raw) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                src_doc_id, dst_id, dst_path_norm, dst_name, int(is_path),
                link.type, link.alias, link.anchor, int(dst_id is not None),
                link.start, link.raw,
            ),
        )


def resolve_path(conn: sqlite3.Connection, target: str, src_folder: str = "") -> str | None:
    """Resolve a link target to an existing document's path, or None."""
    try:
        dst_path_norm, dst_name, is_path = _link_keys(target)
    except PathError:
        return None
    did = _resolve(conn, dst_path_norm, dst_name, is_path, src_folder)
    if not did:
        return None
    r = conn.execute("SELECT path FROM documents WHERE id=?", (did,)).fetchone()
    return r["path"] if r else None


def backfill_links_for(conn: sqlite3.Connection, doc_id: int, doc_path_norm: str, doc_stem: str) -> None:
    """When a document is (re)created, resolve previously-dangling links that point
    at it. Explicit-path links match unambiguously. Bare-name links are re-resolved
    against each link's OWN source folder so the same-folder-first rule still holds:
    a blanket update would point a folder-local ``[[A]]`` at a new ``A.md`` in a
    different folder, disagreeing with how ``store_links`` (via ``_resolve``) would
    have resolved it live."""
    # Explicit-path links: the path is unambiguous, so a direct match is safe.
    conn.execute(
        "UPDATE links SET dst_doc_id=?, is_resolved=1 "
        "WHERE is_resolved=0 AND dst_is_path=1 AND dst_path_norm=?",
        (doc_id, doc_path_norm),
    )
    # Bare-name links: re-resolve each through _resolve with its source folder, which
    # applies same-folder-first + the deterministic tiebreaker over all current matches
    # — so a backfilled target is identical to the live-resolved one. We re-resolve two
    # sets: (a) still-dangling links, and (b) links whose source is in THIS doc's folder
    # — a new same-folder note shadows whatever a folder-local ``[[name]]`` resolved to
    # before, so those must repoint to it.
    fr = conn.execute("SELECT folder FROM documents WHERE id=?", (doc_id,)).fetchone()
    new_folder = fr["folder"] if fr else ""
    rows = conn.execute(
        "SELECT l.id AS link_id, d.folder AS src_folder FROM links l "
        "JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.dst_is_path=0 AND l.dst_name=? AND d.is_deleted=0 "
        "AND (l.is_resolved=0 OR d.folder=?)",
        (doc_stem, new_folder),
    ).fetchall()
    for r in rows:
        rid = _resolve(conn, doc_stem + ".md", doc_stem, False, r["src_folder"])
        conn.execute(
            "UPDATE links SET dst_doc_id=?, is_resolved=? WHERE id=?",
            (rid, 1 if rid is not None else 0, r["link_id"]),
        )


def unresolve_incoming(conn: sqlite3.Connection, doc_id: int) -> None:
    """When a document is deleted, incoming links become broken (not removed)."""
    conn.execute(
        "UPDATE links SET dst_doc_id=NULL, is_resolved=0 WHERE dst_doc_id=?",
        (doc_id,),
    )


def _latest_bodies(conn: sqlite3.Connection, doc_ids: list[int]) -> dict[int, str]:
    """Latest-revision body text per doc id, in batched IN(...) queries (chunked under
    SQLite's bound-parameter limit). This stored body is the SAME coordinate space that
    ``links.char_start`` indexes into — ``extract_links`` masks frontmatter/code to
    equal-length spaces, so a link offset maps straight onto the raw body."""
    out: dict[int, str] = {}
    chunk = 400
    for i in range(0, len(doc_ids), chunk):
        part = doc_ids[i:i + chunk]
        placeholders = ",".join("?" * len(part))
        rows = conn.execute(
            f"SELECT r.doc_id AS doc_id, r.body AS body FROM revisions r "
            f"JOIN (SELECT doc_id, MAX(version) AS v FROM revisions "
            f"      WHERE doc_id IN ({placeholders}) GROUP BY doc_id) m "
            f"ON m.doc_id=r.doc_id AND m.v=r.version",
            part,
        ).fetchall()
        for r in rows:
            out[r["doc_id"]] = r["body"] or ""
    return out


def _link_context(body: str, char_start: int | None, radius: int = 120) -> str | None:
    """A one-line snippet of ``body`` around ``char_start`` (a link's offset), expanded to
    whitespace boundaries and whitespace-collapsed, with … where it was clipped. Returns
    None when there's no usable offset (legacy/NULL char_start) so the caller omits it."""
    if char_start is None or not body:
        return None
    n = len(body)
    if char_start < 0 or char_start > n:
        return None
    lo, hi = max(0, char_start - radius), min(n, char_start + radius)
    while lo > 0 and not body[lo - 1].isspace():
        lo -= 1
    while hi < n and not body[hi].isspace():
        hi += 1
    snippet = " ".join(body[lo:hi].split())
    if not snippet:
        return None
    return ("… " if lo > 0 else "") + snippet + (" …" if hi < n else "")


def get_backlinks(conn: sqlite3.Connection, doc_id: int, *,
                  with_context: bool = False, context_radius: int = 120) -> list[dict]:
    """Documents linking to ``doc_id``. With ``with_context`` each backlink also carries a
    ``context`` snippet (the surrounding sentence of the inbound link) so a caller learns
    WHY each doc links here without one read per source — the bodies are loaded in a single
    batched query and sliced at the link offset."""
    rows = conn.execute(
        "SELECT d.id AS src_id, d.path AS src_path, d.title AS src_title, "
        "l.alias, l.anchor, l.link_type, l.char_start "
        "FROM links l JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.dst_doc_id=? AND d.is_deleted=0 ORDER BY d.path, l.char_start",
        (doc_id,),
    ).fetchall()
    out = [{"src_path": r["src_path"], "src_title": r["src_title"],
            "alias": r["alias"], "anchor": r["anchor"], "link_type": r["link_type"]}
           for r in rows]
    if with_context and rows:
        bodies = _latest_bodies(conn, sorted({r["src_id"] for r in rows}))
        for o, r in zip(out, rows, strict=True):
            ctx = _link_context(bodies.get(r["src_id"], ""), r["char_start"], context_radius)
            if ctx:
                o["context"] = ctx
    return out


def list_broken_links(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Every unresolved (dangling) link across non-deleted documents, ordered by
    source path. ``target`` is the path or bare note name the link points at."""
    rows = conn.execute(
        "SELECT d.path AS src_path, l.dst_path_norm, l.dst_name, l.dst_is_path, "
        "l.link_type, l.alias, l.anchor, l.raw "
        "FROM links l JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.is_resolved=0 AND d.is_deleted=0 "
        "ORDER BY d.path, l.char_start LIMIT ?",
        (limit,),
    ).fetchall()
    return [{
        "src_path": r["src_path"],
        "target": r["dst_path_norm"] if r["dst_is_path"] else r["dst_name"],
        "link_type": r["link_type"], "alias": r["alias"],
        "anchor": r["anchor"], "raw": r["raw"],
    } for r in rows]


def get_outgoing(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT l.dst_path_norm, l.dst_name, l.dst_doc_id, l.is_resolved, l.link_type, "
        "l.alias, l.anchor, d.path AS dst_path, d.title AS dst_title "
        "FROM links l LEFT JOIN documents d ON d.id=l.dst_doc_id "
        "WHERE l.src_doc_id=? ORDER BY l.char_start",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _neighbors(conn: sqlite3.Connection, ids: list[int]) -> set[int]:
    """All document ids directly linked to/from any of ``ids`` (both directions),
    in batched IN(...) queries instead of two per node. Chunked to stay under
    SQLite's bound-parameter limit for large frontiers."""
    out: set[int] = set()
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT dst_doc_id FROM links WHERE src_doc_id IN ({ph}) AND dst_doc_id IS NOT NULL",
            chunk,
        ):
            out.add(r[0])
        for r in conn.execute(
            f"SELECT src_doc_id FROM links WHERE dst_doc_id IN ({ph})", chunk
        ):
            out.add(r[0])
    return out


def _doc_filter_clause(
    folder: str | None, tags: list[str] | None,
) -> tuple[str, list]:
    """SQL fragment (AND …) + params restricting documents by folder subtree and tags."""
    clauses: list[str] = []
    params: list = []
    if folder:
        f = folder.strip("/")
        if f:
            clauses.append("(folder=? OR folder LIKE ?)")
            params += [f, f + "/%"]
    for t in tags or []:
        if t:
            clauses.append("id IN (SELECT doc_id FROM tags WHERE tag=?)")
            params.append(t)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def build_graph(
    conn: sqlite3.Connection,
    root_path: str | None = None,
    depth: int = 1,
    limit: int = 500,
    include_unresolved: bool = True,
    folder: str | None = None,
    tag: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Return {nodes, edges} for Cytoscape/D3. With a root, BFS outward over links
    (both directions) up to ``depth``; otherwise the most-recent ``limit`` docs.

    Optional ``folder`` / ``tag`` / ``tags`` restrict the node set to matching
    documents (folder = subtree; tags = AND). Unresolved ghost nodes are only
    emitted when ``include_unresolved`` is true."""
    depth = max(1, min(depth, 3))
    limit = max(1, min(limit, 2000))
    root_norm = path_norm(normalize_rel_path(root_path)) if root_path else None
    raw_tags = [t for t in ([tag] if tag else []) + list(tags or []) if t]
    tag_list: list[str] = []
    for t in raw_tags:
        if t not in tag_list:
            tag_list.append(t)
    filt_sql, filt_params = _doc_filter_clause(folder, tag_list)

    if root_norm:
        root = conn.execute(
            "SELECT id, path FROM documents WHERE path_norm=? AND is_deleted=0",
            (root_norm,),
        ).fetchone()
        if not root:
            return {"ok": True, "root": root_path, "depth": depth, "truncated": False,
                    "nodes": [], "edges": []}
        visited: set[int] = set()
        frontier = {root["id"]}
        for _ in range(depth):
            visited |= frontier
            if not frontier:
                break
            # One pair of batched queries per BFS level (was two per frontier node:
            # an N+1 that dominated query count on wide graphs up to the 2000 cap).
            frontier = _neighbors(conn, list(frontier)) - visited
        visited |= frontier
        # When folder/tag filters are active, keep only matching docs from the BFS set.
        if filt_sql:
            ph = ",".join("?" * len(visited)) if visited else "NULL"
            if visited:
                match_ids = {
                    r[0]
                    for r in conn.execute(
                        f"SELECT id FROM documents WHERE id IN ({ph}) AND is_deleted=0{filt_sql}",
                        list(visited) + filt_params,
                    )
                }
            else:
                match_ids = set()
            # Always keep the root so a focused graph is not emptied by a narrow filter.
            match_ids.add(root["id"])
            visited = match_ids
        doc_ids = list(visited)[:limit]
        truncated = len(visited) > limit
    else:
        doc_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM documents WHERE is_deleted=0{filt_sql} "
            "ORDER BY updated_at DESC LIMIT ?",
            filt_params + [limit],
        )]
        total = conn.execute(
            f"SELECT COUNT(*) FROM documents WHERE is_deleted=0{filt_sql}",
            filt_params,
        ).fetchone()[0]
        truncated = total > limit

    id_set = set(doc_ids)
    nodes: dict[str, dict] = {}
    path_by_id: dict[int, str] = {}
    link_rows: list = []
    if doc_ids:
        # Batch the node metadata, tags, and outgoing edges into three queries
        # rather than 3×N per-node queries (N up to the 2000-node cap).
        ph = ",".join("?" * len(doc_ids))
        tag_map: dict[int, list[str]] = {}
        for tr in conn.execute(f"SELECT doc_id, tag FROM tags WHERE doc_id IN ({ph})", doc_ids):
            tag_map.setdefault(tr["doc_id"], []).append(tr["tag"])
        for d in conn.execute(
            f"SELECT id, path, title, folder FROM documents WHERE id IN ({ph})", doc_ids
        ):
            path_by_id[d["id"]] = d["path"]
            nodes[d["path"]] = {
                "id": d["path"], "label": d["title"] or d["path"], "exists": True,
                "folder": d["folder"], "tags": sorted(tag_map.get(d["id"], [])), "degree": 0,
                "is_root": (root_norm is not None and d["path"].lower() == root_norm),
            }
        link_rows = conn.execute(
            f"SELECT src_doc_id, dst_doc_id, dst_name, is_resolved, link_type, alias, anchor "
            f"FROM links WHERE src_doc_id IN ({ph})", doc_ids
        ).fetchall()

    edges: list[dict] = []
    eid = 0
    for lk in link_rows:
        src = path_by_id.get(lk["src_doc_id"])
        if src is None:
            continue
        if lk["dst_doc_id"] is not None:
            if lk["dst_doc_id"] not in id_set:
                continue
            target_id = path_by_id[lk["dst_doc_id"]]
        else:
            if not include_unresolved:
                continue
            target_id = "unresolved:" + (lk["dst_name"] or "?")
            if target_id not in nodes:
                nodes[target_id] = {
                    "id": target_id, "label": lk["dst_name"] or "?", "exists": False,
                    "folder": None, "tags": [], "degree": 0, "is_root": False,
                }
        eid += 1
        edges.append({
            "id": f"e{eid}", "source": src, "target": target_id,
            "type": lk["link_type"], "resolved": bool(lk["is_resolved"]),
            "alias": lk["alias"], "anchor": lk["anchor"],
        })
        nodes[src]["degree"] += 1
        # Resolved targets came from ``path_by_id`` and unresolved targets are inserted
        # above, so every emitted edge has a node at both ends.
        nodes[target_id]["degree"] += 1

    return {"ok": True, "root": root_path, "depth": depth, "truncated": truncated,
            "nodes": list(nodes.values()), "edges": edges}
