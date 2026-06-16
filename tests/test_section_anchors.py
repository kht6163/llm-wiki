"""#9 heading-path-aware chunks: breadcrumbs + section anchors for search results."""
from llm_wiki.markdown_utils import chunk_markdown, heading_slug
from llm_wiki.search import search_page


def test_chunk_heading_path_breadcrumb():
    text = "# Install\n\nintro\n\n## Linux\n\nlinux body\n\n### apt\n\napt body here\n"
    chunks = chunk_markdown(text)
    by_heading = {c.heading: c.heading_path for c in chunks}
    assert by_heading["Install"] == "Install"
    assert by_heading["Linux"] == "Install > Linux"
    assert by_heading["apt"] == "Install > Linux > apt"


def test_heading_path_pops_back_up_a_level():
    text = "# A\n\nx\n\n## B\n\ny\n\n## C\n\nz\n"
    paths = {c.heading: c.heading_path for c in chunk_markdown(text)}
    assert paths["B"] == "A > B"
    assert paths["C"] == "A > C"  # C is a sibling of B, not nested under it


def test_heading_slug_matches_outline_rules():
    # Mirrors web/static/outline.js: lowercase, strip punctuation, spaces -> hyphens.
    assert heading_slug("Hello World") == "hello-world"
    assert heading_slug("Set up & Config!") == "set-up-config"
    assert heading_slug("  Spaced  Out  ") == "spaced-out"
    assert heading_slug("###") == "section"  # nothing slug-able -> fallback
    assert heading_slug("한글 제목") == "한글-제목"  # unicode word chars preserved


def test_search_result_carries_section_anchor(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "guide.md",
                "# Guide\n\nintro\n\n## Networking\n\nconfigure the proxy and firewall settings\n")
    results, _ = search_page(ctx.db, ctx.embedder, "proxy firewall", mode="vector", top_k=5)
    hit = next((r for r in results if r.path == "guide.md"), None)
    assert hit is not None
    assert hit.heading == "Networking"
    assert hit.heading_path == "Guide > Networking"
    assert hit.anchor == "networking"
