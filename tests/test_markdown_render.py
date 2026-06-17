"""Tests for Obsidian-flavored markdown rendering: callouts, task checkboxes,
==highlight==, note embeds (transclusion), and that sanitization + code-region
protection still hold."""
from llm_wiki.markdown_render import render_markdown
from llm_wiki.markdown_utils import (
    document_properties,
    remove_frontmatter_property,
    section_text,
    set_frontmatter_property,
)


def _resolver(docs):
    """Build a render_markdown embed resolver over an in-memory {name: (title, body)}."""
    def resolve(target):
        if target in docs:
            title, body = docs[target]
            return {"path": target + ".md", "title": title, "content": body}
        return None
    return resolve


def test_frontmatter_is_stripped_not_rendered_as_heading():
    # Left in, CommonMark turns `---` into <hr> and the key lines + closing `---`
    # into a setext <h2>. The renderer must strip the block entirely.
    html = render_markdown("---\ntitle: 문서\ntags: [a, b]\n---\n\n# 본문 제목\n\n단락\n")
    assert "title: 문서" not in html
    assert "tags:" not in html
    assert "<hr" not in html  # the opening --- must not survive as a rule
    assert "본문 제목" in html  # the real body still renders


def test_render_without_frontmatter_is_unchanged():
    html = render_markdown("# 그냥 제목\n\n본문\n")
    assert "그냥 제목" in html and "본문" in html


def test_document_properties_excludes_title_and_tags():
    props = document_properties(
        "---\ntitle: T\ntags: [x]\naliases: [별명1, 별명2]\nstatus: draft\n---\n본문"
    )
    keys = [k for k, _ in props]
    assert "title" not in keys and "tags" not in keys
    assert ("aliases", ["별명1", "별명2"]) in props
    assert ("status", ["draft"]) in props


def test_document_properties_empty_when_only_title_tags():
    assert document_properties("---\ntitle: T\ntags: [x]\n---\n본문") == []
    assert document_properties("# 프론트매터 없음\n본문") == []


def test_callout_renders_typed_box_with_title():
    html = render_markdown("> [!info] 제목\n> 본문\n")
    assert 'class="callout callout-info"' in html
    assert '<div class="callout-title">제목</div>' in html
    assert "본문" in html


def test_callout_without_title_uses_default_korean_title():
    html = render_markdown("> [!warning]\n> 조심\n")
    assert 'class="callout callout-warning"' in html
    assert "경고" in html  # default title for 'warning'


def test_plain_blockquote_is_not_a_callout():
    html = render_markdown("> just a quote\n")
    assert "callout" not in html
    assert "<blockquote>" in html


def test_task_checkboxes_get_index_and_checked_state():
    html = render_markdown("- [ ] todo one\n- [x] done two\n")
    assert '<input type="checkbox" data-ti="0" disabled>' in html
    assert 'data-ti="1" disabled checked' in html
    assert 'class="task-item"' in html


def test_highlight_renders_mark_but_preserves_code():
    html = render_markdown("==강조== 와 `==코드==` 보존\n")
    assert "<mark>강조</mark>" in html
    assert "<code>==코드==</code>" in html  # untouched inside code


def test_render_still_sanitizes_script():
    html = render_markdown("<script>alert(1)</script>\n\n> [!info] x\n> y\n")
    assert "<script>" not in html
    assert 'class="callout callout-info"' in html


# ---- note embeds (transclusion) -------------------------------------------
def test_embed_expands_target_body_with_resolver():
    res = _resolver({"회의록": ("회의록", "# 회의록\n\n결정 본문\n")})
    html = render_markdown("앞 문단\n\n![[회의록]]\n", "a.md", resolve_embed=res)
    assert 'class="embed' in html and "embed-body" in html
    assert "결정 본문" in html
    assert '/doc/' in html  # the embed title links to the note


def test_embed_section_extracts_only_that_heading():
    body = "# T\n\n## 결정사항\n결정 본문\n\n## 기타\n기타 본문\n"
    res = _resolver({"T": ("T", body)})
    html = render_markdown("![[T#결정사항]]\n", "a.md", resolve_embed=res)
    assert "결정 본문" in html
    assert "기타 본문" not in html


def test_embed_missing_document_shows_missing_box_not_broken_image():
    res = _resolver({})
    html = render_markdown("![[없는문서]]\n", "a.md", resolve_embed=res)
    assert "embed-missing" in html
    assert "<img" not in html  # the ![[ ]] must NOT fall through to image syntax


def test_embed_missing_section_is_flagged():
    res = _resolver({"T": ("T", "# T\n\n## 있는섹션\nx\n")})
    html = render_markdown("![[T#없는섹션]]\n", "a.md", resolve_embed=res)
    assert "embed-missing" in html


def test_embed_without_resolver_renders_as_link_not_image():
    # Plain render (no DB context, e.g. a snippet preview) keeps the embed as a link.
    html = render_markdown("![[노트]]\n", "a.md")
    assert "<img" not in html
    assert "/go?" in html and ">노트<" in html


def test_embed_cycle_is_guarded():
    res = _resolver({"Self": ("Self", "루프 ![[Self]] 끝")})
    html = render_markdown("![[Self]]\n", "x.md", resolve_embed=res)
    assert "embed-cycle" in html  # the self-reference is refused, not infinitely expanded


def test_embed_inside_code_fence_stays_literal():
    res = _resolver({"노트": ("노트", "본문")})
    html = render_markdown("```\n![[노트]]\n```\n", "a.md", resolve_embed=res)
    assert "embed-body" not in html
    assert "![[노트]]" in html  # literal inside the code block


# ---- frontmatter property helpers -----------------------------------------
def test_set_frontmatter_property_scalar_and_list():
    c = "---\ntitle: T\nstatus: draft\n---\n본문\n"
    assert "status: done" in set_frontmatter_property(c, "status", "done")
    assert "aliases: [별명1, 별명2]" in set_frontmatter_property(c, "aliases", ["별명1", "별명2"])
    # other keys + body preserved
    out = set_frontmatter_property(c, "status", "done")
    assert "title: T" in out and "본문" in out


def test_set_frontmatter_property_creates_block_when_absent():
    out = set_frontmatter_property("그냥 본문", "status", "draft")
    assert out.startswith("---\n") and "status: draft" in out and "그냥 본문" in out


def test_remove_frontmatter_property_drops_key_and_empty_block():
    assert "status" not in remove_frontmatter_property("---\nstatus: draft\n---\n본문\n", "status")
    # removing the only key drops the whole block
    assert remove_frontmatter_property("---\nstatus: draft\n---\n본문\n", "status").lstrip().startswith("본문")


def test_section_text_returns_section_until_next_same_level_heading():
    body = "# T\n\n## A\n에이\n### A1\n하위\n## B\n비\n"
    sec = section_text(body, "A")
    assert "에이" in sec and "하위" in sec  # includes deeper subsection
    assert "비" not in sec                  # stops at the next ## heading
    assert section_text(body, "없음") is None
