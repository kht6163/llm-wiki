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
    heading_path: str | None = None  # "Install > Linux > apt" breadcrumb of ancestors


# Mirrors the client-side heading-id slug in web/static/outline.js so a search
# result's section anchor lands on the right rendered heading. Keep them in sync.
_SLUG_STRIP_RE = re.compile(r"[^a-zA-Z0-9_À-￿\s-]")


def heading_slug(text: str) -> str:
    s = _SLUG_STRIP_RE.sub("", (text or "").lower().strip())
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "section"


def section_text(body: str, heading: str) -> str | None:
    """Return the markdown of the section whose heading matches ``heading`` (the first
    match, compared by heading slug so spacing/case differences don't matter), from the
    heading line up to the next heading of the same or higher level. ``None`` if no such
    heading. Used to embed ``![[note#heading]]`` section transclusions."""
    want = heading_slug(heading)
    heads = list(HEADING_RE.finditer(body))
    for idx, h in enumerate(heads):
        if heading_slug(h.group(2)) != want:
            continue
        level = len(h.group(1))
        end = len(body)
        for nxt in heads[idx + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return body[h.start():end].strip()
    return None


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


# Frontmatter keys already surfaced elsewhere in the reading view (the title is the
# page <h1>, tags are the #chips in the doc-meta bar), so the Properties panel omits
# them to avoid duplicating machine data.
_PROPS_OMIT = {"title", "tags"}


def document_properties(content: str) -> list[tuple[str, list[str]]]:
    """Ordered (key, values) frontmatter properties for the reading-view panel.

    Excludes title/tags (shown elsewhere) and empty values. Every value is
    normalized to a list of display strings so the template renders scalars and
    inline lists uniformly (one chip each)."""
    meta, _ = parse_frontmatter(content or "")
    props: list[tuple[str, list[str]]] = []
    for key, value in meta.items():
        if key.lower() in _PROPS_OMIT:
            continue
        if isinstance(value, list):
            vals = [str(v).strip() for v in value if str(v).strip()]
        else:
            text = str(value).strip()
            vals = [text] if text else []
        if vals:
            props.append((key, vals))
    return props


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


def _format_tag_list(tags: list[str]) -> str:
    parts = []
    for t in tags:
        parts.append('"' + t.replace('"', '\\"') + '"' if any(c in t for c in ", []\"'#") else t)
    return "[" + ", ".join(parts) + "]"


def set_frontmatter_tags(content: str, tags: list[str]) -> str:
    """Rewrite a document's frontmatter ``tags`` to ``tags`` (an inline list),
    preserving every other frontmatter key and the body. Inserts a frontmatter
    block if absent and ``tags`` is non-empty; drops the key (and an otherwise-empty
    block) when ``tags`` is empty. Inline ``#hashtags`` in the body are untouched."""
    m = FRONTMATTER_RE.match(content or "")
    if m:
        rest = content[m.end():]
        lines = m.group(1).split("\n")
        kept: list[str] = []
        i = 0
        while i < len(lines):
            km = re.match(r"^([A-Za-z0-9_\-]+):[ \t]*(.*)$", lines[i])
            if km and km.group(1).strip().lower() == "tags":
                if km.group(2).strip() == "":  # block list: skip following '- ' items
                    i += 1
                    while i < len(lines) and re.match(r"^[ \t]*-[ \t]+", lines[i]):
                        i += 1
                else:
                    i += 1
                continue
            kept.append(lines[i])
            i += 1
        if tags:
            kept.append(f"tags: {_format_tag_list(tags)}")
        body = "\n".join(kept).strip("\n")
        return f"---\n{body}\n---\n{rest}" if body else rest.lstrip("\n")
    if not tags:
        return content
    return f"---\ntags: {_format_tag_list(tags)}\n---\n\n{content}"


_KEY_RE = re.compile(r"^([A-Za-z0-9_\-]+):[ \t]*(.*)$")


def _emit_frontmatter_value(key: str, value: str | list[str]) -> str:
    """One frontmatter line for ``key``. Lists become an inline ``[a, b]``; scalars are
    quoted only when they contain YAML-significant characters or edge whitespace."""
    if isinstance(value, list):
        return f"{key}: {_format_tag_list([str(v) for v in value])}"
    v = str(value)
    if v == "" or v != v.strip() or any(c in v for c in ":#[]{}\"'") or v[:1] in "-?&*!|>%@`":
        return f'{key}: "' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{key}: {v}"


def _drop_key_lines(lines: list[str], i: int) -> int:
    """Advance past the value of the key at ``lines[i]`` — a scalar/inline line, plus any
    following block ``- item`` lines. Returns the index after the value."""
    km = _KEY_RE.match(lines[i])
    i += 1
    if km and km.group(2).strip() == "":
        while i < len(lines) and re.match(r"^[ \t]*-[ \t]+", lines[i]):
            i += 1
    return i


def _rewrite_frontmatter(content: str, key: str, new_line: str | None) -> str:
    """Set (``new_line`` given) or remove (``new_line`` None) one frontmatter key,
    preserving every other key and the body. Creates a block when setting into a
    document that has none; drops an emptied block entirely."""
    key_l = key.strip().lower()
    m = FRONTMATTER_RE.match(content or "")
    if not m:
        return content if new_line is None else f"---\n{new_line}\n---\n\n{content or ''}"
    rest = content[m.end():]
    lines = m.group(1).split("\n")
    kept: list[str] = []
    placed = False
    i = 0
    while i < len(lines):
        km = _KEY_RE.match(lines[i])
        if km and km.group(1).strip().lower() == key_l:
            i = _drop_key_lines(lines, i)
            if new_line is not None and not placed:
                kept.append(new_line)
                placed = True
            continue
        kept.append(lines[i])
        i += 1
    if new_line is not None and not placed:
        kept.append(new_line)
    body = "\n".join(kept).strip("\n")
    return f"---\n{body}\n---\n{rest}" if body else rest.lstrip("\n")


def set_frontmatter_property(content: str, key: str, value: str | list[str]) -> str:
    """Set/replace a single frontmatter ``key`` (other keys and the body untouched)."""
    return _rewrite_frontmatter(content, key, _emit_frontmatter_value(key.strip(), value))


def remove_frontmatter_property(content: str, key: str) -> str:
    """Remove a single frontmatter ``key`` (no-op if absent)."""
    return _rewrite_frontmatter(content, key, None)


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


def rewrite_link_target(link: Link, new_target: str) -> str:
    """Rebuild a link's raw text pointing at ``new_target``, preserving the alias,
    anchor, and (for markdown links) any title. Used by reference-rename to repoint
    stale links after a document moves, touching only the target."""
    if link.type == "wikilink":
        inner = link.raw[2:-2]
        if "|" in inner:
            linkpart, aliaspart = inner.split("|", 1)
        else:
            linkpart, aliaspart = inner, None
        anchor = ("#" + linkpart.split("#", 1)[1]) if "#" in linkpart else ""
        new_inner = new_target + anchor + (("|" + aliaspart) if aliaspart is not None else "")
        return f"[[{new_inner}]]"
    # markdown: [text](url[ "title"]) — match against the raw (anchored at 0).
    m = MDLINK_RE.match(link.raw)
    if not m:
        return link.raw
    text_part, url = m.group(1), m.group(2)
    anchor = ("#" + url.split("#", 1)[1]) if "#" in url else ""
    title = link.raw[m.end(2):-1]  # any ` "title"` between the url and the closing ')'
    return f"[{text_part}]({new_target}{anchor}{title})"


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

    # Build (heading, heading_path, start, end) sections over the body region. The
    # heading_path is the breadcrumb of enclosing headings (e.g. "Install > Linux"),
    # tracked with a level stack so a matched chunk knows where it sits.
    heads = list(HEADING_RE.finditer(body_region))
    sections: list[tuple[str | None, str | None, int, int]] = []
    if not heads:
        sections.append((None, None, 0, len(body_region)))
    else:
        if heads[0].start() > 0 and body_region[: heads[0].start()].strip():
            sections.append((None, None, 0, heads[0].start()))
        stack: list[tuple[int, str]] = []
        for idx, h in enumerate(heads):
            level, htext = len(h.group(1)), h.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, htext))
            hpath = " > ".join(t for _lvl, t in stack)
            end = heads[idx + 1].start() if idx + 1 < len(heads) else len(body_region)
            sections.append((htext, hpath, h.start(), end))

    chunks: list[Chunk] = []
    ordinal = 0
    for heading, heading_path, s, e in sections:
        seg = body_region[s:e]
        if not seg.strip():
            continue
        base = meta_end + s
        if len(seg) <= max_chars:
            chunks.append(_mk_chunk(ordinal, heading, seg, base, text, heading_path))
            ordinal += 1
            continue
        # pack by paragraphs
        paras = _split_keep_offsets(seg)
        cur = ""
        cur_start = 0
        for ptext, pstart in paras:
            if cur and len(cur) + len(ptext) > max_chars:
                chunks.append(_mk_chunk(ordinal, heading, cur, base + cur_start, text, heading_path))
                ordinal += 1
                tail = cur[-overlap:] if overlap else ""
                cur = tail + ptext
                cur_start = pstart - len(tail)
            else:
                if not cur:
                    cur_start = pstart
                cur += ptext
        if cur.strip():
            chunks.append(_mk_chunk(ordinal, heading, cur, base + cur_start, text, heading_path))
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


def _mk_chunk(ordinal: int, heading: str | None, seg: str, base: int, full: str,
              heading_path: str | None = None) -> Chunk:
    stripped = seg.strip()
    lead = len(seg) - len(seg.lstrip())
    start = base + lead
    return Chunk(
        ordinal=ordinal,
        heading=heading,
        text=stripped,
        char_start=start,
        char_end=start + len(stripped),
        heading_path=heading_path,
    )
