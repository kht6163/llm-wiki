"""API key scopes: read-only keys cannot write via MCP; unused keys are flagged."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import create_mcp_server
from llm_wiki.services import auth
from llm_wiki.services.errors import ValidationError


def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    import json

    return json.loads(blocks[0].text)


def test_create_api_key_default_scope_is_readwrite(ctx, principals):
    key = auth.create_api_key(ctx.db, principals["editor"], "full")
    p = auth.principal_from_api_key(ctx.db, key)
    assert p is not None
    assert p.key_scope == "readwrite"
    assert p.can_write_via_key is True


def test_create_api_key_read_scope(ctx, principals):
    key = auth.create_api_key(ctx.db, principals["editor"], "ro", scope="read")
    p = auth.principal_from_api_key(ctx.db, key)
    assert p is not None
    assert p.key_scope == "read"
    assert p.role == "editor"
    # Key scope gates can_write for MCP principals even when role is editor.
    assert p.can_write is False
    assert p.can_write_via_key is False


def test_create_api_key_rejects_unknown_scope(ctx, principals):
    with pytest.raises(ValidationError):
        auth.create_api_key(ctx.db, principals["editor"], "bad", scope="admin")


def test_list_api_keys_includes_scope_and_unused_flag(ctx, principals):
    auth.create_api_key(ctx.db, principals["editor"], "fresh", scope="read")
    keys = auth.list_api_keys(ctx.db, principals["editor"].user_id)
    assert keys
    row = next(k for k in keys if k["name"] == "fresh")
    assert row["scope"] == "read"
    assert row["unused"] is True  # never used


def test_list_api_keys_marks_stale_last_used(ctx, principals):
    key = auth.create_api_key(ctx.db, principals["editor"], "old")
    prefix = key[: auth.API_KEY_PREFIX_LEN]
    old = (datetime.now(UTC) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at=? WHERE key_prefix=?",
            (old, prefix),
        )
    keys = auth.list_api_keys(ctx.db, principals["editor"].user_id, unused_after_days=30)
    row = next(k for k in keys if k["name"] == "old")
    assert row["unused"] is True


async def test_mcp_read_scope_key_cannot_write(ctx, principals, monkeypatch):
    token = auth.create_api_key(ctx.db, principals["editor"], "ro-agent", scope="read")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: token)
    mcp = create_mcp_server(ctx)

    who = _payload(await mcp.call_tool("whoami", {}))
    assert who["ok"] is True
    assert who.get("can_write") is False or who.get("key_scope") == "read"

    write = _payload(
        await mcp.call_tool(
            "create_document",
            {"path": "scope-deny.md", "content": "nope"},
        )
    )
    assert write["ok"] is False
    assert write["error"]["code"] == "forbidden"


async def test_mcp_readwrite_scope_key_can_write(ctx, principals, monkeypatch):
    token = auth.create_api_key(ctx.db, principals["editor"], "rw-agent", scope="readwrite")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: token)
    mcp = create_mcp_server(ctx)
    write = _payload(
        await mcp.call_tool(
            "create_document",
            {"path": "scope-ok.md", "content": "yes"},
        )
    )
    assert write["ok"] is True
