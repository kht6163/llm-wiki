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
        "(path_norm = ? OR path_norm LIKE ? ESCAPE '\\')",
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
    at it (by explicit path or by bare name)."""
    conn.execute(
        "UPDATE links SET dst_doc_id=?, is_resolved=1 WHERE is_resolved=0 AND "
        "((dst_is_path=1 AND dst_path_norm=?) OR (dst_is_path=0 AND dst_name=?))",
        (doc_id, doc_path_norm, doc_stem),
    )


def unresolve_incoming(conn: sqlite3.Connection, doc_id: int) -> None:
    """When a document is deleted, incoming links become broken (not removed)."""
    conn.execute(
        "UPDATE links SET dst_doc_id=NULL, is_resolved=0 WHERE dst_doc_id=?",
        (doc_id,),
    )


def get_backlinks(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT d.path AS src_path, d.title AS src_title, l.alias, l.anchor, l.link_type "
        "FROM links l JOIN documents d ON d.id=l.src_doc_id "
        "WHERE l.dst_doc_id=? AND d.is_deleted=0 ORDER BY d.path",
        (doc_id,),
    ).fetchall()
    return [dict(r) for r in rows]


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


def build_graph(
    conn: sqlite3.Connection,
    root_path: str | None = None,
    depth: int = 1,
    limit: int = 500,
    include_unresolved: bool = True,
) -> dict:
    """Return {nodes, edges} for Cytoscape/D3. With a root, BFS outward over links
    (both directions) up to ``depth``; otherwise the most-recent ``limit`` docs."""
    depth = max(1, min(depth, 3))
    limit = max(1, min(limit, 2000))
    root_norm = path_norm(normalize_rel_path(root_path)) if root_path else None

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
            nxt: set[int] = set()
            for did in frontier:
                for r in conn.execute(
                    "SELECT dst_doc_id FROM links WHERE src_doc_id=? AND dst_doc_id IS NOT NULL", (did,)
                ):
                    nxt.add(r[0])
                for r in conn.execute("SELECT src_doc_id FROM links WHERE dst_doc_id=?", (did,)):
                    nxt.add(r[0])
            frontier = nxt - visited
        visited |= frontier
        doc_ids = list(visited)[:limit]
        truncated = len(visited) > limit
    else:
        doc_ids = [r[0] for r in conn.execute(
            "SELECT id FROM documents WHERE is_deleted=0 ORDER BY updated_at DESC LIMIT ?", (limit,)
        )]
        total = conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0]
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
        if target_id in nodes:
            nodes[target_id]["degree"] += 1

    return {"ok": True, "root": root_path, "depth": depth, "truncated": truncated,
            "nodes": list(nodes.values()), "edges": edges}
