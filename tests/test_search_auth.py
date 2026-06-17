from llm_wiki.search import search, search_page
from llm_wiki.services.auth import (
    authenticate,
    create_api_key,
    principal_from_api_key,
)


def test_hybrid_search_finds_doc(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "python.md", "# Python\n\nPython is a programming language for data science.")
    docs.create(p, "cooking.md", "# Cooking\n\nHow to bake bread at home.")

    res = search(ctx.db, ctx.embedder, "programming language", mode="hybrid", top_k=5)
    assert res, "expected at least one hybrid result"
    assert res[0].path == "python.md"

    res_bm = search(ctx.db, ctx.embedder, "bread", mode="bm25", top_k=5)
    assert any(r.path == "cooking.md" for r in res_bm)

    res_vec = search(ctx.db, ctx.embedder, "software development", mode="vector", top_k=5)
    assert any(r.path == "python.md" for r in res_vec)


def test_fts_body_excludes_frontmatter(ctx, principals):
    # The BM25 snippet leg must not leak `tags: [...]` / `---` from frontmatter;
    # only body prose is indexed (title is a separate column, tags a separate table).
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "fm.md", "---\ntitle: 제목\ntags: [alpha, beta]\n---\n\n본문 내용입니다\n")
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT f.body FROM documents_fts f JOIN documents d ON d.id = f.rowid "
            "WHERE d.path_norm = ?",
            ("fm.md",),
        ).fetchone()
    body = row[0]
    assert "본문 내용입니다" in body
    assert "tags:" not in body
    assert "alpha" not in body
    assert "---" not in body


def test_search_folder_filter(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "work/report.md", "quarterly sales report")
    docs.create(p, "home/report.md", "home renovation report")
    res = search(ctx.db, ctx.embedder, "report", mode="bm25", top_k=10, folder="work")
    assert res and all(r.path.startswith("work/") for r in res)


def test_search_tag_filter_requires_all_tags(ctx, principals):
    # Exercises the batch-loaded tag filter path: only docs carrying ALL tags pass.
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "a.md", "shared report content", tags=["release", "todo"])
    docs.create(p, "b.md", "shared report content", tags=["release"])
    res, _ = search_page(ctx.db, ctx.embedder, "report content", mode="hybrid",
                         top_k=10, tags=["release", "todo"])
    paths = {r.path for r in res}
    assert "a.md" in paths and "b.md" not in paths


def test_bm25_folder_filter_pushed_into_sql(ctx, principals):
    # The folder filter must constrain the candidate LIMIT, not post-filter a fixed
    # top-k. With a small limit, a folder match that ranks LAST must still be found.
    from llm_wiki.search import _bm25, _fts_match
    docs, p = ctx.docs, principals["editor"]
    for i in range(3):
        docs.create(p, f"other/o{i}.md", "report report report")  # high tf -> ranks higher
    docs.create(p, "work/w.md", "report")  # low tf -> would fall outside a small window
    with ctx.db.reader() as conn:
        rows = _bm25(conn, _fts_match("report"), 2, folder="work")
        paths = [conn.execute("SELECT path FROM documents WHERE id=?", (did,)).fetchone()["path"]
                 for did, _ in rows]
    assert paths == ["work/w.md"]  # only the folder match, recovered despite ranking last


def test_search_page_truncation_is_exact(ctx, principals):
    # A corpus of exactly top_k matches must report truncated=False (no misleading
    # 'raise top_k'); fewer slots than matches reports True.
    docs, p = ctx.docs, principals["editor"]
    for i in range(3):
        docs.create(p, f"k{i}.md", "shared keyword apple here")
    res, trunc = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=3)
    assert len(res) == 3 and trunc is False
    res2, trunc2 = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=2)
    assert len(res2) == 2 and trunc2 is True


def test_password_and_api_key_auth(ctx, principals):
    # password auth
    assert authenticate(ctx.db, "alice", "secret12") is not None
    assert authenticate(ctx.db, "alice", "wrong") is None

    # api key auth round-trip
    token = create_api_key(ctx.db, principals["viewer"].user_id, "test-key")
    pr = principal_from_api_key(ctx.db, token)
    assert pr is not None and pr.username == "bob" and pr.role == "viewer"
    assert pr.can_write is False

    assert principal_from_api_key(ctx.db, "lw_bogustokenvalue") is None
    assert principal_from_api_key(ctx.db, None) is None


def test_reindex_external_edit(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "ext.md", "original")
    # simulate an external editor writing directly to the vault
    (ctx.settings.vault_path / "ext.md").write_text("externally changed", encoding="utf-8")
    (ctx.settings.vault_path / "new_external.md").write_text("# Brand New\n\nhello", encoding="utf-8")

    res = docs.reindex_all()
    assert res["created"] >= 1  # new_external.md
    assert res["updated"] >= 1  # ext.md changed
    assert "externally changed" in docs.get("ext.md")["content"]
    assert docs.get("new_external.md")["title"] == "Brand New"
