"""llms.txt / llms-full.txt corpus exports — the agent-facing site map and the
whole-corpus ingest. Covers the DocumentService builders (grouping, descriptions,
frontmatter stripping, truncation), the dual-auth web routes (session OR Bearer),
and the export_corpus MCP tool."""
import json

import pytest
from starlette.testclient import TestClient

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.mcp_server import create_mcp_server
from llm_wiki.services.auth import create_api_key
from llm_wiki.web import create_web_app


def _seed(ctx, principals):
    docs, ed = ctx.docs, principals["editor"]
    docs.create(ed, "guide/intro.md",
                "---\ndescription: 시작 가이드 한 줄 설명\n---\n# Intro\n\n첫 문장입니다.\n", embed=False)
    docs.create(ed, "guide/deep.md", "# Deep\n\n본문 첫 줄\n\n둘째 단락\n", embed=False)
    docs.create(ed, "rootdoc.md", "# Root\n\n루트 문서 본문\n", embed=False)


# -- service: llms_index ---------------------------------------------------
def test_llms_index_structure(ctx, principals):
    _seed(ctx, principals)
    text = ctx.docs.llms_index(site_title="내 위키")
    lines = text.splitlines()
    assert lines[0] == "# 내 위키"
    assert any(ln.startswith("> ") for ln in lines)         # blockquote summary
    # H2 section per folder; root docs grouped under "루트"
    assert "## guide" in lines
    assert "## 루트" in lines
    # each doc is a markdown link to its raw (.md) source
    assert "- [Intro](/doc/guide/intro.md/raw): 시작 가이드 한 줄 설명" in text
    # no frontmatter description -> falls back to first body line
    assert "- [Deep](/doc/guide/deep.md/raw): 본문 첫 줄" in text


def test_llms_index_absolute_base_url(ctx, principals):
    _seed(ctx, principals)
    text = ctx.docs.llms_index(site_title="W", base_url="https://wiki.example/")
    assert "(https://wiki.example/doc/guide/intro.md/raw)" in text


def test_llms_index_empty_vault(ctx, principals):
    text = ctx.docs.llms_index(site_title="W")
    assert text.startswith("# W")
    assert "문서 0개" in text


# -- service: llms_full ----------------------------------------------------
def test_llms_full_concatenates_and_strips_frontmatter(ctx, principals):
    _seed(ctx, principals)
    res = ctx.docs.llms_full(site_title="W")
    assert res["included"] == 3 and res["total"] == 3 and res["truncated"] is False
    body = res["text"]
    # full bodies present, each with a path header, frontmatter YAML stripped out
    assert "- 경로: `guide/intro.md`" in body
    assert "첫 문장입니다." in body
    assert "description:" not in body          # frontmatter not echoed verbatim
    assert "루트 문서 본문" in body


def test_llms_full_truncates_on_budget(ctx, principals):
    _seed(ctx, principals)
    res = ctx.docs.llms_full(site_title="W", max_chars=10)
    # at least one doc always emitted, then truncation kicks in with a marker
    assert res["truncated"] is True
    assert 1 <= res["included"] < res["total"]
    assert "[truncated]" in res["text"]


# -- web routes: dual auth (session OR Bearer) -----------------------------
@pytest.fixture
def client(ctx, principals):
    return TestClient(create_web_app(ctx))


def _login(client):
    import re
    tok = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"username": "alice", "password": "secret12", "csrf_token": tok})


def test_llms_txt_requires_auth(client):
    r = client.get("/llms.txt")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_llms_txt_via_session(ctx, principals, client):
    _seed(ctx, principals)
    _login(client)
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "## guide" in r.text
    # absolute base_url derived from the request host
    assert "http://testserver/doc/guide/intro.md/raw" in r.text


def test_llms_full_via_bearer_api_key(ctx, principals, client):
    _seed(ctx, principals)
    key = create_api_key(ctx.db, principals["editor"].user_id, "agent")
    r = client.get("/llms-full.txt", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert "첫 문장입니다." in r.text
    assert "- 경로: `rootdoc.md`" in r.text


def test_llms_full_bad_bearer_rejected(client):
    r = client.get("/llms-full.txt", headers={"Authorization": "Bearer not-a-real-key"})
    assert r.status_code == 401


def test_raw_md_link_fetchable_by_bearer_agent(ctx, principals, client):
    # The whole point of /llms.txt: an agent that fetched the index with its API key
    # must be able to GET each linked raw (.md) with the SAME key (was session-only).
    _seed(ctx, principals)
    key = create_api_key(ctx.db, principals["editor"].user_id, "agent")
    hdr = {"Authorization": f"Bearer {key}"}
    idx = client.get("/llms.txt", headers=hdr)
    assert idx.status_code == 200
    raw = client.get("/doc/guide/intro.md/raw", headers=hdr)
    assert raw.status_code == 200
    assert "첫 문장입니다." in raw.text
    # without any credential the raw .md stays gated (redirect to login, not served)
    assert client.get("/doc/guide/intro.md/raw",
                      follow_redirects=False).status_code in (303, 401)


# -- MCP tool: export_corpus -----------------------------------------------
def _payload(out):
    blocks = out[0] if isinstance(out, tuple) else out
    return json.loads(blocks[0].text)


@pytest.fixture
def editor_mcp(ctx, principals, monkeypatch):
    key = create_api_key(ctx.db, principals["editor"].user_id, "agent")
    monkeypatch.setattr(mcp_mod, "_bearer_token", lambda _c: key)
    return create_mcp_server(ctx)


async def test_export_corpus_index_and_full(ctx, principals, editor_mcp):
    _seed(ctx, principals)
    idx = _payload(await editor_mcp.call_tool("export_corpus", {"format": "index"}))
    assert idx["ok"] and idx["format"] == "index"
    assert "## guide" in idx["text"]

    full = _payload(await editor_mcp.call_tool("export_corpus", {"format": "full"}))
    assert full["ok"] and full["format"] == "full"
    assert full["included"] == 3 and full["truncated"] is False
    assert "첫 문장입니다." in full["text"]


async def test_export_corpus_full_truncates(ctx, principals, editor_mcp):
    _seed(ctx, principals)
    # MCP enforces max_chars>=1000, so seed bodies past that to force truncation.
    big = "가" * 1500
    ctx.docs.create(principals["editor"], "big1.md", f"# Big1\n\n{big}\n", embed=False)
    ctx.docs.create(principals["editor"], "big2.md", f"# Big2\n\n{big}\n", embed=False)
    full = _payload(await editor_mcp.call_tool(
        "export_corpus", {"format": "full", "max_chars": 1000}))
    assert full["truncated"] is True
    assert full["included"] < full["total"]
