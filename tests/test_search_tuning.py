"""Configurable hybrid-search fusion tuning (RRF_K + candidate/vector over-fetch).

The knobs were promoted from hardcoded constants to Settings -> FusionParams, threaded
through search and the DocumentService. These guard the defaults (no behavior drift),
the validation bounds, and that the configured values actually reach the search path.
"""
import pydantic
import pytest

from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.search import DEFAULT_FUSION, FusionParams, search_page

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
