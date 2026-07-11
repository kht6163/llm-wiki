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


_FIXED_NOW = "2026-01-01T00:00:00Z"


def _insert_doc(
    conn, path, *, title=None, body="body", folder=None, deleted=False, tags=()
):
    doc_folder = path.rpartition("/")[0] if folder is None else folder
    content_hash = f"hash:{path}"
    cur = conn.execute(
        "INSERT INTO documents(path, path_norm, title, content_hash, folder, "
        "is_deleted, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            path,
            path.lower(),
            title or path,
            content_hash,
            doc_folder,
            deleted,
            _FIXED_NOW,
            _FIXED_NOW,
        ),
    )
    conn.execute(
        "INSERT INTO revisions(doc_id, version, body, title, content_hash, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (cur.lastrowid, 1, body, title or path, content_hash, _FIXED_NOW),
    )
    conn.executemany(
        "INSERT INTO tags(doc_id, tag) VALUES(?,?)",
        [(cur.lastrowid, tag) for tag in tags],
    )
    return cur.lastrowid


def _full_header(total, site_title="W"):
    return f"# {site_title}\n\n> 전체 코퍼스 export — 문서 {total}개.\n"


def _full_candidate(path, title, body, *, tags=()):
    tag_line = f"- 태그: {', '.join(sorted(tags))}\n" if tags else ""
    return (
        f"\n---\n\n# {title}\n\n"
        f"- 경로: `{path}`\n"
        f"{tag_line}"
        f"- 수정: {_FIXED_NOW}\n\n"
        f"{body.strip()}\n"
    )


def _full_marker(included, total):
    return (
        f"\n---\n\n> [truncated] {included}/{total} 문서만 포함되었습니다. "
        "나머지는 /llms.txt 색인이나 개별 문서로 가져오세요.\n"
    )


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


def test_llms_index_escapes_markdown_label(ctx, principals):
    ctx.docs.create(
        principals["editor"], "odd.md", "body", title="A [B] \\", embed=False
    )
    text = ctx.docs.llms_index(site_title="W")
    assert r"[A \[B\] \\](/doc/odd.md/raw)" in text


def test_llms_index_batches_129_rows_and_loads_current_bodies_with_one_join(
    ctx, monkeypatch
):
    with ctx.db.writer() as conn:
        ids = [
            _insert_doc(
                conn,
                f"batch/{i:03}.md",
                title=f"Doc {i:03}",
                body=f"body {i:03}",
                tags=(f"tag-{i:03}",),
            )
            for i in range(129)
        ]
        conn.execute(
            "UPDATE documents SET version=2 WHERE id=?",
            (ids[-1],),
        )
        conn.execute(
            "INSERT INTO revisions(doc_id, version, body, title, content_hash, created_at) "
            "VALUES(?,2,?,?,?,?)",
            (ids[-1], "latest body 128", "Doc 128", "hash:latest", _FIXED_NOW),
        )

    tag_batch_sizes = []
    original_tags_for_ids = ctx.docs._tags_for_ids

    def record_tag_batch(conn, ids):
        tag_batch_sizes.append(len(ids))
        return original_tags_for_ids(conn, ids)

    monkeypatch.setattr(ctx.docs, "_tags_for_ids", record_tag_batch)
    statements = []
    with ctx.db.reader() as conn:
        conn.set_trace_callback(statements.append)
        try:
            text = ctx.docs.llms_index(site_title="W")
        finally:
            conn.set_trace_callback(None)

    body_queries = [
        sql for sql in statements
        if "revisions" in sql.lower() and "body" in sql.lower()
    ]
    assert "latest body 128" in text
    assert "): body 128\n" not in text
    assert tag_batch_sizes == [128, 1]
    assert len(body_queries) == 1
    assert "join revisions" in " ".join(body_queries[0].lower().split())
    assert not any(
        "select body from revisions where doc_id=" in " ".join(sql.lower().split())
        for sql in statements
    )


def test_corpus_helpers_filter_folder_subtree_deleted_rows_and_sort(ctx):
    with ctx.db.writer() as conn:
        _insert_doc(conn, "root-z.md")
        _insert_doc(conn, "alpha/z.md")
        _insert_doc(conn, "alpha/a.md")
        _insert_doc(conn, "alpha/nested/b.md")
        _insert_doc(conn, "beta/a.md")
        _insert_doc(conn, "alpha/deleted.md", deleted=True)

    with ctx.db.reader() as conn:
        assert ctx.docs._corpus_count("/alpha/", conn=conn) == 3
        alpha = list(ctx.docs._iter_corpus_docs("/alpha/", conn=conn))
        all_docs = list(ctx.docs._iter_corpus_docs(conn=conn))

    assert [doc["path"] for doc in alpha] == [
        "alpha/a.md",
        "alpha/z.md",
        "alpha/nested/b.md",
    ]
    assert [doc["path"] for doc in all_docs] == [
        "root-z.md",
        "alpha/a.md",
        "alpha/z.md",
        "alpha/nested/b.md",
        "beta/a.md",
    ]


