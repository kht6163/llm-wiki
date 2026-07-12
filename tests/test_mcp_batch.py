"""MCP batch-read surface: ``read_documents`` multi-path reads with partial
failure isolation (missing docs don't abort the whole call) and hard input
limits (empty / over-20 paths → validation)."""
import json

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import create_mcp_server
from llm_wiki.services.auth import create_api_key


def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"], "batch-reader")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


async def test_read_documents_is_registered(editor_mcp):
    names = {t.name for t in await editor_mcp.list_tools()}
    assert "read_documents" in names


async def test_read_documents_multi_read(editor_mcp):
    for path, body in (("a.md", "# A\n\nalpha body"), ("b.md", "# B\n\nbeta body")):
        created = _payload(await editor_mcp.call_tool(
            "create_document", {"path": path, "content": body}))
        assert created["ok"]

    d = _payload(await editor_mcp.call_tool(
        "read_documents", {"paths": ["a.md", "b.md"]}))
    assert d["ok"] is True
    assert d["count"] == 2
    assert len(d["items"]) == 2

    by_path = {item["path"]: item for item in d["items"]}
    assert by_path["a.md"]["ok"] is True
    assert "alpha body" in by_path["a.md"]["content"]
    assert by_path["a.md"]["version"] == 1
    assert by_path["a.md"]["title"]
    assert by_path["b.md"]["ok"] is True
    assert "beta body" in by_path["b.md"]["content"]


async def test_read_documents_max_chars_applies_per_item(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "long.md", "content": "abcdefghij"}))
    d = _payload(await editor_mcp.call_tool(
        "read_documents", {"paths": ["long.md"], "max_chars": 4}))
    assert d["ok"] is True
    item = d["items"][0]
    assert item["ok"] is True
    assert item["content"] == "abcd"
    assert item["truncated"] is True
    assert item["full_length"] == 10


async def test_read_documents_missing_path_partial_success(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "alive.md", "content": "# Alive\n\nhere"}))

    d = _payload(await editor_mcp.call_tool(
        "read_documents", {"paths": ["alive.md", "ghost.md", "also-missing.md"]}))
    assert d["ok"] is True
    assert d["count"] == 3
    assert len(d["items"]) == 3

    alive, ghost, missing = d["items"]
    assert alive["ok"] is True and alive["path"] == "alive.md"
    assert "here" in alive["content"]

    assert ghost["ok"] is False and ghost["path"] == "ghost.md"
    assert ghost["error"]["code"] == "not_found"
    assert ghost["error"]["suggested_action"] == "verify_path"

    assert missing["ok"] is False and missing["path"] == "also-missing.md"
    assert missing["error"]["code"] == "not_found"


async def test_read_documents_over_limit_validation(editor_mcp):
    paths = [f"doc{i}.md" for i in range(21)]
    d = _payload(await editor_mcp.call_tool("read_documents", {"paths": paths}))
    assert d["ok"] is False
    assert d["error"]["code"] == "validation"
    assert d["error"]["suggested_action"] == "fix_request"
    assert "20" in d["error"]["message"]


async def test_read_documents_empty_list_validation(editor_mcp):
    d = _payload(await editor_mcp.call_tool("read_documents", {"paths": []}))
    assert d["ok"] is False
    assert d["error"]["code"] == "validation"
    assert d["error"]["suggested_action"] == "fix_request"


async def test_read_documents_exactly_20_paths_ok(editor_mcp, ctx, principals):
    for i in range(20):
        ctx.docs.create(principals["editor"], f"bulk{i}.md", f"# B{i}\n\nbody {i}")
    paths = [f"bulk{i}.md" for i in range(20)]
    d = _payload(await editor_mcp.call_tool("read_documents", {"paths": paths}))
    assert d["ok"] is True
    assert d["count"] == 20
    assert all(item["ok"] for item in d["items"])
