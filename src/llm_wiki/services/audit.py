"""Append-only audit trail for security-relevant events: document writes, auth
outcomes, API-key lifecycle, and account/role changes. Stored in the ``audit_log``
table so "who changed what, when, over which surface" is answerable.

Read-heavy MCP traffic is logged to the application logger instead (see
mcp_server), to avoid taking the writer lock on every read.
"""
from __future__ import annotations

import logging

from ..db import Database
from ..util import clamp_int, now_iso

log = logging.getLogger("llm_wiki.audit")

# Document/content activity — the subset of the audit trail that's safe and useful
# to surface to editors and LLM agents (the "what changed in the vault" feed).
# Security/account events (login*, key_*, user_*, role_change, password_change) are
# deliberately excluded here and only shown to admins via the unfiltered feed.
DOC_ACTIONS: tuple[str, ...] = (
    "doc_create", "doc_update", "doc_move", "doc_delete",
    "doc_reconcile", "attachment_upload",
)


def record(conn, *, actor: str | None, via: str, action: str,
           target: str | None = None, outcome: str = "ok", detail: str | None = None) -> None:
    """Insert one audit row on an EXISTING write connection (atomic with the change
    it records). Use inside a docs/users write transaction."""
    conn.execute(
        "INSERT INTO audit_log(ts, actor, via, action, target, outcome, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (now_iso(), actor or "-", via, action, target, outcome, detail),
    )
    log.info("audit action=%s actor=%s via=%s target=%s outcome=%s",
             action, actor or "-", via, target, outcome)


def record_tx(db: Database, **kw) -> None:
    """Insert an audit row in its own short transaction (when no write txn is open)."""
    with db.writer() as conn:
        record(conn, **kw)


def recent(db: Database, *, limit: int = 100, since: str | None = None,
           until: str | None = None, actor: str | None = None, via: str | None = None,
           action: str | None = None, outcome: str | None = None,
           actions: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Most-recent audit rows (newest first), narrowed by any combination of:
    an ISO-8601 ``ts`` window (since/until), exact ``actor``/``via``/``action``/
    ``outcome``, or an ``actions`` whitelist (IN clause — e.g. DOC_ACTIONS to scope
    the feed to document activity)."""
    clauses: list[str] = []
    params: list = []
    for col, val in (("ts >= ?", since), ("ts <= ?", until), ("actor = ?", actor),
                     ("via = ?", via), ("action = ?", action), ("outcome = ?", outcome)):
        if val:
            clauses.append(col)
            params.append(val)
    if actions:
        ph = ",".join("?" * len(actions))
        clauses.append(f"action IN ({ph})")
        params.extend(actions)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(clamp_int(limit, 1, 1000))
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ts, actor, via, action, target, outcome, detail "
            f"FROM audit_log{where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
