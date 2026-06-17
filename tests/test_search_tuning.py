"""Configurable hybrid-search fusion tuning (RRF_K + candidate/vector over-fetch).

The knobs were promoted from hardcoded constants to Settings -> FusionParams, threaded
through search and the DocumentService. These guard the defaults (no behavior drift),
the validation bounds, and that the configured values actually reach the search path.
"""
import pydantic
import pytest

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.search import DEFAULT_FUSION, FusionParams, _rerank_boost, search_page

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def test_fusion_defaults_match_legacy_constants():
    # Defaults must equal the values that were hardcoded before this change, so the
    # out-of-the-box ranking is unchanged.
    assert (DEFAULT_FUSION.rrf_k, DEFAULT_FUSION.candidate_factor, DEFAULT_FUSION.candidate_min,
            DEFAULT_FUSION.vector_factor, DEFAULT_FUSION.vector_cap) == (60, 4, 40, 3, 600)


@pytest.mark.parametrize("field,bad", [
    ("rrf_k", 0), ("rrf_k", 5000),
    ("search_candidate_factor", 0), ("search_candidate_factor", 999),
    ("search_candidate_min", 0),
    ("search_vector_factor", 0),
    ("search_vector_cap", 0),
])
def test_settings_rejects_out_of_range_tuning(field, bad):
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, session_secret="x", **{field: bad})


def test_settings_accepts_in_range_tuning():
    s = Settings(_env_file=None, session_secret="x", rrf_k=20, search_candidate_factor=6,
                 search_candidate_min=10, search_vector_factor=2, search_vector_cap=300)
    assert (s.rrf_k, s.search_candidate_factor, s.search_vector_cap) == (20, 6, 300)


def test_build_context_threads_tuning_to_service(tmp_path):
    s = Settings(
        _env_file=None, vault_path=tmp_path / "v", db_path=tmp_path / "d" / "wiki.db",
        embedding_model=TEST_MODEL, gui_port=8090, mcp_port=8091, session_secret="x",
        rrf_k=12, search_candidate_factor=2, search_candidate_min=5,
        search_vector_factor=2, search_vector_cap=100)
    ctx = build_context(s, full=True)
    fp = ctx.docs.search_params
    assert (fp.rrf_k, fp.candidate_factor, fp.candidate_min, fp.vector_factor,
            fp.vector_cap) == (12, 2, 5, 2, 100)


def test_service_search_page_returns_results(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "# Apple\n\napple orchard fruit harvest")
    docs.create(p, "b.md", "# Banana\n\nbanana tropical yellow")
    res, _trunc = docs.search_page("apple", mode="bm25", top_k=5)
    assert any(r.path == "a.md" for r in res)
    assert ctx.docs.search_params == DEFAULT_FUSION  # default ctx uses the default tuning


def test_module_search_page_accepts_custom_fusion_params(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "# Apple\n\napple apple orchard")
    fp = FusionParams(rrf_k=5, candidate_factor=2, candidate_min=10, vector_factor=2, vector_cap=50)
    res, _ = search_page(ctx.db, ctx.embedder, "apple", mode="hybrid", top_k=3, params=fp)
    assert any(r.path == "a.md" for r in res)


# -- lightweight reranking ------------------------------------------------------

def test_fusion_rerank_defaults():
    # Title boosts are on by default (exact > prefix); proximity is opt-in (0).
    assert DEFAULT_FUSION.title_exact_boost == 0.05
    assert DEFAULT_FUSION.title_prefix_boost == 0.015
    assert DEFAULT_FUSION.proximity_weight == 0.0


def test_rerank_boost_rewards_exact_then_prefix_then_nothing():
    p = DEFAULT_FUSION
    assert _rerank_boost("Rate Limiter", None, "rate limiter", p) == p.title_exact_boost
    assert _rerank_boost("Rate Limiter Design", None, "rate limiter", p) == p.title_prefix_boost
    assert _rerank_boost("Unrelated Note", None, "rate limiter", p) == 0.0
    assert _rerank_boost("Anything", None, "", p) == 0.0          # empty query never boosts
    # Proximity is off by default even with a close vector hit...
    assert _rerank_boost("x", {"distance": 0.1}, "q", p) == 0.0
    # ...and when enabled adds weight * cosine-similarity (1 - distance).
    pp = FusionParams(title_exact_boost=0.0, title_prefix_boost=0.0, proximity_weight=0.2)
    assert _rerank_boost(None, {"distance": 0.25}, "q", pp) == pytest.approx(0.2 * 0.75)


def test_exact_title_match_is_reranked_to_the_top(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    # guide.md matches the query only via its title; notes.md matches heavily in the body
    # and so out-ranks guide on pure BM25/RRF.
    docs.create(p, "guide.md", "# Apple\n\nA short fruit guide with no other terms.")
    docs.create(p, "notes.md", "# Notes\n\napple apple apple apple apple apple orchard")
    off = FusionParams(title_exact_boost=0.0, title_prefix_boost=0.0)  # pure rank-only RRF
    off_res, _ = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=5, params=off)
    on_res, _ = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=5, params=DEFAULT_FUSION)
    off_score = {r.path: r.score for r in off_res}
    on_score = {r.path: r.score for r in on_res}
    # The exact-title doc gains exactly the boost; the body-only doc is unchanged...
    assert on_score["guide.md"] == pytest.approx(off_score["guide.md"] + DEFAULT_FUSION.title_exact_boost)
    assert on_score["notes.md"] == pytest.approx(off_score["notes.md"])
    # ...which lifts it to the top under the default reranking.
    assert on_res[0].path == "guide.md"


def test_settings_rejects_out_of_range_rerank_weights():
    for field, bad in (("search_title_exact_boost", -0.1), ("search_title_exact_boost", 11.0),
                       ("search_proximity_weight", -1.0)):
        with pytest.raises(pydantic.ValidationError):
            Settings(_env_file=None, session_secret="x", **{field: bad})


def test_build_context_threads_rerank_knobs(tmp_path):
    s = Settings(
        _env_file=None, vault_path=tmp_path / "v", db_path=tmp_path / "d" / "wiki.db",
        embedding_model=TEST_MODEL, gui_port=8092, mcp_port=8093, session_secret="x",
        search_title_exact_boost=0.2, search_title_prefix_boost=0.1, search_proximity_weight=0.3)
    ctx = build_context(s, full=True)
    fp = ctx.docs.search_params
    assert (fp.title_exact_boost, fp.title_prefix_boost, fp.proximity_weight) == (0.2, 0.1, 0.3)
