"""Append-only audit trail for security-relevant events: document writes, auth
outcomes, API-key lifecycle, and account/role changes. Stored in the ``audit_log``
table so "who changed what, when, over which surface" is answerable.

Read-heavy MCP traffic is logged to the application logger instead (see
mcp_server), to avoid taking the writer lock on every read.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..db import Database
from ..util import clamp_int, now_iso

log = logging.getLogger("llm_wiki.audit")

# Document/content activity — the subset of the audit trail that's safe and useful
# to surface to editors and LLM agents (the "what changed in the vault" feed).
# Security/account events (login*, key_*, user_*, role_change, password_change) are
# deliberately excluded here and only shown to admins via the unfiltered feed.
DOC_ACTIONS: tuple[str, ...] = (
    "doc_create", "doc_update", "doc_move", "doc_delete",
    "doc_restore", "doc_purge", "doc_reconcile", "attachment_upload",
)

_FIELD_LIMITS = {
    "actor": 128,
    "via": 32,
    "action": 64,
    "target": 4096,
    "outcome": 32,
    "detail": 4096,
}


def _bounded(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    text = str(value)[: _FIELD_LIMITS[field]]
    # Audit rows are also mirrored to the application log. Neutralize ASCII control
    # characters so attacker-controlled actor/target values cannot forge log lines.
    return "".join("?" if ord(char) < 32 or ord(char) == 127 else char for char in text)


def record(conn, *, actor: str | None, via: str, action: str,
           target: str | None = None, outcome: str = "ok", detail: str | None = None) -> None:
    """Insert one audit row on an EXISTING write connection (atomic with the change
    it records). Use inside a docs/users write transaction."""
    safe_actor = _bounded(actor or "-", "actor")
    safe_via = _bounded(via, "via")
    safe_action = _bounded(action, "action")
    safe_target = _bounded(target, "target")
    safe_outcome = _bounded(outcome, "outcome")
    safe_detail = _bounded(detail, "detail")
    conn.execute(
        "INSERT INTO audit_log(ts, actor, via, action, target, outcome, detail) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            now_iso(),
            safe_actor,
            safe_via,
            safe_action,
            safe_target,
            safe_outcome,
            safe_detail,
        ),
    )
    log.info("audit action=%s actor=%s via=%s target=%s outcome=%s",
             safe_action, safe_actor, safe_via, safe_target, safe_outcome)


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


def prune(db: Database, *, older_than_days: int, apply: bool) -> dict:
    """Delete audit_log rows older than ``older_than_days`` (0 = keep all). Returns a
    report; ``apply=False`` counts without deleting. Used by the ``prune`` CLI to bound
    the append-only log's growth. ``idx_audit_ts`` covers the ts range scan."""
    days = max(0, int(older_than_days))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.reader() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE ts < ?", (cutoff,)).fetchone()[0]
    if apply and count:
        with db.writer() as conn:
            conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        log.info("audit prune: deleted %d row(s) older than %dd (before %s)", count, days, cutoff)
    return {"cutoff": cutoff, "older_than_days": days,
            "deletable_events": count, "applied": bool(apply)}
