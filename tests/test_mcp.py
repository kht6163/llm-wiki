"""MCP-layer tests: tool registration, Bearer-header parsing, the structured error
envelope, and end-to-end tool calls (auth gate, RBAC, conflict, rate limit) driven
through the real FastMCP tool wrappers."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.db import Database
from llm_wiki.mcp_server import _bearer_token, create_mcp_server
from llm_wiki.services.auth import create_api_key
from llm_wiki.services.errors import ForbiddenError


def _ctx(authorization):
    headers = {"authorization": authorization} if authorization is not None else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


def _payload(out):
    """call_tool returns a list of content blocks (or a (content, structured) tuple);
    the tool's dict is JSON in the first text block."""
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def test_bearer_token_parsing():
    assert _bearer_token(_ctx("Bearer abc123")) == "abc123"
    assert _bearer_token(_ctx("bearer abc123")) == "abc123"  # case-insensitive scheme
    assert _bearer_token(_ctx("rawtoken")) == "rawtoken"  # tolerate a bare token
    assert _bearer_token(_ctx(None)) is None
    no_request = SimpleNamespace(request_context=SimpleNamespace(request=None))
    assert _bearer_token(no_request) is None


async def test_tools_registered(ctx):
    mcp = create_mcp_server(ctx)
    names = {t.name for t in await mcp.list_tools()}
    expected = {
        "search_documents", "read_document", "get_document_info", "get_outline", "read_chunk",
        "list_documents",
        "list_recent_changes", "list_activity", "list_broken_links", "get_tags", "get_links",
        "get_backlinks", "resolve_links", "get_revisions", "get_revision", "compare_revisions",
        "get_graph",
        "assemble_context", "get_related_documents",
        "create_document", "update_document", "patch_document", "replace_section",
        "append_section", "append_to_document", "patch_tags", "move_document",
        "delete_document", "restore_revision", "rename_references", "edit_documents",
        "set_document_property", "remove_document_property",
        "list_folders", "create_folder", "delete_folder", "toggle_task", "export_corpus",
        "whoami",
        "set_document_properties",
        "get_or_create_daily_note", "list_trash", "restore_document", "purge_document",
        "list_favorites", "set_favorite", "upload_attachment", "rename_tag", "merge_tags",
    }
    assert expected <= names, names


def test_server_exposes_agent_instructions(ctx):
    # The initialize-time orientation must reach the FastMCP server so clients can
    # surface it to the model (primes vault conventions: locking, wikilinks, etc.).
    mcp = create_mcp_server(ctx)
    instr = mcp.instructions or ""
    assert "base_version" in instr and "[[wikilinks]]" in instr


async def test_whoami_reports_role_and_capabilities(editor_mcp):
    d = _payload(await editor_mcp.call_tool("whoami", {}))
    assert d["ok"] and d["username"] == "alice" and d["role"] == "editor"
    assert d["can_read"] is True and d["can_write"] is True and d["can_admin"] is False


def test_error_envelope_shape():
    d = ForbiddenError("nope").to_dict()
    assert d == {"ok": False, "error": {"code": "forbidden", "message": "nope",
                                        "suggested_action": "check_permissions"}}


def test_error_suggested_actions_per_code():
    from llm_wiki.services.errors import (
        ConflictError,
        EmbeddingUnavailableError,
        NotFoundError,
        RateLimitedError,
        ValidationError,
    )
    assert NotFoundError("x").to_dict()["error"]["suggested_action"] == "verify_path"
    assert ValidationError("x").to_dict()["error"]["suggested_action"] == "fix_request"
    assert ConflictError("x").to_dict()["error"]["suggested_action"] == "re_read_and_retry"
    assert RateLimitedError("x").to_dict()["error"]["suggested_action"] == "retry_after"
    unavailable = EmbeddingUnavailableError("x")
    assert unavailable.http_status == 503
    assert unavailable.to_dict()["error"] == {
        "code": "embedding_unavailable",
        "message": "x",
        "suggested_action": "restart_service",
    }
    # Per-instance extra coexists with the code-level hint.
    e = ConflictError("x", current_version=3).to_dict()["error"]
    assert e["suggested_action"] == "re_read_and_retry" and e["current_version"] == 3


# -- end-to-end tool calls -------------------------------------------------
@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"], "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


async def test_unauthorized_envelope(ctx, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: None)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("get_tags", {}))
    assert d["ok"] is False and d["error"]["code"] == "unauthorized"


def _audit_count(ctx, action):
    with ctx.db.reader() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action=?", (action,)).fetchone()[0]


def _audit_rows(ctx, *, action: str, target: str) -> list[dict]:
    with ctx.db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, via, action, target, outcome, detail FROM audit_log "
            "WHERE action=? AND target=? ORDER BY id",
            (action, target),
        ).fetchall()
    return [dict(row) for row in rows]


async def test_mcp_auth_failure_audited_once_at_threshold(ctx, monkeypatch):
    # A Bearer brute-force is persisted to the audit trail exactly once per window — on
    # the failure that crosses the limiter threshold (10) — so it surfaces in the admin
    # feed without taking the writer lock on every attempt.
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: "bad-key")
    mcp = create_mcp_server(ctx)
    for _ in range(9):
        await mcp.call_tool("get_tags", {})
    assert _audit_count(ctx, "mcp_auth_failed") == 0   # below threshold: app-log only
    await mcp.call_tool("get_tags", {})                # 10th crosses the threshold
    assert _audit_count(ctx, "mcp_auth_failed") == 1
    await mcp.call_tool("get_tags", {})                # already blocked: no re-amplification
    assert _audit_count(ctx, "mcp_auth_failed") == 1


