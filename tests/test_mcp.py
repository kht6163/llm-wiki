"""MCP-layer tests: tool registration, Bearer-header parsing, the structured error
envelope, and end-to-end tool calls (auth gate, RBAC, conflict, rate limit) driven
through the real FastMCP tool wrappers."""
import json
from types import SimpleNamespace

import pytest

import llm_wiki.mcp_server as mcp_mod
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
    }
    assert expected <= names, names


def test_error_envelope_shape():
    d = ForbiddenError("nope").to_dict()
    assert d == {"ok": False, "error": {"code": "forbidden", "message": "nope"}}


# -- end-to-end tool calls -------------------------------------------------
@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"].user_id, "agent")
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
    vkey = create_api_key(ctx.db, principals["viewer"].user_id, "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("create_document", {"path": "x.md", "content": "nope"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


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
    key = create_api_key(ctx.db, principals["editor"].user_id, "agent")
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
