"""Render markdown to safe HTML for the web viewer, with Obsidian-style
``[[wikilink]]`` support. Wikilinks become links to a ``/go`` resolver route so
rendering needs no database access. Output is sanitized with bleach.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import bleach
from markdown_it import MarkdownIt

from .markdown_utils import WIKILINK_RE

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    .enable("table")
    .enable("strikethrough")
)

_ALLOWED_TAGS = set(bleach.sanitizer.ALLOWED_TAGS) | {
    "p", "pre", "hr", "br", "span", "mark", "del", "ins", "sup", "sub",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "blockquote", "div", "input",
}
_ALLOWED_ATTRS = {
    "a": ["href", "title", "class"],
    "img": ["src", "alt", "title"],
    "span": ["class"],
    "code": ["class"],
    "div": ["class"],
    "li": ["class"],
    "input": ["type", "checked", "disabled", "data-ti"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

# Obsidian callout types -> Korean default titles. The CSS keys off callout-<type>.
_CALLOUT_TITLES = {
    "note": "노트", "info": "정보", "tip": "팁", "hint": "팁", "important": "중요",
    "success": "성공", "check": "성공", "done": "성공", "question": "질문", "faq": "질문",
    "warning": "경고", "caution": "주의", "attention": "주의", "danger": "위험",
    "error": "오류", "bug": "버그", "quote": "인용", "example": "예시", "abstract": "요약",
}
_CALLOUT_RE = re.compile(
    r"<blockquote>\s*<p>\[!(?P<type>[A-Za-z]+)\][ \t]*(?P<title>[^\n<]*)(?P<rest>.*?)</blockquote>",
    re.DOTALL,
)
_TASK_RE = re.compile(r"<li>\[([ xX])\] ")
_HILITE_SPLIT_RE = re.compile(r"(<pre>.*?</pre>|<code>.*?</code>)", re.DOTALL)
_HILITE_RE = re.compile(r"==(\S(?:.*?\S)?)==")


def _callouts(html: str) -> str:
    def repl(m: re.Match) -> str:
        ctype = m.group("type").lower()
        title = m.group("title").strip() or _CALLOUT_TITLES.get(ctype, ctype.capitalize())
        # The first paragraph lost its <p> opener to the title match; restore it so
        # the body renders as paragraph(s). `rest` runs up to </blockquote>.
        body = "<p>" + m.group("rest").lstrip("\n")
        return (f'<div class="callout callout-{ctype}">'
                f'<div class="callout-title">{title}</div>{body}</div>')
    return _CALLOUT_RE.sub(repl, html)


def _tasklist(html: str) -> str:
    counter = [0]

    def repl(m: re.Match) -> str:
        checked = m.group(1).lower() == "x"
        i = counter[0]
        counter[0] += 1
        box = (f'<input type="checkbox" data-ti="{i}" disabled'
               f'{" checked" if checked else ""}>')
        return f'<li class="task-item">{box} '
    return _TASK_RE.sub(repl, html)


def _highlight(html: str) -> str:
    parts = _HILITE_SPLIT_RE.split(html)
    for i in range(0, len(parts), 2):  # even indices are outside code regions
        parts[i] = _HILITE_RE.sub(r"<mark>\1</mark>", parts[i])
    return "".join(parts)


def _wiki_repl(m: re.Match, src_path: str) -> str:
    inner = m.group(1).strip()
    if not inner:
        return m.group(0)
    if "|" in inner:
        linkpart, alias = inner.split("|", 1)
    else:
        linkpart, alias = inner, None
    if "#" in linkpart:
        target, anchor = linkpart.split("#", 1)
    else:
        target, anchor = linkpart, None
    target = target.strip()
    if not target:
        return m.group(0)
    label = (alias.strip() if alias else None) or (target + (f"#{anchor.strip()}" if anchor else ""))
    href = f"/go?from={quote(src_path)}&target={quote(target)}"
    return f"[{label}]({href})"


def _convert_wikilinks(text: str, src_path: str) -> str:
    # Protect fenced code blocks so wikilinks inside them stay literal.
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    for i in range(0, len(parts), 2):
        parts[i] = WIKILINK_RE.sub(lambda m: _wiki_repl(m, src_path), parts[i])
    return "".join(parts)


def render_markdown(text: str, src_path: str = "") -> str:
    html = _md.render(_convert_wikilinks(text or "", src_path))
    # Obsidian-flavored post-processing before sanitization: callout blocks,
    # task-list checkboxes, and ==highlight==.
    html = _callouts(html)
    html = _tasklist(html)
    html = _highlight(html)
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