async def test_search_rejects_empty_query(editor_mcp):
    # An empty/whitespace query is a client error, not a successful 0-result search,
    # so an agent can tell "no matches" from "bad query".
    d = _payload(await editor_mcp.call_tool("search_documents", {"query": "   "}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_search_path_operator_and_triage_metadata(editor_mcp):
    # An agent narrows by path in ONE call (no post-filtering) and reads content_length/
    # section_depth off each hit to triage which to open without a follow-up read.
    await editor_mcp.call_tool("create_document", {
        "path": "guide/intro.md", "content": "# Intro Guide\n\n## Setup\n\nwidget install steps " + "x " * 40})
    await editor_mcp.call_tool("create_document", {
        "path": "notes/misc.md", "content": "# Misc\n\nwidget mention only"})
    d = _payload(await editor_mcp.call_tool(
        "search_documents", {"query": "widget path:guide/*", "mode": "bm25"}))
    assert d["ok"]
    paths = {r["path"] for r in d["results"]}
    assert "guide/intro.md" in paths and "notes/misc.md" not in paths
    hit = next(r for r in d["results"] if r["path"] == "guide/intro.md")
    assert isinstance(hit["content_length"], int) and hit["content_length"] > 0
    assert hit["section_depth"] is None or hit["section_depth"] >= 1


async def test_search_operator_only_query_is_validation(editor_mcp):
    await editor_mcp.call_tool("create_document", {"path": "x.md", "content": "# X\n\nbody"})
    d = _payload(await editor_mcp.call_tool("search_documents", {"query": "title:X"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_search_unknown_has_is_validation(editor_mcp):
    d = _payload(await editor_mcp.call_tool("search_documents", {"query": "widget has:bogus"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_search_exposes_char_range_and_context_preview(editor_mcp):
    # An agent can deep-link to the matched span and judge relevance from the preview
    # without a read_chunk round-trip.
    await editor_mcp.call_tool("create_document", {
        "path": "cp.md", "content": "# Doc\n\n## Section\n\nwidget alpha beta gamma delta epsilon zeta"})
    d = _payload(await editor_mcp.call_tool(
        "search_documents", {"query": "widget", "mode": "bm25"}))
    hit = next(r for r in d["results"] if r["path"] == "cp.md")
    assert isinstance(hit["char_start"], int) and isinstance(hit["char_end"], int)
    assert hit["char_end"] > hit["char_start"] >= 0
    assert hit["context_preview"] and "widget" in hit["context_preview"]
    assert "<mark>" not in hit["context_preview"]  # plain prose, not the FTS snippet


async def test_error_envelope_carries_suggested_action(editor_mcp):
    # not_found -> verify_path; validation -> fix_request. Agents branch on the token
    # without parsing the human message.
    nf = _payload(await editor_mcp.call_tool("read_document", {"path": "ghost.md"}))
    assert nf["error"]["code"] == "not_found" and nf["error"]["suggested_action"] == "verify_path"
    val = _payload(await editor_mcp.call_tool("search_documents", {"query": "   "}))
    assert val["error"]["code"] == "validation" and val["error"]["suggested_action"] == "fix_request"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_documents", {"query": "embedding generation", "mode": "vector"}),
        ("assemble_context", {"question": "embedding generation", "mode": "vector"}),
        ("get_related_documents", {"path": "stale.md", "limit": 5}),
    ],
)
async def test_stale_embedding_tools_return_stable_unavailable_error(
    editor_mcp, ctx, principals, tool, arguments
):
    ctx.docs.create(
        principals["editor"], "stale.md", "# Stale\n\nembedding generation"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    other.rebind_model(
        "test/different-same-dimension-model",
        ctx.embedder.dim,
        ctx.embedder.pipeline,
    )

    result = _payload(await editor_mcp.call_tool(tool, arguments))

    assert result["ok"] is False
    assert result["error"]["code"] == "embedding_unavailable"
    assert result["error"]["suggested_action"] == "restart_service"


async def test_create_then_read_roundtrip(editor_mcp):
    created = _payload(await editor_mcp.call_tool("create_document", {"path": "m.md", "content": "# M\n\nbody"}))
    assert created["ok"] and created["version"] == 1
    read = _payload(await editor_mcp.call_tool("read_document", {"path": "m.md"}))
    assert read["ok"] and "body" in read["content"]
    outline = _payload(await editor_mcp.call_tool("get_outline", {"path": "m.md"}))
    assert any(h["text"] == "M" for h in outline["headings"])


async def test_list_activity_reports_agent_surface(editor_mcp):
    # An agent's own write shows up in the activity feed attributed to via='mcp'.
    _payload(await editor_mcp.call_tool("create_document", {"path": "act.md", "content": "# A\n\nbody"}))
    d = _payload(await editor_mcp.call_tool("list_activity", {}))
    assert d["ok"] and d["count"] >= 1
    created = [e for e in d["events"] if e["action"] == "doc_create" and e["target"] == "act.md"]
    assert created and created[0]["via"] == "mcp"
    # Only document actions are exposed (never login/key/role events).
    assert all(e["action"] in d["actions"] for e in d["events"])


async def test_list_activity_rejects_non_document_action(editor_mcp):
    d = _payload(await editor_mcp.call_tool("list_activity", {"action": "login_failed"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_append_to_document_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "log.md", "content": "# Log\n\na\n"}))
    d = _payload(await editor_mcp.call_tool(
        "append_to_document", {"path": "log.md", "text": "b", "return_content": "full"}))
    assert d["ok"] and d["content"].rstrip().endswith("b")


async def test_append_to_document_idempotency_key_dedups(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "log.md", "content": "# Log\n"}))
    first = _payload(await editor_mcp.call_tool(
        "append_to_document", {"path": "log.md", "text": "entry", "idempotency_key": "k1"}))
    assert first["ok"] and not first.get("deduplicated")
    # A retry with the same key returns the prior result without appending again.
    again = _payload(await editor_mcp.call_tool(
        "append_to_document", {"path": "log.md", "text": "entry", "idempotency_key": "k1"}))
    assert again["ok"] and again["deduplicated"] is True
    assert again["version"] == first["version"]
    body = _payload(await editor_mcp.call_tool("read_document", {"path": "log.md"}))["content"]
    assert body.count("entry") == 1


async def test_patch_regex_occurrence_tool(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "r.md", "content": "- [ ] a\n- [ ] b\n"}))
    d = _payload(await editor_mcp.call_tool(
        "patch_document",
        {"path": "r.md", "find": r"^- \[ \]", "replace": "- [x]", "mode": "regex",
         "occurrence": 2, "return_content": "full"}))
    assert d["ok"] and d["content"] == "- [ ] a\n- [x] b\n"


async def test_set_and_remove_property_tools(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "p.md", "content": "# P\n\nbody"}))
    d = _payload(await editor_mcp.call_tool(
        "set_document_property",
        {"path": "p.md", "key": "aliases", "value": ["별명1", "별명2"], "return_content": "full"}))
    assert d["ok"] and "aliases: [별명1, 별명2]" in d["content"]
    # remove it again
    r = _payload(await editor_mcp.call_tool("remove_document_property",
                                            {"path": "p.md", "key": "aliases", "return_content": "full"}))
    assert r["ok"] and "aliases" not in r["content"]


async def test_set_property_rejects_reserved_key(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "p.md", "content": "# P\n\nbody"}))
    d = _payload(await editor_mcp.call_tool(
        "set_document_property", {"path": "p.md", "key": "title", "value": "x"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_compare_revisions_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "cmp.md", "content": "a\nb\n"}))
    _payload(await editor_mcp.call_tool(
        "update_document", {"path": "cmp.md", "base_version": 1, "content": "a\nB\nc\n"}))
    d = _payload(await editor_mcp.call_tool(
        "compare_revisions", {"path": "cmp.md", "from_version": 1, "to_version": 2}))
    assert d["ok"] and d["from_version"] == 1 and d["to_version"] == 2
    classes = {ln["cls"] for ln in d["diff"]}
    assert "add" in classes and "del" in classes
    assert d["summary"]["lines_added"] >= 1 and d["summary"]["lines_deleted"] >= 1


async def test_restore_revision_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "rr.md", "content": "first"}))
    _payload(await editor_mcp.call_tool(
        "update_document", {"path": "rr.md", "base_version": 1, "content": "second"}))
    d = _payload(await editor_mcp.call_tool(
        "restore_revision", {"path": "rr.md", "version": 1, "return_content": "full"}))
    assert d["ok"] and d["content"] == "first" and d["version"] == 3