def test_llms_index_normalizes_labels_and_description_without_changing_url(ctx):
    with ctx.db.writer() as conn:
        _insert_doc(
            conn,
            "odd [x].md",
            title="Title [x] \\\n Next",
            folder="Folder [x] \\\n Next",
            body="---\ndescription: first\t  second\n---\nbody",
        )

    text = ctx.docs.llms_index(site_title="Site [x] \\\n Next")

    assert text.startswith("# Site \\[x\\] \\\\ Next\n")
    assert "## Folder \\[x\\] \\\\ Next\n" in text
    assert (
        "- [Title \\[x\\] \\\\ Next](/doc/odd%20%5Bx%5D.md/raw): first second"
        in text
    )
    full = ctx.docs.llms_full(site_title="Site [x] \\\n Next")
    assert full["text"].startswith("# Site \\[x\\] \\\\ Next\n")
    assert "\n# Title \\[x\\] \\\\ Next\n" in full["text"]
    assert "- 경로: `odd [x].md`" in full["text"]


@pytest.mark.parametrize("export_name", ["llms_index", "llms_full"])
def test_corpus_export_keeps_count_and_rows_in_one_snapshot(
    ctx, monkeypatch, export_name
):
    with ctx.db.writer() as conn:
        _insert_doc(conn, "before.md", title="Before", body="before body")

    original = ctx.docs._iter_corpus_docs

    def interleaved(folder=None, batch_size=128, conn=None):
        writer = ctx.db.connect()
        try:
            writer.execute("BEGIN IMMEDIATE")
            _insert_doc(writer, "after.md", title="After", body="after body")
            writer.execute("COMMIT")
        finally:
            writer.close()
        if conn is None:
            yield from original(folder, batch_size)
        else:
            yield from original(folder, batch_size, conn=conn)

    monkeypatch.setattr(ctx.docs, "_iter_corpus_docs", interleaved)
    result = getattr(ctx.docs, export_name)(site_title="W")

    if export_name == "llms_index":
        assert "문서 1개" in result
        assert "[Before]" in result
        assert "[After]" not in result
    else:
        assert result["total"] == result["included"] == 1
        assert "before body" in result["text"]
        assert "after body" not in result["text"]


def test_corpus_snapshot_ends_owned_transaction_on_success_and_error(ctx):
    with ctx.docs._corpus_read_snapshot() as conn:
        assert conn.in_transaction
    with ctx.db.reader() as conn:
        assert not conn.in_transaction

    with pytest.raises(RuntimeError, match="stop"):
        with ctx.docs._corpus_read_snapshot() as conn:
            assert conn.in_transaction
            raise RuntimeError("stop")
    with ctx.db.reader() as conn:
        assert not conn.in_transaction


def test_corpus_snapshot_does_not_finish_caller_transaction(ctx):
    with ctx.db.writer() as conn:
        _insert_doc(conn, "before.md", title="Before")
        with pytest.raises(RuntimeError, match="caller error"):
            with ctx.docs._corpus_read_snapshot() as snapshot_conn:
                assert snapshot_conn is conn
                raise RuntimeError("caller error")
        assert conn.in_transaction
        assert "[Before]" in ctx.docs.llms_index(site_title="W")
        assert conn.in_transaction
        _insert_doc(conn, "after.md", title="After")
    assert ctx.docs._corpus_count() == 2


# -- service: llms_full ----------------------------------------------------
def test_llms_full_empty_vault_preserves_text_format(ctx):
    res = ctx.docs.llms_full(site_title="W")
    assert res == {
        "text": "# W\n\n> 전체 코퍼스 export — 문서 0개.\n",
        "included": 0,
        "total": 0,
        "truncated": False,
    }


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


def test_llms_full_populated_text_preserves_exact_newline_format(ctx):
    with ctx.db.writer() as conn:
        _insert_doc(
            conn,
            "a.md",
            title="A",
            body="body",
            tags=("beta", "alpha"),
        )

    res = ctx.docs.llms_full(site_title="W")

    assert res == {
        "text": _full_header(1) + _full_candidate(
            "a.md", "A", "body", tags=("beta", "alpha")
        ),
        "included": 1,
        "total": 1,
        "truncated": False,
    }


