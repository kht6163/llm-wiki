"""Render markdown to safe HTML for the web viewer, with Obsidian-style
``[[wikilink]]`` support and ``![[note]]`` / ``![[note#heading]]`` embeds
(transclusion). Wikilinks become links to a ``/go`` resolver route so plain
rendering needs no database access; embeds expand inline only when a
``resolve_embed`` callback is supplied (the web view route injects one).
Output is sanitized with bleach.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from html import escape
from urllib.parse import quote

import bleach
from markdown_it import MarkdownIt

from .markdown_utils import WIKILINK_RE, parse_frontmatter, section_text
from .util import path_norm

# An embed resolver maps a raw link target (e.g. "folder/Note") to the target
# document as {"path", "title", "content"}, or None when it doesn't resolve.
EmbedResolver = Callable[[str], "dict | None"]

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
# Code regions to pass through verbatim when expanding wikilinks/embeds (which run on
# raw markdown, before the parser): fenced blocks AND inline code spans (single/double
# backtick). `[[link]]` / `![[embed]]` inside code must stay literal — Obsidian does the
# same, and the editor's markdown-it rules already skip code spans, so this keeps the
# server render in parity instead of leaking a `[label](/go?…)` link inside `<code>`.
_CODE_SPLIT_RE = re.compile(r"(```.*?```|``[^\n]+?``|`[^`\n]+?`)", re.DOTALL)

# ``![[target]]`` / ``![[target#heading]]`` / ``![[target|alias]]`` embeds.
EMBED_RE = re.compile(r"!\[\[([^\[\]\n]+?)\]\]")
# Recursion safety: cap nesting depth and total expansions per render, and track the
# ancestor chain to refuse cycles (A embeds B embeds A).
_MAX_EMBED_DEPTH = 4
_MAX_EMBEDS = 50
# Private-use sentinel: markdown-it passes it through untouched (not link/typography
# significant), so we can swap rendered embed HTML back in after the parser runs.
_EMBED_SENTINEL = "EMBED{}"


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


def _parse_embed(inner: str) -> tuple[str, str | None]:
    """Split an embed's inner text into (target, anchor); the ``|alias`` part (Obsidian
    uses it for embed dimensions) is dropped — we don't size embeds."""
    inner = inner.split("|", 1)[0].strip()
    if "#" in inner:
        target, anchor = inner.split("#", 1)
        return target.strip(), anchor.strip()
    return inner, None


def _embed_box(cls: str, title_html: str, note: str = "", body: str = "") -> str:
    note_html = f'<span class="embed-note">{escape(note)}</span>' if note else ""
    body_html = f'<div class="embed-body">{body}</div>' if body else ""
    return (f'<div class="embed {cls}"><div class="embed-head">{title_html}{note_html}</div>'
            f'{body_html}</div>')


def _render_embed(target: str, anchor: str | None, src_path: str,
                  resolve: EmbedResolver, depth: int, seen: frozenset[str],
                  budget: list[int]) -> str:
    """Render one ``![[target#anchor]]`` embed to HTML (recursing into the target)."""
    label = target + (f"#{anchor}" if anchor else "")
    go = f"/go?from={quote(src_path)}&target={quote(target)}"
    # No resolver, depth/budget exhausted -> show a collapsed link instead of expanding.
    if depth >= _MAX_EMBED_DEPTH or budget[0] <= 0:
        return _embed_box("embed-collapsed",
                          f'<a class="embed-title" href="{go}">{escape(label)}</a>',
                          note="펼치지 않음")
    res = resolve(target)
    if not res:
        return _embed_box("embed-missing",
                          f'<span class="embed-title">{escape(label)}</span>', note="없는 문서")
    path = res["path"]
    if path_norm(path) in seen:
        return _embed_box("embed-cycle",
                          f'<span class="embed-title">{escape(res.get("title") or path)}</span>',
                          note="순환 임베드")
    body = res.get("content") or ""
    head_label = escape(res.get("title") or path)
    if anchor:
        sect = section_text(body, anchor)
        if sect is None:
            return _embed_box("embed-missing",
                              f'<span class="embed-title">{escape(label)}</span>', note="없는 섹션")
        body = sect
        head_label += f' › {escape(anchor)}'
    budget[0] -= 1
    inner = _render(body, path, resolve, depth + 1, seen | {path_norm(path)}, budget)
    doc_href = "/doc/" + quote(path)
    return _embed_box("", f'<a class="embed-title" href="{doc_href}">{head_label}</a>', body=inner)


def _convert_inline(text: str, src_path: str, resolve: EmbedResolver | None,
                    depth: int, seen: frozenset[str], budget: list[int],
                    embeds: list[str]) -> str:
    """Replace embeds (with sentinels, collecting rendered HTML) and wikilinks (with
    markdown links) outside code regions — fenced blocks and inline code spans alike."""
    def embed_repl(m: re.Match) -> str:
        target, anchor = _parse_embed(m.group(1))
        if not target:
            return m.group(0)
        if resolve is None:
            # Plain render (no DB context): keep the embed visible as a wikilink.
            return f"[[{m.group(1)}]]"
        embeds.append(_render_embed(target, anchor, src_path, resolve, depth, seen, budget))
        return _EMBED_SENTINEL.format(len(embeds) - 1)

    parts = _CODE_SPLIT_RE.split(text)
    for i in range(0, len(parts), 2):
        seg = EMBED_RE.sub(embed_repl, parts[i])
        seg = WIKILINK_RE.sub(lambda m: _wiki_repl(m, src_path), seg)
        parts[i] = seg
    return "".join(parts)


def _render(text: str, src_path: str, resolve: EmbedResolver | None,
            depth: int, seen: frozenset[str], budget: list[int]) -> str:
    # YAML frontmatter is metadata, not prose. Left in, CommonMark turns the
    # opening `---` into an <hr> and the `key: value` lines + closing `---` into a
    # setext <h2>, so the block renders as a broken heading atop every document.
    # Strip it before the parser; the values are surfaced separately as Properties.
    body = (text or "")[parse_frontmatter(text or "")[1]:]
    embeds: list[str] = []
    here = path_norm(src_path) if src_path else None
    seen_here = seen | ({here} if here else set())
    html = _md.render(_convert_inline(body, src_path, resolve, depth, seen_here, budget, embeds))
    # Obsidian-flavored post-processing before sanitization: callout blocks,
    # task-list checkboxes, and ==highlight==. (Embed HTML is swapped in afterwards,
    # so it is rendered exactly once — by the recursive call that produced it.)
    html = _callouts(html)
    html = _tasklist(html)
    html = _highlight(html)
    for i, embed_html in enumerate(embeds):
        token = _EMBED_SENTINEL.format(i)
        html = html.replace(f"<p>{token}</p>", embed_html).replace(token, embed_html)
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )


def render_markdown(text: str, src_path: str = "", *,
                    resolve_embed: EmbedResolver | None = None) -> str:
    """Render markdown to sanitized HTML. ``![[note]]`` / ``![[note#heading]]`` embeds
    expand inline only when ``resolve_embed`` is given (target -> {path,title,content}
    or None); without it they stay as plain links so callers without DB access (tests,
    snippet previews) still render safely."""
    return _render(text, src_path, resolve_embed, 0, frozenset(), [_MAX_EMBEDS])
