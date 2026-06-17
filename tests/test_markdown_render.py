"""Tests for Obsidian-flavored markdown rendering: callouts, task checkboxes,
==highlight==, and that sanitization + code-region protection still hold."""
from llm_wiki.markdown_render import render_markdown
from llm_wiki.markdown_utils import document_properties


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