async def test_resolve_links_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "exists.md", "content": "x"}))
    d = _payload(await editor_mcp.call_tool(
        "resolve_links", {"targets": ["exists", "ghosttarget"]}))
    assert d["ok"]
    assert d["resolved"]["exists"] == "exists.md"
    assert d["resolved"]["ghosttarget"] is None
    assert d["unresolved"] == ["ghosttarget"]


async def test_edit_documents_batch_applies_all(editor_mcp):
    ops = [
        {"op": "create", "path": "m1.md", "content": "# M1\n\na"},
        {"op": "create", "path": "m2.md", "content": "# M2\n\nb"},
        {"op": "append", "path": "m1.md", "text": "more"},
    ]
    d = _payload(await editor_mcp.call_tool("edit_documents", {"operations": ops}))
    assert d["ok"] and d["applied"] == 3 and d["failed"] == 0
    assert all(r["ok"] for r in d["results"])


async def test_edit_documents_stop_on_error(editor_mcp):
    # Second op fails (update with a stale base_version); stop_on_error halts the rest.
    ops = [
        {"op": "create", "path": "b1.md", "content": "one"},
        {"op": "update", "path": "b1.md", "base_version": 0, "content": "two"},  # conflict
        {"op": "create", "path": "b2.md", "content": "never"},
    ]
    d = _payload(await editor_mcp.call_tool("edit_documents", {"operations": ops}))
    assert d["applied"] == 1 and d["failed"] == 1 and d["stopped_early"] is True
    assert d["results"][1]["error"]["code"] == "conflict"
    # Batch sweeps trim each conflict's body by default (headline token win); the op's
    # decision fields remain, current_content is dropped for current_chars.
    assert "current_content" not in d["results"][1]["error"]
    assert d["results"][1]["error"]["content_omitted"] is True


async def test_edit_documents_best_effort(editor_mcp):
    ops = [
        {"op": "create", "path": "k1.md", "content": "x"},
        {"op": "frobnicate", "path": "k1.md"},                  # unknown op -> validation
        {"op": "create", "path": "k2.md", "content": "y"},
    ]
    d = _payload(await editor_mcp.call_tool("edit_documents", {"operations": ops, "stop_on_error": False}))
    assert d["applied"] == 2 and d["failed"] == 1 and d["stopped_early"] is False


async def test_rename_references_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "t/old.md", "content": "x"}))
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "r.md", "content": "see [link](t/old.md)"}))
    _payload(await editor_mcp.call_tool("move_document", {"path": "t/old.md", "new_path": "t/new.md"}))
    d = _payload(await editor_mcp.call_tool(
        "rename_references", {"old_path": "t/old.md", "new_path": "t/new.md"}))
    assert d["ok"] and d["docs_rewritten"] == 1
    read = _payload(await editor_mcp.call_tool("read_document", {"path": "r.md"}))
    assert "t/new.md" in read["content"]


async def test_viewer_write_forbidden(ctx, principals, monkeypatch):
    vkey = create_api_key(ctx.db, principals["viewer"], "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("create_document", {"path": "x.md", "content": "nope"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_viewer_cannot_list_activity_or_trash(ctx, principals, monkeypatch):
    vkey = create_api_key(ctx.db, principals["viewer"], "viewer-read-key")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)

    activity = _payload(await mcp.call_tool("list_activity", {}))
    trash = _payload(await mcp.call_tool("list_trash", {}))

    assert activity["ok"] is False and activity["error"]["code"] == "forbidden"
    assert trash["ok"] is False and trash["error"]["code"] == "forbidden"


async def test_mcp_write_failures_are_audited_without_sensitive_data(
    ctx, principals, monkeypatch,
):
    attempted_body = "TOP-SECRET-ATTEMPT"
    viewer_key = create_api_key(ctx.db, principals["viewer"], "viewer-write-key")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: viewer_key)
    viewer_mcp = create_mcp_server(ctx)

    forbidden = _payload(await viewer_mcp.call_tool(
        "create_document", {"path": "private.md", "content": attempted_body}))
    assert forbidden["ok"] is False and forbidden["error"]["code"] == "forbidden"
    forbidden_rows = _audit_rows(ctx, action="doc_create", target="private.md")
    assert forbidden_rows == [{
        "actor": "bob",
        "via": "mcp",
        "action": "doc_create",
        "target": "private.md",
        "outcome": "forbidden",
        "detail": None,
    }]
    assert attempted_body not in json.dumps(forbidden_rows)
    assert viewer_key not in json.dumps(forbidden_rows)

    editor_key = create_api_key(ctx.db, principals["editor"], "editor-write-key")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: editor_key)
    editor_mcp = create_mcp_server(ctx)
    current_body = "TOP-SECRET-CURRENT"
    created = _payload(await editor_mcp.call_tool(
        "create_document", {"path": "confidential.md", "content": current_body}))
    assert created["ok"] is True

    conflict = _payload(await editor_mcp.call_tool("update_document", {
        "path": "confidential.md", "base_version": 0, "content": attempted_body,
    }))
    assert conflict["ok"] is False and conflict["error"]["code"] == "conflict"
    conflict_rows = _audit_rows(ctx, action="doc_update", target="confidential.md")
    assert conflict_rows == [{
        "actor": "alice",
        "via": "mcp",
        "action": "doc_update",
        "target": "confidential.md",
        "outcome": "conflict",
        "detail": None,
    }]
    serialized = json.dumps(conflict_rows)
    assert attempted_body not in serialized
    assert current_body not in serialized
    assert editor_key not in serialized

    updated = _payload(await editor_mcp.call_tool("update_document", {
        "path": "confidential.md", "base_version": 1, "content": "safe replacement",
    }))
    assert updated["ok"] is True
    assert [row["outcome"] for row in _audit_rows(
        ctx, action="doc_update", target="confidential.md",
    )] == ["conflict", "ok"]


async def test_mcp_validation_and_not_found_write_failures_are_audited(
    ctx, principals, monkeypatch,
):
    editor_key = create_api_key(ctx.db, principals["editor"], "rejected-writes")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: editor_key)
    mcp = create_mcp_server(ctx)

    validation = _payload(await mcp.call_tool("patch_document", {
        "path": "validation.md", "find": "", "replace": "ignored",
    }))
    missing = _payload(await mcp.call_tool("update_document", {
        "path": "missing.md", "base_version": 1, "content": "ignored",
    }))

    assert validation["ok"] is False and validation["error"]["code"] == "validation"
    assert missing["ok"] is False and missing["error"]["code"] == "not_found"
    assert _audit_rows(ctx, action="doc_update", target="validation.md") == [{
        "actor": "alice", "via": "mcp", "action": "doc_update",
        "target": "validation.md", "outcome": "validation", "detail": None,
    }]
    assert _audit_rows(ctx, action="doc_update", target="missing.md") == [{
        "actor": "alice", "via": "mcp", "action": "doc_update",
        "target": "missing.md", "outcome": "not_found", "detail": None,
    }]


