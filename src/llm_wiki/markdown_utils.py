"""Pure markdown parsing helpers: frontmatter, title/tags, link extraction, and
heading-aware chunking. No database or filesystem access here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .util import MEDIA_EXTS

FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
INLINE_TAG_RE = re.compile(r"(?:(?<=\s)|^)#([A-Za-z0-9_][A-Za-z0-9_\-/]*)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


@dataclass
class Link:
    type: str  # "wikilink" | "markdown"
    target: str  # raw target as written (path or note name), no anchor/alias
    alias: str | None
    anchor: str | None
    raw: str
    start: int
    end: int


@dataclass
class Chunk:
    ordinal: int
    heading: str | None
    text: str
    char_start: int
    char_end: int


def _parse_simple_yaml(raw: str) -> dict:
    """Minimal frontmatter parser: scalar ``key: value``, inline ``[a, b]`` lists,
    and block ``- item`` lists. Sufficient for title/tags/aliases."""
    meta: dict = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_\-]+):[ \t]*(.*)$", line)
        if not m:
            i += 1
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if val == "":
            items: list[str] = []
            j = i + 1
            while j < len(lines) and re.match(r"^[ \t]*-[ \t]+", lines[j]):
                items.append(re.sub(r"^[ \t]*-[ \t]+", "", lines[j]).strip().strip("\"'"))
                j += 1
            if items:
                meta[key] = items
                i = j
                continue
            meta[key] = ""
            i += 1
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            meta[key] = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
        else:
            meta[key] = val.strip().strip("\"'")
        i += 1
    return meta


def parse_frontmatter(text: str) -> tuple[dict, int]:
    """Return (metadata, body_start_offset). body_start is 0 if no frontmatter."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, 0
    return _parse_simple_yaml(m.group(1)), m.end()


def _mask(text: str) -> str:
    """Replace frontmatter, fenced code, and inline code with same-length spaces so
    link/tag regexes do not match inside them while character offsets stay aligned."""
    chars = list(text)

    def blank(match: re.Match) -> None:
        for k in range(match.start(), match.end()):
            if chars[k] != "\n":
                chars[k] = " "

    fm = FRONTMATTER_RE.match(text)
    if fm:
        blank(fm)
    for rx in (FENCE_RE, INLINE_CODE_RE):
        for match in rx.finditer(text):
            blank(match)
    return "".join(chars)


def derive_title(meta: dict, body: str, rel_path: str) -> str:
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    hm = HEADING_RE.search(body)
    if hm:
        return hm.group(2).strip()
    name = rel_path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def extract_tags(meta: dict, text: str) -> list[str]:
    tags: set[str] = set()
    raw = meta.get("tags")
    if isinstance(raw, str):
        raw = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
    if isinstance(raw, list):
        for t in raw:
            tags.add(str(t).lstrip("#").strip())
    scan = _mask(text)
    for m in INLINE_TAG_RE.finditer(scan):
        tags.add(m.group(1))
    return sorted(t for t in tags if t)


def _is_internal_md(url: str) -> bool:
    if not url or url.startswith("#"):
        return False
    if SCHEME_RE.match(url) or url.startswith("//"):
        return False
    lower = url.lower()
    for ext in MEDIA_EXTS:
        if lower.endswith(ext):
            return False
    return True


def extract_links(text: str) -> list[Link]:
    """Extract wikilinks and internal markdown links (code regions ignored)."""
    scan = _mask(text)
    links: list[Link] = []

    for m in WIKILINK_RE.finditer(scan):
        inner = m.group(1).strip()
        if not inner:
            continue
        if "|" in inner:
            linkpart, alias = inner.split("|", 1)
        else:
            linkpart, alias = inner, None
        if "#" in linkpart:
            target, anchor = linkpart.split("#", 1)
        else:
            target, anchor = linkpart, None
        target = target.strip()
        if not target:  # same-document anchor like [[#heading]] -> skip for graph
            continue
        links.append(Link(
            type="wikilink", target=target,
            alias=alias.strip() if alias else None,
            anchor=anchor.strip() if anchor else None,
            raw=m.group(0), start=m.start(), end=m.end(),
        ))

    for m in MDLINK_RE.finditer(scan):
        text_part, url = m.group(1), m.group(2).strip()
        if not _is_internal_md(url):
            continue
        if "#" in url:
            target, anchor = url.split("#", 1)
        else:
            target, anchor = url, None
        target = target.strip()
        if not target:
            continue
        links.append(Link(
            type="markdown", target=target,
            alias=text_part.strip() or None,
            anchor=anchor.strip() if anchor else None,
            raw=m.group(0), start=m.start(), end=m.end(),
        ))
    return links


def chunk_markdown(
    text: str, *, max_chars: int = 1200, overlap: int = 180
) -> list[Chunk]:
    """Heading-aware chunking with char offsets into the original ``text``.

    Sections are split on headings; oversized sections are packed by paragraph
    with a small character overlap between consecutive chunks.
    """
    meta_end = parse_frontmatter(text)[1]
    body_region = text[meta_end:]
    if not body_region.strip():
        return []

    # Build (heading, start, end) sections over the body region.
    heads = list(HEADING_RE.finditer(body_region))
    sections: list[tuple[str | None, int, int]] = []
    if not heads:
        sections.append((None, 0, len(body_region)))
    else:
        if heads[0].start() > 0 and body_region[: heads[0].start()].strip():
            sections.append((None, 0, heads[0].start()))
        for idx, h in enumerate(heads):
            end = heads[idx + 1].start() if idx + 1 < len(heads) else len(body_region)
            sections.append((h.group(2).strip(), h.start(), end))

    chunks: list[Chunk] = []
    ordinal = 0
    for heading, s, e in sections:
        seg = body_region[s:e]
        if not seg.strip():
            continue
        base = meta_end + s
        if len(seg) <= max_chars:
            chunks.append(_mk_chunk(ordinal, heading, seg, base, text))
            ordinal += 1
            continue
        # pack by paragraphs
        paras = _split_keep_offsets(seg)
        cur = ""
        cur_start = 0
        for ptext, pstart in paras:
            if cur and len(cur) + len(ptext) > max_chars:
                chunks.append(_mk_chunk(ordinal, heading, cur, base + cur_start, text))
                ordinal += 1
                tail = cur[-overlap:] if overlap else ""
                cur = tail + ptext
                cur_start = pstart - len(tail)
            else:
                if not cur:
                    cur_start = pstart
                cur += ptext
        if cur.strip():
            chunks.append(_mk_chunk(ordinal, heading, cur, base + cur_start, text))
            ordinal += 1
    return chunks


def _split_keep_offsets(seg: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    pos = 0
    for part in re.split(r"(\n\s*\n)", seg):
        if part:
            out.append((part, pos))
            pos += len(part)
    # merge separators into following text for cleanliness
    merged: list[tuple[str, int]] = []
    for txt, off in out:
        if txt.strip() == "" and merged:
            merged[-1] = (merged[-1][0] + txt, merged[-1][1])
        else:
            merged.append((txt, off))
    return merged or [(seg, 0)]


def _mk_chunk(ordinal: int, heading: str | None, seg: str, base: int, full: str) -> Chunk:
    stripped = seg.strip()
    lead = len(seg) - len(seg.lstrip())
    start = base + lead
    return Chunk(
        ordinal=ordinal,
        heading=heading,
        text=stripped,
        char_start=start,
        char_end=start + len(stripped),
    )
