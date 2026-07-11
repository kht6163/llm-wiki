"""Coverage of MCP envelopes, throttles, batch dispatch and redacted auditing."""

import json
from types import SimpleNamespace

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import _request, _request_id, create_mcp_server
from llm_wiki.services.auth import create_api_key
from llm_wiki.services.errors import ForbiddenError, ValidationError


def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def _server(ctx, principal, monkeypatch):
    key = create_api_key(ctx.db, principal, "coverage")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _ctx: key)
    return create_mcp_server(ctx)


def test_request_helpers_honor_trimmed_header_and_survive_missing_context(monkeypatch):
    request = SimpleNamespace(headers={"x-request-id": "  caller-id  "})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=request))
    assert _request_id(ctx) == "caller-id"
    request.headers["x-request-id"] = " "
    monkeypatch.setattr(mcp_mod, "new_request_id", lambda: "minted")
    assert _request_id(ctx) == "minted"

    class BrokenContext:
        @property
        def request_context(self):
            raise RuntimeError("outside request")

    assert _request(BrokenContext()) is None
    assert _request_id(BrokenContext()) == "minted"


@pytest.mark.asyncio
async def test_auth_block_audit_failure_preserves_unauthorized_envelope(ctx, monkeypatch, caplog):
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _ctx: "invalid-secret-token")
    monkeypatch.setattr(
        mcp_mod.audit,
        "record_tx",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    mcp = create_mcp_server(ctx)
    results = [_payload(await mcp.call_tool("get_tags", {})) for _ in range(11)]
    assert all(result["ok"] is False for result in results)
    assert results[-1]["error"]["code"] == "unauthorized"
    assert "failed to audit mcp auth block" in caplog.text
    assert "invalid-secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_read_throttle_audit_failure_and_limit_envelope(
    ctx, principals, monkeypatch, caplog
):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    monkeypatch.setattr(ctx.docs, "search_page", lambda *a, **k: ([], False))

    def audit_failure(*args, **kwargs):
        if kwargs.get("action") == "read_rate_limited":
            raise RuntimeError("audit unavailable")

    monkeypatch.setattr(mcp_mod.audit, "record_tx", audit_failure)
    for _ in range(60):
        result = _payload(await mcp.call_tool("search_documents", {"query": "bounded"}))
        assert result["ok"] is True
    blocked = _payload(await mcp.call_tool("search_documents", {"query": "bounded"}))
    assert blocked["ok"] is False and blocked["error"]["code"] == "rate_limited"
    assert "failed to audit read rate limit" in caplog.text