async def test_default_daily_note_failure_audits_resolved_utc_date(
    ctx, principals, monkeypatch,
):
    fixed_now = datetime(2035, 2, 3, 0, 0, tzinfo=UTC)

    class FixedDatetime:
        @classmethod
        def now(cls, tz):
            assert tz is UTC
            return fixed_now

    monkeypatch.setattr(mcp_mod, "datetime", FixedDatetime)
    seen_dates = []
    original_daily_note = ctx.docs.daily_note

    def capture_daily_note(principal, date=None, *, folder="daily"):
        seen_dates.append(date)
        return original_daily_note(principal, date, folder=folder)

    monkeypatch.setattr(ctx.docs, "daily_note", capture_daily_note)
    viewer_key = create_api_key(ctx.db, principals["viewer"], "default-daily")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: viewer_key)
    mcp = create_mcp_server(ctx)

    result = _payload(await mcp.call_tool("get_or_create_daily_note", {}))

    assert result["ok"] is False and result["error"]["code"] == "forbidden"
    assert seen_dates == ["2035-02-03"]
    assert _audit_rows(ctx, action="doc_create", target="daily/2035-02-03.md") == [{
        "actor": "bob", "via": "mcp", "action": "doc_create",
        "target": "daily/2035-02-03.md", "outcome": "forbidden", "detail": None,
    }]


async def test_all_rejected_mcp_write_tools_use_safe_audit_metadata(
    ctx, principals, monkeypatch,
):
    secret = "DO-NOT-AUDIT-THIS-PAYLOAD"
    viewer_key = create_api_key(ctx.db, principals["viewer"], "viewer-all-writes")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: viewer_key)
    mcp = create_mcp_server(ctx)
    cases = [
        ("patch_document", {"path": "patch.md", "find": "old", "replace": secret},
         "doc_update", "patch.md"),
        ("append_to_document", {"path": "append.md", "text": secret},
         "doc_update", "append.md"),
        ("restore_revision", {"path": "revision.md", "version": 1},
         "doc_update", "revision.md"),
        ("replace_section", {"path": "replace.md", "heading": "H", "text": secret},
         "doc_update", "replace.md"),
        ("append_section", {"path": "section.md", "heading": "H", "text": secret},
         "doc_update", "section.md"),
        ("patch_tags", {"path": "tags.md", "add": ["private"]},
         "doc_update", "tags.md"),
        ("set_document_property", {"path": "property.md", "key": "status", "value": secret},
         "doc_update", "property.md"),
        ("remove_document_property", {"path": "remove-property.md", "key": "status"},
         "doc_update", "remove-property.md"),
        ("set_document_properties", {"path": "properties.md", "properties": {"status": secret}},
         "doc_update", "properties.md"),
        ("toggle_task", {"path": "task.md", "index": 0},
         "doc_update", "task.md"),
        ("get_or_create_daily_note", {"date": "2026-07-12", "folder": "daily"},
         "doc_create", "daily/2026-07-12.md"),
        ("move_document", {"path": "old.md", "new_path": "new.md"},
         "doc_move", "old.md -> new.md"),
        ("rename_references", {"old_path": "before.md", "new_path": "after.md"},
         "doc_update", "before.md -> after.md"),
        ("delete_document", {"path": "delete.md"},
         "doc_delete", "delete.md"),
        ("restore_document", {"path": "restore.md"},
         "doc_restore", "restore.md"),
        ("purge_document", {"path": "purge.md"},
         "doc_purge", "purge.md"),
        ("rename_tag", {"old": "private", "new": "public"},
         "doc_update", "tag:private -> public"),
        ("merge_tags", {"sources": ["one", "two"], "dest": "merged"},
         "doc_update", "tags:one,two -> merged"),
        ("create_folder", {"path": "new-folder"},
         "folder_create", "new-folder"),
        ("delete_folder", {"path": "old-folder"},
         "folder_delete", "old-folder"),
        ("upload_attachment", {"filename": "private.pdf", "content_base64": "eA=="},
         "attachment_upload", "private.pdf"),
    ]

    for tool, arguments, action, target in cases:
        result = _payload(await mcp.call_tool(tool, arguments))
        assert result["ok"] is False and result["error"]["code"] == "forbidden", tool
        assert _audit_rows(ctx, action=action, target=target) == [{
            "actor": "bob",
            "via": "mcp",
            "action": action,
            "target": target,
            "outcome": "forbidden",
            "detail": None,
        }], tool

    serialized = json.dumps(_audit_rows(ctx, action="doc_update", target="patch.md"))
    assert secret not in serialized
    assert viewer_key not in serialized


async def test_edit_documents_audits_per_operation_failures(ctx, principals, monkeypatch):
    secret = "BATCH-SECRET-CONTENT"
    editor_key = create_api_key(ctx.db, principals["editor"], "batch-editor")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: editor_key)
    mcp = create_mcp_server(ctx)
    assert _payload(await mcp.call_tool(
        "create_document", {"path": "batch-existing.md", "content": "current"}))[
            "ok"] is True
    assert _payload(await mcp.call_tool(
        "create_document", {"path": "batch-update.md", "content": "current"}))[
            "ok"] is True

    result = _payload(await mcp.call_tool("edit_documents", {
        "operations": [
            {"op": "create", "path": "batch-existing.md", "content": secret},
            {"op": "update", "path": "batch-update.md", "base_version": 0,
             "content": secret},
        ],
        "stop_on_error": False,
    }))
    assert result["ok"] is True and result["failed"] == 2
    create_rows = _audit_rows(ctx, action="doc_create", target="batch-existing.md")
    update_rows = _audit_rows(ctx, action="doc_update", target="batch-update.md")
    assert [row["outcome"] for row in create_rows] == ["ok", "conflict"]
    assert [row["outcome"] for row in update_rows] == ["conflict"]
    assert create_rows[-1]["detail"] is None and update_rows[-1]["detail"] is None

    preview = _payload(await mcp.call_tool("edit_documents", {
        "operations": [{
            "op": "update", "path": "batch-update.md", "base_version": 0,
            "content": secret,
        }],
        "dry_run": True,
    }))
    assert preview["ok"] is True
    assert preview["results"][0]["ok"] is False
    assert preview["results"][0]["error"]["code"] == "conflict"
    assert [row["outcome"] for row in _audit_rows(
        ctx, action="doc_update", target="batch-update.md",
    )] == ["conflict"]

    viewer_key = create_api_key(ctx.db, principals["viewer"], "batch-viewer")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: viewer_key)
    viewer_mcp = create_mcp_server(ctx)
    forbidden = _payload(await viewer_mcp.call_tool("edit_documents", {
        "operations": [{"op": "create", "path": "batch-private.md", "content": secret}],
    }))
    assert forbidden["ok"] is False and forbidden["error"]["code"] == "forbidden"
    assert _audit_rows(ctx, action="doc_create", target="batch-private.md") == [{
        "actor": "bob",
        "via": "mcp",
        "action": "doc_create",
        "target": "batch-private.md",
        "outcome": "forbidden",
        "detail": None,
    }]

    empty = _payload(await viewer_mcp.call_tool("edit_documents", {"operations": []}))
    oversized = _payload(await viewer_mcp.call_tool("edit_documents", {
        "operations": [
            {"op": "create", "path": f"too-many-{index}.md", "content": secret}
            for index in range(101)
        ],
    }))
    assert empty["ok"] is False and empty["error"]["code"] == "forbidden"
    assert oversized["ok"] is False and oversized["error"]["code"] == "forbidden"

    max_batch = _payload(await viewer_mcp.call_tool("edit_documents", {
        "operations": [
            {"op": "create", "path": f"max-batch-{index}.md", "content": secret}
            for index in range(100)
        ],
    }))
    assert max_batch["ok"] is False and max_batch["error"]["code"] == "forbidden"
    assert _audit_rows(
        ctx,
        action="batch_write",
        target="count=100 first=max-batch-0.md",
    ) == [{
        "actor": "bob",
        "via": "mcp",
        "action": "batch_write",
        "target": "count=100 first=max-batch-0.md",
        "outcome": "forbidden",
        "detail": None,
    }]
    with ctx.db.reader() as conn:
        expanded_rows = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE actor='bob' "
            "AND target LIKE 'max-batch-%'"
        ).fetchone()[0]
    assert expanded_rows == 0

    preview = _payload(await viewer_mcp.call_tool("edit_documents", {
        "operations": [{"op": "create", "path": "preview-only.md", "content": secret}],
        "dry_run": True,
    }))
    assert preview["ok"] is False and preview["error"]["code"] == "forbidden"
    assert _audit_rows(ctx, action="doc_create", target="preview-only.md") == []

    serialized = json.dumps(create_rows + update_rows + _audit_rows(
        ctx, action="doc_create", target="batch-private.md",
    ))
    assert secret not in serialized
    assert editor_key not in serialized
    assert viewer_key not in serialized


