"""Authorship-surface (`via`) persistence on revisions and the audit activity feed.

`via` records which surface authored an edit — web (human), mcp (LLM agent), or cli
(external/reconcile) — so co-editing by humans and agents is legible in history and
the activity feed.
"""
import pytest

from llm_wiki.services import audit
from llm_wiki.services.auth import Principal, create_api_key, principal_from_api_key
from llm_wiki.services.errors import ConflictError, NotFoundError


def _p(ctx, principals, via: str) -> Principal:
    e = principals["editor"]
    if via == "mcp":
        token = create_api_key(ctx.db, e, "activity-via")
        principal = principal_from_api_key(ctx.db, token)
        assert principal is not None
        return principal
    return Principal(e.user_id, e.username, e.role, via=via)


def test_info_is_body_free_metadata(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "meta.md", "# Meta\n\nthe body text")
    info = docs.info("meta.md")
    assert info["version"] == 1 and info["last_via"] == "mcp" and info["title"] == "Meta"
    assert "content" not in info  # the whole point: no body load
    with pytest.raises(NotFoundError):
        docs.info("missing.md")


def test_conflict_envelope_carries_competing_surface(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "race.md", "v1")        # competing edit over web…
    docs.update(_p(ctx, principals, "web"), "race.md", 1, "v2")
    with pytest.raises(ConflictError) as ei:                   # …agent retries on stale base
        docs.update(_p(ctx, principals, "mcp"), "race.md", 1, "v2-from-agent")
    assert ei.value.extra["current_via"] == "web"             # so it knows a human edited
    assert ei.value.extra["current_version"] == 2


def test_revision_records_authoring_surface(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "n.md", "# N\n\nv1")
    docs.update(_p(ctx, principals, "web"), "n.md", 1, "# N\n\nv2")

    by_ver = {r["version"]: r["via"] for r in docs.revisions("n.md")["revisions"]}
    assert by_ver == {1: "mcp", 2: "web"}
    # Single-revision read carries it too.
    assert docs.revision("n.md", 1)["via"] == "mcp"
    # The document read surfaces the LAST editor's surface.
    assert docs.get("n.md")["last_via"] == "web"


def test_list_docs_exposes_last_via(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "byagent.md", "x")
    docs.create(_p(ctx, principals, "web"), "byhuman.md", "y")
    last = {d["path"]: d["last_via"] for d in docs.list_docs()}
    assert last["byagent.md"] == "mcp"
    assert last["byhuman.md"] == "web"


def test_external_reconcile_marks_cli(ctx, principals):
    docs = ctx.docs
    # A file appearing in the vault and reconciled in is attributed to 'cli'.
    vault = ctx.settings.vault_path
    (vault).mkdir(parents=True, exist_ok=True)
    (vault / "external.md").write_text("# Ext\n\nfrom disk", encoding="utf-8")
    docs.reindex_all()
    assert docs.revision("external.md", 1)["via"] == "cli"
    assert docs.get("external.md")["last_via"] == "cli"


def test_audit_recent_filters(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "a.md", "x")       # doc_create  via=mcp
    docs.update(_p(ctx, principals, "mcp"), "a.md", 1, "x2")   # doc_update  via=mcp
    docs.create(_p(ctx, principals, "web"), "b.md", "y")       # doc_create  via=web

    mcp_only = audit.recent(ctx.db, via="mcp")
    assert mcp_only and all(e["via"] == "mcp" for e in mcp_only)

    creates = audit.recent(ctx.db, action="doc_create")
    assert creates and all(e["action"] == "doc_create" for e in creates)

    # The DOC_ACTIONS whitelist never surfaces non-document events.
    docs_only = audit.recent(ctx.db, actions=audit.DOC_ACTIONS)
    assert docs_only and all(e["action"] in audit.DOC_ACTIONS for e in docs_only)


def test_audit_recent_excludes_security_events_from_doc_scope(ctx, principals):
    # Failed logins and key/role events must not leak through the document-scoped feed.
    audit.record_tx(ctx.db, actor="mallory", via="web", action="login_failed",
                    outcome="error")
    docs = ctx.docs
    docs.create(_p(ctx, principals, "web"), "c.md", "z")
    scoped = audit.recent(ctx.db, actions=audit.DOC_ACTIONS)
    assert all(e["action"] != "login_failed" for e in scoped)
    # …but the unfiltered feed (admin view) still has it.
    assert any(e["action"] == "login_failed" for e in audit.recent(ctx.db))


def test_audit_fields_are_defensively_bounded(ctx):
    audit.record_tx(
        ctx.db,
        actor="a" * 10_000,
        via="v" * 10_000,
        action="x" * 10_000,
        target="t" * 10_000,
        outcome="o" * 10_000,
        detail="d" * 20_000,
    )
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT length(actor),length(via),length(action),length(target),"
            "length(outcome),length(detail) FROM audit_log ORDER BY id DESC"
        ).fetchone()
    assert tuple(row) == (128, 32, 64, 4096, 32, 4096)


def test_via_counts_summary_by_surface(ctx, principals):
    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "a.md", "x")
    docs.update(_p(ctx, principals, "mcp"), "a.md", 1, "x2")
    docs.create(_p(ctx, principals, "web"), "b.md", "y")
    docs.create(_p(ctx, principals, "cli"), "c.md", "z")

    counts = audit.via_counts(ctx.db, actions=audit.DOC_ACTIONS)
    assert counts.get("mcp", 0) >= 2
    assert counts.get("web", 0) >= 1
    assert counts.get("cli", 0) >= 1
    assert sum(counts.values()) >= 4


def test_activity_page_shows_via_summary_and_highlights_mcp(ctx, principals):
    """Activity HTML surfaces via counts and marks agent-authored rows."""
    import re

    from starlette.testclient import TestClient

    from llm_wiki.web import create_web_app

    docs = ctx.docs
    docs.create(_p(ctx, principals, "mcp"), "agent.md", "from agent")
    docs.create(_p(ctx, principals, "web"), "human.md", "from human")

    client = TestClient(create_web_app(ctx))
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/login").text
    ).group(1)
    client.post(
        "/login",
        data={"username": "alice", "password": "secret12", "csrf_token": token},
    )
    html = client.get("/activity").text
    assert "via-summary" in html
    assert "에이전트" in html and "사람" in html
    assert "via-mcp-row" in html
    assert 'class="via-mcp-row"' in html or "via-mcp-row" in html
