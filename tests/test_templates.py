"""Document templates: vault/_templates listing and create(template=)."""
from __future__ import annotations

import json

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import create_mcp_server
from llm_wiki.services.auth import create_api_key
from llm_wiki.services.errors import ValidationError


def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


def _write_template(vault, name: str, body: str) -> None:
    tdir = vault / "_templates"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / name).write_text(body, encoding="utf-8")


def test_list_templates_empty(ctx):
    assert ctx.docs.list_templates() == []


def test_list_templates_finds_vault_file(ctx):
    _write_template(
        ctx.settings.vault_path,
        "foo.md",
        "---\ntitle: Foo Template\n---\n# Hello\n\nbody preview text\n",
    )
    items = ctx.docs.list_templates()
    assert len(items) == 1
    item = items[0]
    assert item["name"] in ("foo", "foo.md")
    assert item["path"] == "_templates/foo.md"
    assert item["title"] == "Foo Template"
    # preview is body after frontmatter strip, first ~200 chars
    assert "Hello" in item["preview"] or "body preview" in item["preview"]
    assert "title:" not in item["preview"]
    assert not item["preview"].lstrip().startswith("---")


def test_create_with_template_copies_body(ctx, principals):
    body = "# Meeting\n\nAgenda:\n- item one\n"
    _write_template(ctx.settings.vault_path, "meeting.md", body)
    p = principals["editor"]
    doc = ctx.docs.create(p, "notes/m1.md", "", template="meeting", embed=False)
    assert doc["content"] == body
    # also accept name with .md suffix
    doc2 = ctx.docs.create(p, "notes/m2.md", "", template="meeting.md", embed=False)
    assert doc2["content"] == body


def test_create_with_template_path_traversal_raises(ctx, principals):
    p = principals["editor"]
    for bad in ("../secret", "../../etc/passwd", "/etc/passwd", "..\\secret",
                "_templates/../secret", "foo/../../secret"):
        with pytest.raises(ValidationError):
            ctx.docs.create(p, "x.md", "", template=bad, embed=False)


def test_create_without_template_still_works(ctx, principals):
    doc = ctx.docs.create(
        principals["editor"], "plain.md", "# Hi\n\nplain body", embed=False
    )
    assert doc["version"] == 1
    assert "plain body" in doc["content"]


def test_create_with_unknown_template_raises(ctx, principals):
    with pytest.raises(ValidationError):
        ctx.docs.create(
            principals["editor"], "x.md", "", template="missing-tpl", embed=False
        )


# ---- MCP -----------------------------------------------------------------
@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"], "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


async def test_mcp_list_templates_registered(editor_mcp):
    names = {t.name for t in await editor_mcp.list_tools()}
    assert "list_templates" in names
    tools = {t.name: t for t in await editor_mcp.list_tools()}
    props = tools["create_document"].inputSchema.get("properties", {})
    assert "template" in props


async def test_mcp_list_templates_and_create_with_template(editor_mcp, ctx):
    _write_template(
        ctx.settings.vault_path,
        "daily.md",
        "# Daily\n\n## Tasks\n- [ ] \n",
    )
    listed = _payload(await editor_mcp.call_tool("list_templates", {}))
    assert listed["ok"] is True
    names = {t["name"] for t in listed["templates"]}
    assert "daily" in names or "daily.md" in names

    created = _payload(await editor_mcp.call_tool("create_document", {
        "path": "from-tpl.md",
        "content": "",
        "template": "daily",
        "return_content": "full",
    }))
    assert created["ok"] is True
    assert "Tasks" in created.get("content", "") or created.get("content") == ""
    # if metadata-only still ok, re-read
    if "content" not in created or not created.get("content"):
        read = _payload(await editor_mcp.call_tool(
            "read_document", {"path": "from-tpl.md"}))
        assert read["ok"] and "Tasks" in read["content"]
    else:
        assert "Tasks" in created["content"]


async def test_mcp_create_document_template_traversal(editor_mcp):
    d = _payload(await editor_mcp.call_tool("create_document", {
        "path": "bad.md",
        "content": "",
        "template": "../secret",
    }))
    assert d["ok"] is False and d["error"]["code"] == "validation"