async def test_get_document_info_metadata_only(editor_mcp):
    # Cheap poll: returns version/last_via WITHOUT the body, so an agent can detect a
    # change since the version it holds before re-downloading the full note.
    _payload(await editor_mcp.call_tool("create_document", {"path": "i.md", "content": "# I\n\nthe body"}))
    d = _payload(await editor_mcp.call_tool("get_document_info", {"path": "i.md"}))
    assert d["ok"] and d["version"] == 1 and d["last_via"] == "mcp"
    assert "content" not in d  # body deliberately omitted
    miss = _payload(await editor_mcp.call_tool("get_document_info", {"path": "nope.md"}))
    assert miss["ok"] is False and miss["error"]["code"] == "not_found"


async def test_list_activity_actor_filter(editor_mcp):
    # actor filter lets an agent scope the feed to (or away from) one editor.
    _payload(await editor_mcp.call_tool("create_document", {"path": "af.md", "content": "# A\n\nx"}))
    mine = _payload(await editor_mcp.call_tool("list_activity", {"actor": "alice"}))
    assert mine["ok"] and mine["actor"] == "alice" and mine["count"] >= 1
    assert all(e["actor"] == "alice" for e in mine["events"])
    other = _payload(await editor_mcp.call_tool("list_activity", {"actor": "nobody-else"}))
    assert other["ok"] and other["count"] == 0


async def test_update_conflict_envelope(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "c.md", "content": "v1"}))
    d = _payload(await editor_mcp.call_tool(
        "update_document", {"path": "c.md", "base_version": 0, "content": "v2"}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"
    assert d["error"]["current_version"] == 1
    # The envelope names the COMPETING edit's surface so an agent can choose to back
    # off (human/web) vs rebase (agent/mcp). The create above came over mcp.
    assert d["error"]["current_via"] == "mcp"
    # Default (metadata) omits the competing body to save agent tokens; current_chars +
    # content_omitted replace current_content, while the decision fields stay.
    assert "current_content" not in d["error"]
    assert d["error"]["content_omitted"] is True
    assert isinstance(d["error"]["current_chars"], int)


async def test_update_conflict_full_includes_body(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "cf.md", "content": "v1 body"}))
    d = _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "cf.md", "base_version": 0, "content": "v2", "return_content": "full"}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"
    assert "current_content" in d["error"] and "content_omitted" not in d["error"]
    # The recovery hint rides alongside the conflict's decision context (current_version etc.).
    assert d["error"]["suggested_action"] == "re_read_and_retry"
    assert d["error"]["current_version"] == 1