def test_llms_full_truncates_on_budget(ctx, principals):
    _seed(ctx, principals)
    res = ctx.docs.llms_full(site_title="W", max_chars=10)
    assert res["truncated"] is True
    assert res["included"] < res["total"]
    assert len(res["text"]) <= 10


def test_llms_full_hard_caps_single_large_document(ctx, principals):
    with ctx.db.writer() as conn:
        _insert_doc(conn, "big.md", title="Big", body="가" * 5000)
    res = ctx.docs.llms_full(site_title="W", max_chars=1000)
    assert len(res["text"]) == 1000
    assert res["included"] == res["total"] == 1
    assert res["truncated"] is True
    assert res["text"].endswith(_full_marker(1, 1))


def test_llms_full_budget_boundaries(ctx):
    body = "x" * 200
    with ctx.db.writer() as conn:
        _insert_doc(conn, "a.md", title="A", body=body)

    header = _full_header(1)
    candidate = _full_candidate("a.md", "A", body)
    marker_0 = _full_marker(0, 1)
    marker_1 = _full_marker(1, 1)
    full_text = header + candidate
    cases = [
        ("zero", 0, "", 0, True),
        ("header-minus-one", len(header) - 1, header[:-1], 0, True),
        ("exact-header", len(header), header, 0, True),
        (
            "marker-prefix",
            len(header) + 8,
            header + marker_0[:8],
            0,
            True,
        ),
        (
            "below-first-partial",
            len(header) + len(marker_0) + 1,
            header + marker_0,
            0,
            True,
        ),
        (
            "first-partial",
            len(header) + len(marker_1) + 2,
            header + candidate[:2] + marker_1,
            1,
            True,
        ),
        (
            "one-short-of-fit",
            len(full_text) - 1,
            header + candidate[: len(candidate) - len(marker_1) - 1] + marker_1,
            1,
            True,
        ),
        ("exact-fit", len(full_text), full_text, 1, False),
    ]

    for case, budget, expected_text, included, truncated in cases:
        result = ctx.docs.llms_full(site_title="W", max_chars=budget)
        assert len(result["text"]) <= budget, case
        assert result == {
            "text": expected_text,
            "included": included,
            "total": 1,
            "truncated": truncated,
        }, case


def test_llms_full_two_document_boundary(ctx):
    with ctx.db.writer() as conn:
        _insert_doc(conn, "a.md", title="A", body="first")
        _insert_doc(conn, "b.md", title="B", body="second" * 100)

    header = _full_header(2)
    first = _full_candidate("a.md", "A", "first")
    second = _full_candidate("b.md", "B", "second" * 100)
    marker = _full_marker(1, 2)
    first_boundary = len(header) + len(first)

    without_marker = ctx.docs.llms_full(site_title="W", max_chars=first_boundary)
    assert len(without_marker["text"]) <= first_boundary
    assert without_marker == {
        "text": header + first,
        "included": 1,
        "total": 2,
        "truncated": True,
    }

    marker_boundary = first_boundary + len(marker)
    with_marker = ctx.docs.llms_full(site_title="W", max_chars=marker_boundary)
    assert len(with_marker["text"]) <= marker_boundary
    assert with_marker == {
        "text": header + first + marker,
        "included": 1,
        "total": 2,
        "truncated": True,
    }

    full_text = header + first + second
    exact_fit = ctx.docs.llms_full(site_title="W", max_chars=len(full_text))
    assert len(exact_fit["text"]) <= len(full_text)
    assert exact_fit == {
        "text": full_text,
        "included": 2,
        "total": 2,
        "truncated": False,
    }

    marker_2 = _full_marker(2, 2)
    one_short_budget = len(full_text) - 1
    one_short = ctx.docs.llms_full(site_title="W", max_chars=one_short_budget)
    assert len(one_short["text"]) <= one_short_budget
    assert one_short == {
        "text": (
            header
            + first
            + second[: len(second) - len(marker_2) - 1]
            + marker_2
        ),
        "included": 2,
        "total": 2,
        "truncated": True,
    }


def test_llms_full_clamps_negative_string_budget_to_zero(ctx, principals):
    _seed(ctx, principals)
    res = ctx.docs.llms_full(site_title="W", max_chars="-3")
    assert res == {"text": "", "included": 0, "total": 3, "truncated": True}


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