@pytest.mark.asyncio
async def test_write_audit_failure_and_false_result_keep_original_envelope(
    ctx, principals, monkeypatch, caplog
):
    viewer = _server(ctx, principals["viewer"], monkeypatch)
    monkeypatch.setattr(
        mcp_mod.audit,
        "record_tx",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    denied = _payload(
        await viewer.call_tool("create_document", {"path": "private.md", "content": "secret"})
    )
    assert denied["error"]["code"] == "forbidden"
    assert "failed to audit rejected mcp write" in caplog.text
    assert "secret" not in caplog.text

    editor = _server(ctx, principals["editor"], monkeypatch)
    monkeypatch.setattr(ctx.docs, "create", lambda *a, **k: ForbiddenError("shaped").to_dict())
    shaped = _payload(
        await editor.call_tool("create_document", {"path": "false.md", "content": "hidden"})
    )
    assert shaped == ForbiddenError("shaped").to_dict()


@pytest.mark.asyncio
async def test_read_document_max_chars_and_corpus_formats(ctx, principals, monkeypatch):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    _payload(await mcp.call_tool("create_document", {"path": "long.md", "content": "abcdef"}))
    read = _payload(await mcp.call_tool("read_document", {"path": "long.md", "max_chars": 3}))
    assert read["content"] == "abc" and read["truncated"] and read["full_length"] == 6
    broken = _payload(await mcp.call_tool("list_broken_links", {}))
    assert broken["ok"] and "links" in broken
    index = _payload(await mcp.call_tool("export_corpus", {"format": "index"}))
    full = _payload(await mcp.call_tool("export_corpus", {"format": "full", "max_chars": 1000}))
    assert index["format"] == "index" and full["format"] == "full"


@pytest.mark.asyncio
async def test_batch_dispatches_remaining_operations_and_errors(ctx, principals, monkeypatch):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    _payload(await mcp.call_tool("create_document", {
        "path": "batch.md", "content": "# T\n\n## A\nold\n- [ ] task\n",
    }))
    _payload(await mcp.call_tool("create_document", {"path": "dest.md", "content": "dest"}))
    _payload(await mcp.call_tool("create_document", {"path": "ref.md", "content": "[[batch]]"}))

    operations = [
        {"op": "patch", "path": "batch.md", "find": "old", "replace": "new"},
        {"op": "replace_section", "path": "batch.md", "heading": "A", "text": "replace"},
        {"op": "append_section", "path": "batch.md", "heading": "A", "text": "append"},
        {"op": "patch_tags", "path": "batch.md", "add": ["x"], "remove": []},
        {"op": "delete_folder", "path": "missing-folder"},
        {"op": "move", "path": "batch.md"},
        {"op": "restore", "path": "batch.md"},
        {"op": "rename_references", "old_path": 3, "new_path": None},
        {"op": "rename_references", "old_path": "batch.md", "new_path": "renamed.md"},
        {"op": "unknown"},
        {"op": "delete", "path": "batch.md"},
    ]
    result = _payload(await mcp.call_tool(
        "edit_documents", {"operations": operations, "stop_on_error": False}
    ))
    assert result["ok"] and result["applied"] >= 5 and result["failed"] >= 3
    codes = [item.get("error", {}).get("code") for item in result["results"]]
    assert "validation" in codes
    assert ctx.docs.exists("batch.md") is False


@pytest.mark.asyncio
async def test_batch_preview_reports_every_feasibility_branch(ctx, principals, monkeypatch):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    _payload(await mcp.call_tool("create_document", {"path": "live.md", "content": "v1"}))
    _payload(await mcp.call_tool("create_document", {"path": "occupied.md", "content": "v1"}))
    operations = [
        {"op": "rename_references", "old_path": "live.md", "new_path": "renamed.md"},
        {"op": "rename_references", "old_path": 3},
        {"op": "create", "path": "live.md"},
        {"op": "create", "path": "new.md"},
        {"op": "create_folder", "path": "folder"},
        {"op": "update", "path": "live.md", "base_version": 99},
        {"op": "update", "path": "live.md"},
        {"op": "move", "path": "live.md"},
        {"op": "move", "path": "live.md", "new_path": "occupied.md"},
        {"op": "move", "path": "live.md", "new_path": "free.md"},
        {"op": "restore", "path": "tomb.md"},
        {"op": "unknown", "path": "live.md"},
        {"op": "update", "path": "live.md", "base_version": "not-an-int"},
        {"op": "create"},
    ]
    preview = _payload(await mcp.call_tool(
        "edit_documents", {"operations": operations, "dry_run": True}
    ))
    assert preview["dry_run"] and preview["would_apply"] >= 4
    assert preview["would_fail"] >= 7
    errors = [row["error"]["code"] for row in preview["results"] if not row["ok"]]
    assert {"validation", "conflict"} <= set(errors)
    assert ctx.docs.exists("new.md") is False and ctx.docs.exists("free.md") is False

    preview_op = dict(zip(
        mcp._tool_manager.get_tool("edit_documents").fn.__code__.co_freevars,
        mcp._tool_manager.get_tool("edit_documents").fn.__closure__,
        strict=True,
    ))["_preview_op"].cell_contents
    forbidden = preview_op(principals["viewer"], {"op": "create", "path": "private.md"})
    assert forbidden["ok"] is False and forbidden["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_batch_malformed_objects_stop_modes_and_redacted_audit(
    ctx, principals, monkeypatch
):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    tool = mcp._tool_manager.get_tool("edit_documents")

    stopped = await tool.fn(None, operations=[None, {"op": "create", "path": "later.md"}])
    assert stopped["failed"] == 1 and stopped["stopped_early"] is True
    continued = await tool.fn(
        None,
        operations=[None, {"op": "create", "path": "later.md", "content": "safe"}],
        stop_on_error=False,
    )
    assert continued["applied"] == 1 and continued["failed"] == 1
    assert ctx.docs.get("later.md")["content"] == "safe"

    malformed = _payload(await mcp.call_tool("edit_documents", {
        "operations": [
            {"op": "set_properties", "path": "later.md", "properties": ["secret"]},
            {"op": "create", "path": "after-error.md", "content": "kept"},
        ],
        "stop_on_error": False,
    }))
    assert malformed["failed"] == 1
    assert malformed["results"][0]["error"]["code"] == "validation"
    assert malformed["applied"] == 1 and ctx.docs.exists("after-error.md")
    stopped_bad_shape = _payload(await mcp.call_tool("edit_documents", {
        "operations": [
            {"op": "set_properties", "path": "later.md", "properties": ["secret"]},
            {"op": "create", "path": "must-not-run.md"},
        ],
    }))
    assert stopped_bad_shape["stopped_early"] is True
    assert ctx.docs.exists("must-not-run.md") is False
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT target, detail FROM audit_log WHERE actor='alice' AND action='doc_update' "
            "AND outcome='validation' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert dict(row) == {"target": "later.md", "detail": None}


@pytest.mark.asyncio
async def test_batch_limits_and_viewer_preview_forbidden_are_structured(ctx, principals, monkeypatch):
    editor = _server(ctx, principals["editor"], monkeypatch)
    empty = _payload(await editor.call_tool("edit_documents", {"operations": []}))
    huge = _payload(await editor.call_tool(
        "edit_documents", {"operations": [{"op": "create", "path": f"x{i}.md"} for i in range(101)]}
    ))
    assert empty["error"]["code"] == huge["error"]["code"] == "validation"

    viewer = _server(ctx, principals["viewer"], monkeypatch)
    preview = _payload(await viewer.call_tool("edit_documents", {
        "operations": [{"op": "create", "path": "private.md"}], "dry_run": True,
    }))
    assert preview["error"]["code"] == "forbidden"
    tool = viewer._tool_manager.get_tool("edit_documents")
    denied = await tool.fn(None, operations=[None, {}], stop_on_error=False)
    assert denied["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_batch_move_failure_audit_redacts_paths_only(ctx, principals, monkeypatch):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    _payload(await mcp.call_tool("create_document", {"path": "from.md", "content": "body"}))
    _payload(await mcp.call_tool("create_document", {"path": "to.md", "content": "body"}))
    result = _payload(await mcp.call_tool("edit_documents", {"operations": [{
        "op": "move", "path": "from.md", "new_path": "to.md", "private": "secret"
    }]}))
    assert result["results"][0]["error"]["code"] == "conflict"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT target, detail FROM audit_log WHERE actor='alice' AND action='doc_move' "
            "AND outcome='conflict' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert dict(row) == {"target": "from.md -> to.md", "detail": None}


@pytest.mark.asyncio
async def test_batch_restore_with_version_applies_snapshot(ctx, principals, monkeypatch):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    _payload(await mcp.call_tool("create_document", {"path": "restore.md", "content": "v1"}))
    _payload(await mcp.call_tool("update_document", {
        "path": "restore.md", "base_version": 1, "content": "v2",
    }))
    restored = _payload(await mcp.call_tool("edit_documents", {"operations": [{
        "op": "restore", "path": "restore.md", "version": 1,
    }]}))
    assert restored["applied"] == 1
    assert ctx.docs.get("restore.md")["content"] == "v1"


@pytest.mark.asyncio
async def test_call_wiki_error_before_principal_keeps_structured_envelope(
    ctx, principals, monkeypatch
):
    mcp = _server(ctx, principals["editor"], monkeypatch)
    tool = mcp._tool_manager.get_tool("get_tags")
    call_cell = dict(zip(tool.fn.__code__.co_freevars, tool.fn.__closure__, strict=True))["_call"]
    call = call_cell.cell_contents
    principal_cell = dict(zip(call.__code__.co_freevars, call.__closure__, strict=True))["_principal"]
    original = principal_cell.cell_contents
    principal_cell.cell_contents = lambda _token: (_ for _ in ()).throw(ValidationError("pre-auth"))
    try:
        result = _payload(await mcp.call_tool("get_tags", {}))
    finally:
        principal_cell.cell_contents = original
    assert result["ok"] is False and result["error"]["code"] == "validation"
