"""Tests for Obsidian-flavored markdown rendering: callouts, task checkboxes,
==highlight==, and that sanitization + code-region protection still hold."""
from llm_wiki.markdown_render import render_markdown


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
