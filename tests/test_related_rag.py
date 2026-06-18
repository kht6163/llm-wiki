"""Related-documents (vector similarity) and the RAG ``assemble_context`` primitive.

Both reuse the existing chunk-vector index; ``docs.create`` embeds synchronously on
commit, so a freshly created doc is immediately discoverable here.
"""
import pytest

from llm_wiki import search
from llm_wiki.services.errors import NotFoundError, ValidationError


def _seed_topics(docs, p):
    docs.create(p, "ml.md", "# Machine Learning\n\nNeural networks and deep learning models "
                "are trained on large datasets to make predictions.")
    docs.create(p, "ai.md", "# Artificial Intelligence\n\nModern AI is powered by deep learning "
                "and neural networks trained on data.")
    docs.create(p, "cooking.md", "# Cooking\n\nHow to bake fresh sourdough bread in a home oven "
                "for a crisp crust.")


def test_related_ranks_similar_first_and_excludes_self(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    _seed_topics(docs, p)
    out = docs.related("ml.md", limit=5)["related"]
    paths = [r["path"] for r in out]
    assert "ml.md" not in paths              # never returns the source itself
    assert paths and paths[0] == "ai.md"     # the closest neighbor ranks first
    scores = [r["score"] for r in out]
    assert scores == sorted(scores, reverse=True)  # similarity, best first
    if "cooking.md" in paths:                # the unrelated note must rank below ai.md
        assert out[paths.index("ai.md")]["score"] > out[paths.index("cooking.md")]["score"]


def test_related_respects_limit(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    _seed_topics(docs, p)
    assert len(docs.related("ml.md", limit=1)["related"]) == 1


def test_related_empty_when_source_has_no_vectors(ctx):
    # A source with no chunk vectors (here: a non-existent doc id) yields no neighbors,
    # rather than raising.
    with ctx.db.reader() as conn:
        assert search.related_documents(conn, 10_000_000) == []


def test_related_missing_document_raises(ctx):
    with pytest.raises(NotFoundError):
        ctx.docs.related("does-not-exist.md")


def test_assemble_context_cites_sources_and_caps_budget(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    body = "Photosynthesis lets plants convert sunlight into chemical energy. " * 14
    docs.create(p, "bio.md", f"# Biology\n\n{body}")  # one ~900-char chunk (< 1200 cap)
    res = docs.assemble_context("photosynthesis energy", max_chars=200, max_sources=5)
    assert res["count"] >= 1
    assert res["sources"][0]["path"] == "bio.md"
    assert res["context"].startswith("[1] bio.md")           # citation marker + source path
    assert sum(s["chars"] for s in res["sources"]) <= 200    # passage budget honored
    assert res["truncated"] is True                          # the chunk outran the budget


def test_assemble_context_marker_count_matches_sources(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "alpha.md", "# Alpha\n\nThe alpha note talks about rivers and water flow.")
    docs.create(p, "beta.md", "# Beta\n\nThe beta note also discusses rivers and streams.")
    res = docs.assemble_context("rivers", mode="bm25", max_sources=5)
    assert res["count"] == len(res["sources"])
    for s in res["sources"]:
        assert f"[{s['n']}] {s['path']}" in res["context"]


def test_assemble_context_folder_filter(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "work/report.md", "quarterly sales report figures revenue")
    docs.create(p, "home/report.md", "home renovation report budget")
    res = docs.assemble_context("report", mode="bm25", folder="work")
    assert res["count"] >= 1
    assert all(s["path"].startswith("work/") for s in res["sources"])


def test_assemble_context_rejects_empty_question(ctx):
    with pytest.raises(ValidationError):
        ctx.docs.assemble_context("   ")


def test_assemble_context_honors_path_operator(ctx, principals):
    # The RAG primitive accepts the same title:/path:/has: operators as search, so an
    # agent can scope grounded context in one call (parity with search_documents).
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "work/report.md", "quarterly sales report figures revenue")
    docs.create(p, "home/report.md", "home renovation report budget plan")
    res = docs.assemble_context("report path:work/*", mode="bm25", max_sources=5)
    assert res["count"] >= 1
    assert all(s["path"].startswith("work/") for s in res["sources"])


def test_assemble_context_rejects_operator_only_question(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "x.md", "# X\n\nbody text here")
    with pytest.raises(ValidationError):
        docs.assemble_context("path:work/*", mode="bm25")


def test_assemble_context_expands_to_neighbour_chunks(ctx, principals):
    # A generous budget should pull neighbouring chunks around the matched one, so a
    # passage straddling a chunk boundary isn't cut in half (read_chunk-style expansion).
    body = ("# Doc\n\n## Alpha\n\n" + "alpha " * 60 + "\n\n## Beta\n\n" + "beta " * 60
            + "\n\n## Gamma\n\n" + "gamma " * 60)
    ctx.docs.create(principals["editor"], "exp.md", body)
    res = ctx.docs.assemble_context("beta", mode="bm25", max_chars=8000, max_sources=1)
    assert res["count"] == 1
    assert "beta" in res["context"]
    assert ("alpha" in res["context"]) or ("gamma" in res["context"])  # neighbour pulled in


def test_trim_to_budget_word_and_fence_boundaries():
    from llm_wiki.search import _trim_to_budget
    out, trunc = _trim_to_budget("alpha beta gamma delta", 12)
    assert trunc and out == "alpha beta"               # word boundary, not mid-word
    fenced, ftrunc = _trim_to_budget("```py\nprint(1)\n" + "x" * 50, 18)
    assert ftrunc and fenced.count("```") % 2 == 0      # never a half-open code fence


def test_embed_text_prepends_heading_path():
    from llm_wiki.indexing import _embed_text
    assert _embed_text({"text": "apt install", "heading_path": "Install > Linux"}) == \
        "Install > Linux\n\napt install"
    assert _embed_text({"text": "body", "heading_path": None}) == "body"
