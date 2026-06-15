from llm_wiki.search import search
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


def test_search_folder_filter(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "work/report.md", "quarterly sales report")
    docs.create(p, "home/report.md", "home renovation report")
    res = search(ctx.db, ctx.embedder, "report", mode="bm25", top_k=10, folder="work")
    assert res and all(r.path.startswith("work/") for r in res)


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
