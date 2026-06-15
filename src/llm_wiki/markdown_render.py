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
    "img", "blockquote",
}
_ALLOWED_ATTRS = {
    "a": ["href", "title", "class"],
    "img": ["src", "alt", "title"],
    "span": ["class"],
    "code": ["class"],
    "th": ["align"],
    "td": ["align"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


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
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
