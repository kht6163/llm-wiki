"""MCP three-way merge preview tool — parity with DocumentService.merge_preview / web UI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import create_mcp_server
from llm_wiki.services.auth import create_api_key


def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"], "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


@pytest.fixture
def viewer_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["viewer"], "viewer-agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


def _doc_state(ctx, path: str) -> tuple[int, int, str]:
    with ctx.db.reader() as conn:
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm=lower(?)", (path,)
        ).fetchone()["id"]
        revisions = conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
    return ctx.docs.get(path)["version"], revisions, Path(ctx.docs.vault, path).read_text()


async def test_preview_document_merge_registered(editor_mcp):
    names = {t.name for t in await editor_mcp.list_tools()}
    assert "preview_document_merge" in names


async def test_preview_disjoint_auto_merge_does_not_persist(editor_mcp, ctx):
    base = "one\ntwo\nthree\n"
    mine = "ONE\ntwo\nthree\n"
    current = "one\ntwo\nTHREE\n"
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "merge.md", "content": base}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "merge.md", "base_version": 1, "content": current}))
    before = _doc_state(ctx, "merge.md")

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {
            "path": "merge.md",
            "base_version": 1,
            "mine": mine,
            "mine_title": "Merge",
        },
    ))

    assert preview["ok"] is True
    assert preview["path"] == "merge.md"
    assert preview["manual_only"] is False
    assert preview["conflicts"] == []
    assert preview["merged"] == "ONE\ntwo\nTHREE\n"
    assert preview["merged_title"] == "Merge"
    assert preview["title_conflict"] is False
    assert preview["suggested_action"] == "apply_merged_and_update"
    assert preview["base_version"] == 1
    assert preview["current_version"] == 2
    assert _doc_state(ctx, "merge.md") == before


async def test_preview_overlap_serializes_conflicts(editor_mcp):
    base = "one\ntwo\nthree\n"
    mine = "one\nMINE\nthree\n"
    current = "one\nCURRENT\nthree\n"
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "overlap.md", "content": base}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "overlap.md", "base_version": 1, "content": current}))

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {"path": "overlap.md", "base_version": 1, "mine": mine},
    ))

    assert preview["ok"] is True
    assert preview["manual_only"] is False
    assert preview["merged"] == base
    assert preview["conflicts"] == [
        {
            "start_line": 2,
            "base": "two\n",
            "mine": "MINE\n",
            "current": "CURRENT\n",
            "resolved": None,
            "merged_start": 4,
        }
    ]
    assert preview["suggested_action"] == "resolve_conflicts"


async def test_preview_manual_only_when_base_pruned(editor_mcp, ctx):
    base = "base body\n"
    mine = "mine body\n"
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "pruned.md", "content": base}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "pruned.md", "base_version": 1, "content": "current body\n"}))
    with ctx.db.writer() as conn:
        conn.execute("DELETE FROM revisions WHERE version=1")

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {"path": "pruned.md", "base_version": 1, "mine": mine},
    ))

    assert preview["ok"] is True
    assert preview["manual_only"] is True
    assert preview["merged"] is None
    assert preview["conflicts"] == []
    assert preview["suggested_action"] == "manual_merge"


async def test_preview_title_conflict_flag(editor_mcp):
    _payload(await editor_mcp.call_tool(
        "create_document",
        {"path": "titles.md", "content": "body\n", "title": "Base"}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {
            "path": "titles.md",
            "base_version": 1,
            "content": "body\n",
            "title": "Current",
        },
    ))

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {
            "path": "titles.md",
            "base_version": 1,
            "mine": "body\n",
            "mine_title": "Mine",
        },
    ))

    assert preview["ok"] is True
    assert preview["title_conflict"] is True
    assert preview["merged_title"] is None
    assert preview["suggested_action"] == "resolve_conflicts"


async def test_preview_forbidden_for_viewer(viewer_mcp):
    d = _payload(await viewer_mcp.call_tool(
        "preview_document_merge",
        {"path": "x.md", "base_version": 1, "mine": "x"},
    ))
    assert d["ok"] is False
    assert d["error"]["code"] == "forbidden"


async def test_preview_missing_path(editor_mcp):
    d = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {"path": "ghost.md", "base_version": 1, "mine": "x"},
    ))
    assert d["ok"] is False
    assert d["error"]["code"] == "not_found"


async def test_preview_then_update_with_current_version_succeeds(editor_mcp, ctx):
    base = "one\ntwo\nthree\n"
    mine = "ONE\ntwo\nthree\n"
    current = "one\ntwo\nTHREE\n"
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "apply.md", "content": base}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "apply.md", "base_version": 1, "content": current}))

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {
            "path": "apply.md",
            "base_version": 1,
            "mine": mine,
            "mine_title": "Apply",
        },
    ))
    assert preview["suggested_action"] == "apply_merged_and_update"
    assert preview["merged"] == "ONE\ntwo\nTHREE\n"

    updated = _payload(await editor_mcp.call_tool(
        "update_document",
        {
            "path": "apply.md",
            "base_version": preview["current_version"],
            "content": preview["merged"],
            "title": preview["merged_title"],
            "return_content": "full",
        },
    ))
    assert updated["ok"] is True
    assert updated["version"] == 3
    assert updated["content"] == "ONE\ntwo\nTHREE\n"
    assert ctx.docs.get("apply.md")["content"] == "ONE\ntwo\nTHREE\n"


async def test_preview_metadata_omits_bodies(editor_mcp):
    base = "one\ntwo\nthree\n"
    mine = "ONE\ntwo\nthree\n"
    current = "one\ntwo\nTHREE\n"
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "meta.md", "content": base}))
    _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "meta.md", "base_version": 1, "content": current}))

    preview = _payload(await editor_mcp.call_tool(
        "preview_document_merge",
        {
            "path": "meta.md",
            "base_version": 1,
            "mine": mine,
            "return_content": "metadata",
        },
    ))

    assert preview["ok"] is True
    assert preview["suggested_action"] == "apply_merged_and_update"
    assert preview["content_omitted"] is True
    for key in ("base", "mine", "current", "merged"):
        assert key not in preview
    assert preview["base_chars"] == len(base)
    assert preview["mine_chars"] == len(mine)
    assert preview["current_chars"] == len(current)
    assert preview["merged_chars"] == len("ONE\ntwo\nTHREE\n")
    assert preview["conflicts"] == []


async def test_update_conflict_envelope_still_has_no_merge_fields(editor_mcp):
    """Bare update conflict stays token-cheap — no automatic merge payload."""
    _payload(await editor_mcp.call_tool(
        "create_document", {"path": "bare.md", "content": "v1"}))
    d = _payload(await editor_mcp.call_tool(
        "update_document",
        {"path": "bare.md", "base_version": 0, "content": "v2"}))
    assert d["ok"] is False and d["error"]["code"] == "conflict"
    assert "merged" not in d["error"]
    assert "conflicts" not in d["error"]
    assert d["error"]["suggested_action"] == "re_read_and_retry"
