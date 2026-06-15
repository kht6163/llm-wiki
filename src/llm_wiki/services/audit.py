"""Append-only audit trail for security-relevant events: document writes, auth
outcomes, API-key lifecycle, and account/role changes. Stored in the ``audit_log``
table so "who changed what, when, over which surface" is answerable.

Read-heavy MCP traffic is logged to the application logger instead (see
mcp_server), to avoid taking the writer lock on every read.
"""
from __future__ import annotations

import logging

from ..db import Database
from ..util import now_iso

log = logging.getLogger("llm_wiki.audit")


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


def recent(db: Database, limit: int = 100) -> list[dict]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT ts, actor, via, action, target, outcome, detail "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
    return [dict(r) for r in rows]
