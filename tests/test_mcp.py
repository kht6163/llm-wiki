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
        "search_documents", "read_document", "get_outline", "list_documents",
        "list_recent_changes", "list_broken_links", "get_tags", "get_links",
        "get_backlinks", "get_revisions", "get_revision", "get_graph",
        "assemble_context", "get_related_documents",
        "create_document", "update_document", "patch_document", "replace_section",
        "append_section", "patch_tags", "move_document", "delete_document",
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


async def test_viewer_write_forbidden(ctx, principals, monkeypatch):
    vkey = create_api_key(ctx.db, principals["viewer"].user_id, "vk")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: vkey)
    mcp = create_mcp_server(ctx)
    d = _payload(await mcp.call_tool("create_document", {"path": "x.md", "content": "nope"}))
    assert d["ok"] is False and d["error"]["code"] == "forbidden"


async def test_update_conflict_envelope(editor_mcp):
    _payload(await editor_mcp.call_tool("create_document", {"path": "c.md", "content": "v1"}))
    d = _payload(await editor_mcp.call_tool(
        "update_document", {"path": "c.md", "base_version": 0, "content": "v2"}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"
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
    pd = _payload(await mcp.call_tool("patch_document", {"path": "w.md", "find": "alpha", "replace": "beta"}))
    assert pd["ok"] and "beta" in pd["content"]
    ap = _payload(await mcp.call_tool("append_section", {"path": "w.md", "heading": "A", "text": "gamma"}))
    assert ap["ok"] and "gamma" in ap["content"]
    pt = _payload(await mcp.call_tool("patch_tags", {"path": "w.md", "add": ["t1", "t2"]}))
    assert pt["ok"] and {"t1", "t2"} <= set(pt["tags"])
    mv = _payload(await mcp.call_tool("move_document", {"path": "w.md", "new_path": "moved/w.md"}))
    assert mv["ok"] and mv["path"] == "moved/w.md"
    dl = _payload(await mcp.call_tool("delete_document", {"path": "moved/w.md"}))
    assert dl["ok"] and dl["deleted"] is True


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