async def test_section_base_version_conflict_via_tool(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "s.md", "content": "# T\n\n## A\nold\n"}))
    # stale base_version (0) -> conflict; the section edit funnels through CAS.
    d = _payload(await editor_mcp.call_tool(
        "replace_section", {"path": "s.md", "heading": "A", "text": "new", "base_version": 0}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"


async def test_write_tools_e2e(editor_mcp):
    # The write tools an agent uses most — none had behavioral coverage before.
    mcp = editor_mcp
    _payload(await mcp.call_tool("create_document", {"path": "w.md", "content": "# W\n\n## A\nalpha\n"}))
    pd = _payload(await mcp.call_tool(
        "patch_document", {"path": "w.md", "find": "alpha", "replace": "beta", "return_content": "full"}))
    assert pd["ok"] and "beta" in pd["content"]
    ap = _payload(await mcp.call_tool(
        "append_section", {"path": "w.md", "heading": "A", "text": "gamma", "return_content": "full"}))
    assert ap["ok"] and "gamma" in ap["content"]
    pt = _payload(await mcp.call_tool("patch_tags", {"path": "w.md", "add": ["t1", "t2"]}))
    assert pt["ok"] and {"t1", "t2"} <= set(pt["tags"])
    mv = _payload(await mcp.call_tool("move_document", {"path": "w.md", "new_path": "moved/w.md"}))
    assert mv["ok"] and mv["path"] == "moved/w.md"
    dl = _payload(await mcp.call_tool("delete_document", {"path": "moved/w.md"}))
    assert dl["ok"] and dl["deleted"] is True


async def test_write_tools_omit_body_by_default(editor_mcp):
    # Write tools return metadata only by default (token-cheap for agents); the body is
    # echoed only when return_content='full'.
    _payload(await editor_mcp.call_tool("create_document", {"path": "rc.md", "content": "# RC\n\nbody here"}))
    upd = _payload(await editor_mcp.call_tool(
        "update_document", {"path": "rc.md", "base_version": 1, "content": "# RC\n\nnew body"}))
    assert upd["ok"] and "content" not in upd
    assert upd["content_omitted"] is True and upd["chars"] == len("# RC\n\nnew body")
    full = _payload(await editor_mcp.call_tool(
        "update_document", {"path": "rc.md", "base_version": 2, "content": "# RC\n\nfinal",
                            "return_content": "full"}))
    assert full["ok"] and full["content"] == "# RC\n\nfinal"


async def test_replace_section_occurrence_targets_nth(editor_mcp):
    # Repeated headings are disambiguated by 'occurrence' instead of silently hitting #1.
    body = "# Doc\n\n## 예시\nfirst\n\n## 예시\nsecond\n"
    _payload(await editor_mcp.call_tool("create_document", {"path": "dup.md", "content": body}))
    _payload(await editor_mcp.call_tool(
        "replace_section", {"path": "dup.md", "heading": "예시", "text": "SECOND", "occurrence": 2}))
    read = _payload(await editor_mcp.call_tool("read_document", {"path": "dup.md"}))
    # First "예시" untouched; only the second was replaced.
    assert "first" in read["content"] and "SECOND" in read["content"] and "second" not in read["content"]


async def test_section_occurrence_out_of_range_is_validation(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "one.md", "content": "# D\n\n## A\nx\n"}))
    d = _payload(await editor_mcp.call_tool(
        "replace_section", {"path": "one.md", "heading": "A", "text": "y", "occurrence": 3}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_read_tools_e2e(editor_mcp):
    mcp = editor_mcp
    _payload(await mcp.call_tool(
        "create_document", {"path": "notes/a.md", "content": "# A\n\nhello [[b]]", "tags": ["x"]}))
    _payload(await mcp.call_tool("create_document", {"path": "notes/b.md", "content": "# B\n\nworld"}))
    ld = _payload(await mcp.call_tool("list_documents", {"folder": "notes"}))
    assert ld["ok"] and ld["total"] >= 2 and ld["count"] >= 2 and ld["has_more"] is False
    sd = _payload(await mcp.call_tool("search_documents", {"query": "hello", "mode": "bm25"}))
    assert sd["ok"] and any(r["path"] == "notes/a.md" for r in sd["results"])
    bl = _payload(await mcp.call_tool("get_backlinks", {"path": "notes/b.md"}))
    assert bl["ok"] and any(x["src_path"] == "notes/a.md" for x in bl["backlinks"])
    assert _payload(await mcp.call_tool("get_links", {"path": "notes/a.md"}))["ok"]
    gg = _payload(await mcp.call_tool("get_graph", {}))
    assert gg["ok"] and gg["nodes"]
    rc = _payload(await mcp.call_tool("list_recent_changes", {"limit": 5}))
    assert rc["ok"] and "count" in rc and "has_more" in rc
    rv = _payload(await mcp.call_tool("get_revisions", {"path": "notes/a.md"}))
    assert rv["ok"] and rv["revisions"]
    one = _payload(await mcp.call_tool("get_revision", {"path": "notes/a.md", "version": 1}))
    assert one["ok"] and "content" in one


async def test_read_chunk_tool(editor_mcp):
    # Chunk-addressable read: pull one matched passage (plus neighbours) instead of
    # the whole body. Sections are padded so chunk_markdown splits them apart.
    mcp = editor_mcp
    body = ("# Doc\n\n## Alpha\n\n" + "alpha " * 80 + "\n\n## Beta\n\n" + "beta " * 80
            + "\n\n## Gamma\n\n" + "gamma " * 80)
    _payload(await mcp.call_tool("create_document", {"path": "ch.md", "content": body}))

    c0 = _payload(await mcp.call_tool("read_chunk", {"path": "ch.md", "ordinal": 0}))
    assert c0["ok"] and c0["ordinal"] == 0 and c0["chunk_count"] >= 1
    assert c0["has_before"] is False
    assert c0["chunks"][0]["char_start"] == c0["char_start"]

    # A wide 'after' window pulls every chunk; the joined window reaches the end.
    full = _payload(await mcp.call_tool("read_chunk", {"path": "ch.md", "ordinal": 0, "after": 20}))
    assert full["ok"] and len(full["chunks"]) == full["chunk_count"]
    assert full["has_after"] is False

    miss = _payload(await mcp.call_tool("read_chunk", {"path": "ch.md", "ordinal": 999}))
    assert miss["ok"] is False and miss["error"]["code"] == "not_found"


async def test_search_hit_exposes_chunk_address(editor_mcp):
    # Every hit carries chunk_ordinal/chunk_id keys so an agent can hand them to
    # read_chunk; they are None for a BM25-only match (no per-chunk vector rank).
    mcp = editor_mcp
    _payload(await mcp.call_tool(
        "create_document", {"path": "addr.md", "content": "# Addr\n\n" + "needle " * 60}))
    sd = _payload(await mcp.call_tool("search_documents", {"query": "needle", "mode": "bm25"}))
    hit = next(r for r in sd["results"] if r["path"] == "addr.md")
    assert "chunk_ordinal" in hit and "chunk_id" in hit


async def test_get_related_documents_tool(editor_mcp):
    mcp = editor_mcp
    _payload(await mcp.call_tool("create_document", {
        "path": "ml.md", "content": "# ML\n\nneural networks and deep learning on data"}))
    _payload(await mcp.call_tool("create_document", {
        "path": "ai.md", "content": "# AI\n\ndeep learning and neural networks power AI"}))
    _payload(await mcp.call_tool("create_document", {
        "path": "cook.md", "content": "# Cook\n\nbake sourdough bread in a home oven"}))
    rel = _payload(await mcp.call_tool("get_related_documents", {"path": "ml.md", "limit": 5}))
    assert rel["ok"] and rel["path"] == "ml.md"
    paths = [r["path"] for r in rel["related"]]
    assert "ml.md" not in paths and "ai.md" in paths  # self excluded, neighbor surfaced


async def test_assemble_context_tool(editor_mcp):
    mcp = editor_mcp
    _payload(await mcp.call_tool("create_document", {
        "path": "geo.md", "content": "# Geo\n\nRivers carry water from mountains to the sea."}))
    res = _payload(await mcp.call_tool(
        "assemble_context", {"question": "where does river water go", "max_sources": 3}))
    assert res["ok"] and res["count"] >= 1
    assert res["sources"][0]["path"] == "geo.md"
    assert res["context"].startswith("[1] geo.md")
    assert "truncated" in res and "char_count" in res


async def test_assemble_context_rejects_empty_question(editor_mcp):
    d = _payload(await editor_mcp.call_tool("assemble_context", {"question": "  "}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_internal_error_returns_structured_envelope(ctx, principals, monkeypatch):
    # A non-WikiError raised inside a tool body must still reach the agent as the
    # structured {ok:false, error:{code:"internal"}} envelope, not a raw protocol
    # error, and must not leak internals.
    key = create_api_key(ctx.db, principals["editor"], "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)

    def boom():
        raise RuntimeError("kaboom-internal-detail")

    monkeypatch.setattr(ctx.docs, "tags", boom)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("get_tags", {}))
    assert d["ok"] is False and d["error"]["code"] == "internal"
    assert "kaboom" not in json.dumps(d)


async def test_bad_key_is_rate_limited(ctx, monkeypatch):
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: "lw_bogus_invalid_key")
    mcp = create_mcp_server(ctx)
    messages = [_payload(await mcp.call_tool("get_tags", {}))["error"]["message"] for _ in range(15)]
    assert any("Too many" in m for m in messages)  # limiter engaged after repeated failures


# -- MCP parity tools: folders, task toggle, bulk properties ---------------
async def test_folder_create_list_delete_tools(editor_mcp):
    # Create an empty folder, see it via list_folders, then delete it.
    created = _payload(await editor_mcp.call_tool("create_folder", {"path": "Projects"}))
    assert created["ok"] and created["path"] == "Projects"
    listed = _payload(await editor_mcp.call_tool("list_folders", {}))
    assert listed["ok"] and "Projects" in listed["folders"]
    deleted = _payload(await editor_mcp.call_tool("delete_folder", {"path": "Projects"}))
    assert deleted["ok"] and deleted["deleted"] is True
    again = _payload(await editor_mcp.call_tool("list_folders", {}))
    assert "Projects" not in again["folders"]


async def test_create_folder_conflict_tool(editor_mcp):
    assert _payload(await editor_mcp.call_tool("create_folder", {"path": "Dup"}))["ok"]
    d = _payload(await editor_mcp.call_tool("create_folder", {"path": "Dup"}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"


async def test_delete_nonempty_folder_rejected_tool(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "notes/keep.md", "content": "# K\n\nx"}))
    d = _payload(await editor_mcp.call_tool("delete_folder", {"path": "notes"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_folder_tools_forbidden_for_viewer(ctx, principals, monkeypatch):
    vkey = create_api_key(ctx.db, principals["viewer"], "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("create_folder", {"path": "X"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_toggle_task_tool(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "todo.md", "content": "- [ ] a\n- [ ] b\n"}))
    # 0-based index targets the first checkbox.
    d = _payload(await editor_mcp.call_tool(
        "toggle_task", {"path": "todo.md", "index": 0, "return_content": "full"}))
    assert d["ok"] and "- [x] a" in d["content"] and "- [ ] b" in d["content"]
    # Toggling it back flips it off again.
    back = _payload(await editor_mcp.call_tool(
        "toggle_task", {"path": "todo.md", "index": 0, "return_content": "full"}))
    assert back["ok"] and "- [ ] a" in back["content"]


async def test_toggle_task_requires_target(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "t2.md", "content": "- [ ] a\n"}))
    d = _payload(await editor_mcp.call_tool("toggle_task", {"path": "t2.md"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_set_document_properties_replaces_whole_set(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "props.md", "content": "# P\n\nbody"}))
    d = _payload(await editor_mcp.call_tool(
        "set_document_properties",
        {"path": "props.md", "properties": {"status": "draft", "aliases": ["a1", "a2"]},
         "return_content": "full"}))
    assert d["ok"] and "status: draft" in d["content"] and "aliases: [a1, a2]" in d["content"]
    # Reconciling to a set that omits 'aliases' removes it (declarative full replace).
    r = _payload(await editor_mcp.call_tool(
        "set_document_properties",
        {"path": "props.md", "properties": {"status": "final"}, "return_content": "full"}))
    assert r["ok"] and "status: final" in r["content"] and "aliases" not in r["content"]


async def test_set_document_properties_rejects_reserved_key(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "pr.md", "content": "# P\n\nb"}))
    d = _payload(await editor_mcp.call_tool(
        "set_document_properties", {"path": "pr.md", "properties": {"title": "x"}}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_edit_documents_batch_new_ops(editor_mcp):
    # The newly-exposed ops dispatch through the batch tool too.
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "daily.md", "content": "- [ ] task\n"}))
    ops = [
        {"op": "toggle_task", "path": "daily.md", "index": 0},
        {"op": "set_properties", "path": "daily.md", "properties": {"status": "done"}},
        {"op": "create_folder", "path": "Archive"},
    ]
    d = _payload(await editor_mcp.call_tool("edit_documents", {"operations": ops}))
    assert d["ok"] and d["applied"] == 3 and d["failed"] == 0
    read = _payload(await editor_mcp.call_tool("read_document", {"path": "daily.md"}))
    assert "- [x] task" in read["content"] and "status: done" in read["content"]


async def test_internal_error_envelope_carries_request_id(ctx, principals, monkeypatch):
    # The structured internal-error envelope includes the correlation id so an agent
    # can quote it and an operator can grep straight to the failing call's log line.
    key = create_api_key(ctx.db, principals["editor"], "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)

    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ctx.docs, "tags", boom)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("get_tags", {}))
    assert d["ok"] is False and d["error"]["code"] == "internal"
    assert isinstance(d["error"].get("request_id"), str) and d["error"]["request_id"]


# -- shortlist: daily note, trash, dry-run, search enrichment --------------
@pytest.fixture
def admin_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["admin"], "adminkey")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


async def test_daily_note_create_then_idempotent(editor_mcp):
    first = _payload(await editor_mcp.call_tool(
        "get_or_create_daily_note", {"date": "2026-01-15", "return_content": "full"}))
    assert first["ok"] and first["created"] is True and first["path"] == "daily/2026-01-15.md"
    assert "# 2026-01-15" in first["content"]
    again = _payload(await editor_mcp.call_tool(
        "get_or_create_daily_note", {"date": "2026-01-15"}))
    assert again["ok"] and again["created"] is False and again["version"] == first["version"]


async def test_daily_note_rejects_bad_date(editor_mcp):
    d = _payload(await editor_mcp.call_tool("get_or_create_daily_note", {"date": "2026/01/15"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_trash_lifecycle(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "tr.md", "content": "# T\n\nx"}))
    _payload(await editor_mcp.call_tool("delete_document", {"path": "tr.md"}))
    trash = _payload(await editor_mcp.call_tool("list_trash", {}))
    assert trash["ok"] and any(d["path"] == "tr.md" for d in trash["documents"])
    restored = _payload(await editor_mcp.call_tool("restore_document", {"path": "tr.md"}))
    assert restored["ok"] and restored["restored"] is True
    # Back to a live, readable document; no longer in the trash.
    read = _payload(await editor_mcp.call_tool("read_document", {"path": "tr.md"}))
    assert read["ok"] and "x" in read["content"]
    assert not any(d["path"] == "tr.md"
                   for d in _payload(await editor_mcp.call_tool("list_trash", {}))["documents"])


async def test_restore_rejects_live_document(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "live.md", "content": "x"}))
    d = _payload(await editor_mcp.call_tool("restore_document", {"path": "live.md"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_purge_requires_admin(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "pg.md", "content": "x"}))
    _payload(await editor_mcp.call_tool("delete_document", {"path": "pg.md"}))
    d = _payload(await editor_mcp.call_tool("purge_document", {"path": "pg.md"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_purge_by_admin_removes_history(admin_mcp):
    _payload(await admin_mcp.call_tool("create_document", {"path": "ph.md", "content": "x"}))
    _payload(await admin_mcp.call_tool("delete_document", {"path": "ph.md"}))
    d = _payload(await admin_mcp.call_tool("purge_document", {"path": "ph.md"}))
    assert d["ok"] and d["purged"] is True
    # Gone for good: not in the trash, and a fresh create at the same path is a v1 doc.
    assert not any(x["path"] == "ph.md"
                   for x in _payload(await admin_mcp.call_tool("list_trash", {}))["documents"])
    recreated = _payload(await admin_mcp.call_tool("create_document", {"path": "ph.md", "content": "y"}))
    assert recreated["ok"] and recreated["version"] == 1


async def test_move_dry_run_previews_without_moving(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "mv.md", "content": "# MV\n\nx"}))
    _payload(await editor_mcp.call_tool("create_document", {"path": "ref.md", "content": "see [[mv]]"}))
    prev = _payload(await editor_mcp.call_tool(
        "move_document", {"path": "mv.md", "new_path": "moved.md", "dry_run": True}))
    assert prev["ok"] and prev["dry_run"] is True and prev["dest_exists"] is False
    assert "ref.md" in prev["inbound"] and prev["inbound_count"] >= 1
    # Nothing actually moved: the original still exists, the destination does not.
    assert _payload(await editor_mcp.call_tool("read_document", {"path": "mv.md"}))["ok"]
    assert _payload(await editor_mcp.call_tool("read_document", {"path": "moved.md"}))["ok"] is False


async def test_edit_documents_dry_run_predicts_outcomes(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "ex.md", "content": "v1"}))
    ops = [
        {"op": "create", "path": "fresh.md", "content": "x"},          # would apply
        {"op": "update", "path": "ex.md", "base_version": 0, "content": "y"},  # stale -> conflict
        {"op": "create", "path": "ex.md", "content": "dup"},           # exists -> conflict
    ]
    d = _payload(await editor_mcp.call_tool("edit_documents", {"operations": ops, "dry_run": True}))
    assert d["ok"] and d["dry_run"] is True
    assert d["would_apply"] == 1 and d["would_fail"] == 2
    assert d["results"][1]["error"]["code"] == "conflict"
    # The dry run mutated nothing: fresh.md was not actually created.
    assert _payload(await editor_mcp.call_tool("read_document", {"path": "fresh.md"}))["ok"] is False


