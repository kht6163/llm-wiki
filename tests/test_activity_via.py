"""Authorship-surface (`via`) persistence on revisions and the audit activity feed.

`via` records which surface authored an edit — web (human), mcp (LLM agent), or cli
(external/reconcile) — so co-editing by humans and agents is legible in history and
the activity feed.
"""
from llm_wiki.services import audit
from llm_wiki.services.auth import Principal


def _p(principals, via: str) -> Principal:
    e = principals["editor"]
    return Principal(e.user_id, e.username, e.role, via=via)


def test_revision_records_authoring_surface(ctx, principals):
    docs = ctx.docs
    docs.create(_p(principals, "mcp"), "n.md", "# N\n\nv1")
    docs.update(_p(principals, "web"), "n.md", 1, "# N\n\nv2")

    by_ver = {r["version"]: r["via"] for r in docs.revisions("n.md")["revisions"]}
    assert by_ver == {1: "mcp", 2: "web"}
    # Single-revision read carries it too.
    assert docs.revision("n.md", 1)["via"] == "mcp"
    # The document read surfaces the LAST editor's surface.
    assert docs.get("n.md")["last_via"] == "web"


def test_list_docs_exposes_last_via(ctx, principals):
    docs = ctx.docs
    docs.create(_p(principals, "mcp"), "byagent.md", "x")
    docs.create(_p(principals, "web"), "byhuman.md", "y")
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
    docs.create(_p(principals, "mcp"), "a.md", "x")       # doc_create  via=mcp
    docs.update(_p(principals, "mcp"), "a.md", 1, "x2")   # doc_update  via=mcp
    docs.create(_p(principals, "web"), "b.md", "y")       # doc_create  via=web

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
    docs.create(_p(principals, "web"), "c.md", "z")
    scoped = audit.recent(ctx.db, actions=audit.DOC_ACTIONS)
    assert all(e["action"] != "login_failed" for e in scoped)
    # …but the unfiltered feed (admin view) still has it.
    assert any(e["action"] == "login_failed" for e in audit.recent(ctx.db))
