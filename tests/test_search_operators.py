"""In-query search operators (title:/path:/has:) and result-triage metadata
(content_length/section_depth). The operators let an agent express a precise query in
ONE call instead of post-filtering a broad result set; the metadata lets it triage a
hit (short overview vs long reference, top-level vs deep section) without a follow-up
read."""
import pytest

from llm_wiki.search import parse_query_filters
from llm_wiki.services.errors import ValidationError


# -- parser -----------------------------------------------------------------
def test_parse_extracts_operators_and_free_text():
    text, f = parse_query_filters("neural nets title:Intro path:ml/* has:link")
    assert text == "neural nets"
    assert f.title_contains == ("Intro",)
    assert f.path_specs == (("ml/*", True),)
    assert f.has == ("link",)
    assert f.active


def test_parse_quoted_value_with_spaces():
    text, f = parse_query_filters('design title:"design system"')
    assert text == "design"
    assert f.title_contains == ("design system",)


def test_parse_path_substring_vs_glob():
    _t, sub = parse_query_filters("x path:guide")
    assert sub.path_specs == (("guide", False),)   # no wildcard -> substring
    _t, glob = parse_query_filters("x path:docs/*")
    assert glob.path_specs == (("docs/*", True),)


def test_parse_unknown_has_rejected():
    with pytest.raises(ValidationError):
        parse_query_filters("x has:bogus")


def test_parse_no_operators_is_passthrough():
    text, f = parse_query_filters("just a normal query")
    assert text == "just a normal query"
    assert not f.active


# -- operators end-to-end through search_page -------------------------------
def test_title_operator_filters_by_title(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "alpha.md", "# Apple Guide\n\norchard fruit harvest")
    docs.create(p, "beta.md", "# Banana\n\norchard fruit harvest too")
    res, _ = docs.search_page("orchard title:Apple", mode="bm25", top_k=10)
    paths = {r.path for r in res}
    assert "alpha.md" in paths and "beta.md" not in paths


def test_path_glob_operator(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "ml/intro.md", "# Intro\n\ngradient descent method")
    docs.create(p, "ops/intro.md", "# Intro\n\ngradient descent method")
    res, _ = docs.search_page("gradient path:ml/*", mode="bm25", top_k=10)
    paths = {r.path for r in res}
    assert "ml/intro.md" in paths and "ops/intro.md" not in paths


def test_path_substring_operator(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "ml/guide.md", "# G\n\nwidget how-to")
    docs.create(p, "ops/runbook.md", "# R\n\nwidget how-to")
    res, _ = docs.search_page("widget path:guide", mode="bm25", top_k=10)
    paths = {r.path for r in res}
    assert "ml/guide.md" in paths and "ops/runbook.md" not in paths


def test_has_tag_operator(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "tagged.md", "---\ntags: [project]\n---\n# T\n\nwidget content here")
    docs.create(p, "plain.md", "# P\n\nwidget content here")
    res, _ = docs.search_page("widget has:tag", mode="bm25", top_k=10)
    paths = {r.path for r in res}
    assert "tagged.md" in paths and "plain.md" not in paths


def test_operator_only_query_rejected(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "x.md", "# X\n\nbody text")
    with pytest.raises(ValidationError):
        docs.search_page("title:X", mode="bm25", top_k=5)


def test_unknown_has_rejected_through_search(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "x.md", "# X\n\nwidget body")
    with pytest.raises(ValidationError):
        docs.search_page("widget has:nope", mode="bm25", top_k=5)


# -- triage metadata --------------------------------------------------------
def test_content_length_orders_short_below_long(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "short.md", "# Short\n\nzebra overview")
    docs.create(p, "long.md", "# Long\n\nzebra " + ("padding words here " * 80))
    res, _ = docs.search_page("zebra", mode="bm25", top_k=10)
    by_path = {r.path: r for r in res}
    assert by_path["short.md"].content_length > 0
    assert by_path["long.md"].content_length > by_path["short.md"].content_length


def test_section_depth_matches_heading_path_contract(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "z.md", "# Top\n\n## Sub\n\nzebra deep content under a subsection")
    res, _ = docs.search_page("zebra", mode="bm25", top_k=5)
    hit = next(r for r in res if r.path == "z.md")
    # section_depth is derived from heading_path: it's the breadcrumb segment count, or
    # None when the hit carries no section.
    if hit.heading_path:
        assert hit.section_depth == hit.heading_path.count(" > ") + 1
        assert hit.section_depth >= 1
    else:
        assert hit.section_depth is None