async def test_search_results_carry_link_counts_and_recency(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "kw.md", "content": "# KW\n\nzymurgy distinctive term"}))
    _payload(await editor_mcp.call_tool("create_document", {"path": "p1.md", "content": "[[kw]]"}))
    d = _payload(await editor_mcp.call_tool("search_documents", {"query": "zymurgy", "mode": "bm25"}))
    assert d["ok"] and d["count"] >= 1
    hit = next(r for r in d["results"] if r["path"] == "kw.md")
    assert hit["backlinks_count"] >= 1          # p1 links to it
    assert hit["updated_at"] and "outlinks_count" in hit
    # A future 'since' filters everything out; a past one keeps the hit.
    future = _payload(await editor_mcp.call_tool(
        "search_documents", {"query": "zymurgy", "mode": "bm25", "since": "2999-01-01"}))
    assert future["count"] == 0
    past = _payload(await editor_mcp.call_tool(
        "search_documents", {"query": "zymurgy", "mode": "bm25", "since": "2000-01-01"}))
    assert any(r["path"] == "kw.md" for r in past["results"])


# -- MCP parity finish: favourites, attachment upload, tag rename/merge ----
async def test_favorite_set_and_list(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "fav.md", "content": "# F\n\nx"}))
    assert _payload(await editor_mcp.call_tool("list_favorites", {}))["count"] == 0
    r = _payload(await editor_mcp.call_tool("set_favorite", {"path": "fav.md", "favorite": True}))
    assert r["ok"] and r["favorite"] is True
    listed = _payload(await editor_mcp.call_tool("list_favorites", {}))
    assert listed["count"] == 1 and listed["documents"][0]["path"] == "fav.md"
    # idempotent set True again — still one
    _payload(await editor_mcp.call_tool("set_favorite", {"path": "fav.md", "favorite": True}))
    assert _payload(await editor_mcp.call_tool("list_favorites", {}))["count"] == 1
    # unpin
    assert _payload(await editor_mcp.call_tool(
        "set_favorite", {"path": "fav.md", "favorite": False}))["favorite"] is False
    assert _payload(await editor_mcp.call_tool("list_favorites", {}))["count"] == 0


async def test_set_favorite_missing_doc(editor_mcp):
    d = _payload(await editor_mcp.call_tool("set_favorite", {"path": "ghost.md"}))
    assert d["ok"] is False and d["error"]["code"] == "not_found"


async def test_upload_attachment_roundtrip(editor_mcp):
    import base64
    b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-bytes").decode()
    d = _payload(await editor_mcp.call_tool(
        "upload_attachment", {"filename": "shot.png", "content_base64": b64}))
    assert d["ok"] and d["url"].startswith("/attachments/") and d["markdown"].startswith("![")
    assert d["path"].startswith("_attachments/")


async def test_upload_attachment_rejects_bad_base64(editor_mcp):
    d = _payload(await editor_mcp.call_tool(
        "upload_attachment", {"filename": "x.png", "content_base64": "not!!base64"}))
    assert d["ok"] is False and d["error"]["code"] == "validation"


async def test_upload_attachment_forbidden_for_viewer(ctx, principals, monkeypatch):
    import base64
    vkey = create_api_key(ctx.db, principals["viewer"], "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("upload_attachment",
                 {"filename": "x.png", "content_base64": base64.b64encode(b"x").decode()}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_rename_tag_across_vault(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "a.md", "content": "# A", "tags": ["proj-x", "keep"]}))
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "b.md", "content": "# B", "tags": ["proj-x"]}))
    d = _payload(await editor_mcp.call_tool("rename_tag", {"old": "proj-x", "new": "project-x"}))
    assert d["ok"] and d["docs_affected"] == 2 and d["docs_changed"] == 2
    a = _payload(await editor_mcp.call_tool("read_document", {"path": "a.md"}))
    assert "project-x" in a["tags"] and "proj-x" not in a["tags"] and "keep" in a["tags"]


async def test_merge_tags_folds_sources(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "m1.md", "content": "# M1", "tags": ["draft"]}))
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "m2.md", "content": "# M2", "tags": ["wip"]}))
    d = _payload(await editor_mcp.call_tool(
        "merge_tags", {"sources": ["draft", "wip"], "dest": "in-progress"}))
    assert d["ok"] and d["docs_affected"] == 2
    for path in ("m1.md", "m2.md"):
        tags = _payload(await editor_mcp.call_tool("read_document", {"path": path}))["tags"]
        assert "in-progress" in tags and "draft" not in tags and "wip" not in tags


async def test_rename_tag_forbidden_for_viewer(ctx, principals, monkeypatch):
    vkey = create_api_key(ctx.db, principals["viewer"], "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("rename_tag", {"old": "a", "new": "b"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_get_backlinks_with_context(editor_mcp):
    # with_context=true returns a 'context' snippet per inbound link (one call instead of
    # N read_document round-trips); without it the shape stays lean.
    await editor_mcp.call_tool("create_document", {"path": "t.md", "content": "# T\n\nbody"})
    await editor_mcp.call_tool(
        "create_document", {"path": "s.md", "content": "# S\n\nA line that mentions [[t]] here."})
    plain = _payload(await editor_mcp.call_tool("get_backlinks", {"path": "t.md"}))
    assert plain["ok"] and all("context" not in b for b in plain["backlinks"])
    withctx = _payload(await editor_mcp.call_tool(
        "get_backlinks", {"path": "t.md", "with_context": True}))
    b = next(x for x in withctx["backlinks"] if x["src_path"] == "s.md")
    assert "context" in b and "mentions" in b["context"]


async def test_mcp_read_rate_limited_per_principal(editor_mcp):
    # Embedding-bearing reads are bounded per principal: past the window the agent gets a
    # structured 'rate_limited' envelope, protecting the single-process encoder from a flood.
    await editor_mcp.call_tool("create_document", {"path": "r.md", "content": "# R\n\napple"})
    last = None
    for _ in range(61):
        last = _payload(await editor_mcp.call_tool(
            "search_documents", {"query": "apple", "mode": "bm25"}))
    assert last["ok"] is False and last["error"]["code"] == "rate_limited"
