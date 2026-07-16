"""Targeted document edits for DocumentService.

Extracted so DocumentService stays a thin coordinator. Public entry points remain
on DocumentService (each delegates here).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from itertools import islice
from typing import TYPE_CHECKING

import regex as regex_lib

from ..markdown_utils import (
    document_properties,
    remove_frontmatter_property,
    set_frontmatter_property,
    set_frontmatter_tags,
)
from ..util import normalize_rel_path
from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

if TYPE_CHECKING:
    from .auth import Principal

REGEX_TIMEOUT_S = 0.25
MAX_PATCH_MATCHES = 10_000
_IDEM_KEY_MAX_CHARS = 200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TASK_LINE_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\].*)$")

_PROP_RESERVED = {"title", "tags"}
_PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _heading_matches(body: str):
    """All headings whose text equals nothing in particular — returns (lines, matches)
    where matches is [(line_index, level, text)] for every heading line, in order.
    The basis for section location and occurrence disambiguation."""
    lines = body.splitlines(keepends=True)
    matches: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line.rstrip("\n"))
        if m:
            matches.append((i, len(m.group(1)), m.group(2).strip()))
    return lines, matches

def _count_sections(body: str, heading: str) -> int:
    """How many headings carry this exact text — used to report occurrence range."""
    target = heading.strip().lower()
    _lines, matches = _heading_matches(body)
    return sum(1 for _i, _lvl, text in matches if text.lower() == target)

def _locate_section(body: str, heading: str, occurrence: int = 1):
    """Find a markdown section by heading text. ``occurrence`` (1-based) selects the
    Nth heading with that text, so repeated headings (e.g. several "예시"/"Notes") can
    be edited unambiguously instead of always hitting the first. Returns
    (lines, start, end, level) where start is the heading line index and end is the
    exclusive index of the next heading at the same-or-higher level (the section's
    subtree), or None when there's no such heading / the occurrence is out of range."""
    target = heading.strip().lower()
    occ = max(1, int(occurrence))
    lines, matches = _heading_matches(body)
    hits = [(i, lvl) for i, lvl, text in matches if text.lower() == target]
    if occ > len(hits):
        return None
    start, level = hits[occ - 1]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) <= level:
            end = j
            break
    return lines, start, end, level

def _locate_or_raise(doc: dict, heading: str, occurrence: int):
    """Locate a section or raise a precise error: ValidationError when the heading
    exists but the requested occurrence is out of range (so an agent learns the actual
    count instead of silently editing the wrong one), NotFoundError when nothing matches."""
    loc = _locate_section(doc["content"], heading, occurrence)
    if loc:
        return loc
    n = _count_sections(doc["content"], heading)
    if n and occurrence > n:
        raise ValidationError(
            f"occurrence {occurrence} is out of range: the document has {n} "
            f"section(s) titled {heading!r}.",
            path=doc["path"],
        )
    raise NotFoundError(f"No section titled {heading!r} in this document.", path=doc["path"])

def _as_block(text: str) -> str:
    """Normalize inserted text to end with exactly one trailing newline."""
    return text.rstrip("\n") + "\n"

def get_section(svc, path: str, heading: str, occurrence: int = 1) -> dict:
    doc = svc.get(path)
    lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
    return {
        "path": doc["path"],
        "heading": heading,
        "occurrence": occurrence,
        "version": doc["version"],
        "tags": doc["tags"],
        "content": "".join(lines[start:end]),
    }

def outline(svc, path: str) -> dict:
    """Flat heading outline of a document: [{level, text, line}] (1-based lines).
    Lets an agent discover exact heading strings before a section read/edit."""
    doc = svc.get(path)
    headings: list[dict] = []
    for i, line in enumerate(doc["content"].splitlines()):
        m = _HEADING_RE.match(line)
        if m:
            headings.append(
                {"level": len(m.group(1)), "text": m.group(2).strip(), "line": i + 1}
            )
    return {"path": doc["path"], "version": doc["version"], "headings": headings}

def replace_section(
    svc,
    principal: Principal,
    path: str,
    heading: str,
    text: str,
    base_version: int | None = None,
    occurrence: int = 1,
) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    doc = svc.get(path)
    lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
    # Keep the heading line; replace its body up to the next same/higher heading.
    body = "".join(lines[: start + 1]) + _as_block(text) + "".join(lines[end:])
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, body)

def append_section(
    svc,
    principal: Principal,
    path: str,
    heading: str,
    text: str,
    base_version: int | None = None,
    occurrence: int = 1,
) -> dict:
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    doc = svc.get(path)
    lines, start, end, _ = _locate_or_raise(doc, heading, occurrence)
    head = "".join(lines[:end])
    # Guarantee a line boundary: a final section whose last line has no trailing
    # newline would otherwise glue the appended block onto that line.
    if head and not head.endswith("\n"):
        head += "\n"
    body = head + _as_block(text) + "".join(lines[end:])
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, body)

def append_to_document(
    svc,
    principal: Principal,
    path: str,
    text: str,
    ensure_heading: str | None = None,
    base_version: int | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Append a text block to the document — the natural agent-journaling
    primitive (decision logs, daily notes). With ``ensure_heading`` the block
    goes at the end of that heading's section, creating the heading (h2) at the
    document end if it doesn't exist yet; without it, the block lands at the very
    end. Like the section editors, ``base_version`` defaults to the server-read
    version so a plain append doesn't need a round-trip and won't spuriously
    conflict.

    ``idempotency_key`` makes the append retry-safe: a key the server has already
    applied replays the prior result instead of appending again, so a client that
    retries after a lost response won't duplicate the block. Use a fresh key per
    logical append."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    if not text or not text.strip():
        raise ValidationError("'text' is required.")
    idem_key = (idempotency_key or "").strip()
    if idempotency_key is not None:
        if not idem_key or len(idem_key) > _IDEM_KEY_MAX_CHARS:
            raise ValidationError(
                f"idempotency_key must be 1-{_IDEM_KEY_MAX_CHARS} characters."
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in idem_key):
            raise ValidationError("idempotency_key cannot contain control characters.")
    rel = normalize_rel_path(path)
    logical_heading = ensure_heading.strip().lstrip("#").strip() if ensure_heading else ""
    request_hash = hashlib.sha256(
        "\0".join(
            [rel, text, logical_heading, "" if base_version is None else str(int(base_version))]
        ).encode("utf-8")
    ).hexdigest()
    if idempotency_key:
        cached = svc._idem_lookup("append", principal.user_id, idem_key, request_hash)
        if cached is not None:
            return cached
    doc = svc.get(rel)
    body = doc["content"]
    if ensure_heading and ensure_heading.strip():
        heading = logical_heading
        loc = _locate_section(body, heading)
        if loc:
            lines, _start, end, _ = loc
            head = "".join(lines[:end])
            if head and not head.endswith("\n"):
                head += "\n"
            new_body = head + _as_block(text) + "".join(lines[end:])
        else:
            base = body.rstrip("\n")
            prefix = (base + "\n\n") if base else ""
            new_body = f"{prefix}## {heading}\n\n{_as_block(text)}"
    else:
        base = body.rstrip("\n")
        prefix = (base + "\n\n") if base else ""
        new_body = prefix + _as_block(text)
    bv = doc["version"] if base_version is None else int(base_version)
    idem = (
        ("append", principal.user_id, idem_key, request_hash)
        if idempotency_key
        else None
    )
    try:
        return svc.update(principal, doc["path"], bv, new_body, idempotency=idem)
    except (sqlite3.IntegrityError, ConflictError):
        # A concurrent request with the same key committed between our pre-check and
        # our commit. Depending on whether it won before or after our CAS, the loser
        # sees either the key's UNIQUE constraint or the newer document version. Its
        # transaction rolled back in both cases, so replay the original result.
        cached = (
            svc._idem_lookup("append", principal.user_id, idem_key, request_hash)
            if idempotency_key
            else None
        )
        if cached is not None:
            return cached
        raise

def patch(
    svc,
    principal: Principal,
    path: str,
    find: str,
    replace: str,
    base_version: int | None = None,
    count: int = 1,
    mode: str = "literal",
    occurrence: int | None = None,
) -> dict:
    """Find-and-replace a substring (``mode='literal'``) or a regular expression
    (``mode='regex'``, ``re.MULTILINE``; ``replace`` may use ``\\1`` backrefs).
    ``occurrence`` (1-based) targets a single match deterministically — the way
    out of "appears N times" failures on repetitive content; otherwise ``count``
    bounds how many matches may be replaced (0/None = all)."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    if not find:
        raise ValidationError("'find' text is required.")
    if mode not in ("literal", "regex"):
        raise ValidationError("mode must be 'literal' or 'regex'.")
    if occurrence is not None and occurrence < 1:
        raise ValidationError("occurrence is 1-based (must be >= 1).")
    doc = svc.get(path)
    content, rel = doc["content"], doc["path"]

    if mode == "regex":
        if len(find) > 1000:
            raise ValidationError("regex pattern too long (max 1000 chars).")
        try:
            pat = regex_lib.compile(find, regex_lib.MULTILINE)
        except regex_lib.error as e:
            raise ValidationError(f"invalid regex: {e}") from None
        try:
            matches = list(
                islice(
                    pat.finditer(content, timeout=REGEX_TIMEOUT_S),
                    MAX_PATCH_MATCHES + 1,
                )
            )
            if len(matches) > MAX_PATCH_MATCHES:
                raise ValidationError(
                    f"Pattern matches more than {MAX_PATCH_MATCHES} times; narrow the pattern."
                )
            n = len(matches)
            if n == 0:
                raise NotFoundError("Pattern not found; nothing patched.", path=rel)
            if occurrence is not None:
                if occurrence > n:
                    raise ValidationError(f"occurrence {occurrence} out of range (1..{n}).")
                m = matches[occurrence - 1]
                new_body = content[: m.start()] + m.expand(replace) + content[m.end() :]
            else:
                if count and n > count:
                    raise ValidationError(
                        f"Pattern matches {n} times (limit {count}); narrow it, pass "
                        f"'occurrence', or raise 'count'."
                    )
                new_body = pat.sub(replace, content, count=count or 0, timeout=REGEX_TIMEOUT_S)
        except TimeoutError:
            raise ValidationError("regex evaluation timed out; narrow the pattern.") from None
    else:
        occurrences = content.count(find)
        if occurrences == 0:
            raise NotFoundError("Search text not found; nothing patched.", path=rel)
        if occurrence is not None:
            if occurrence > occurrences:
                raise ValidationError(
                    f"occurrence {occurrence} out of range (1..{occurrences})."
                )
            idx = -len(find)
            for _ in range(occurrence):
                idx = content.find(find, idx + len(find))
            new_body = content[:idx] + replace + content[idx + len(find) :]
        else:
            if count and occurrences > count:
                raise ValidationError(
                    f"Search text appears {occurrences} times (limit {count}); make it more "
                    f"specific, pass 'occurrence', or raise 'count'."
                )
            new_body = content.replace(find, replace, count if count else -1)

    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, rel, bv, new_body)

def restore_revision(
    svc, principal: Principal, path: str, version: int, base_version: int | None = None
) -> dict:
    """Replay a past revision's body as a new edit (one CAS update) — a server-side
    undo. The old body is loaded here and never has to travel through the caller,
    so reverting a large document is a single small call. ``base_version`` defaults
    to the current version; pass it to reject the revert with 'conflict' if the
    document changed since you looked."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    rev = svc.revision(path, int(version))
    bv = svc.get(rev["path"])["version"] if base_version is None else int(base_version)
    return svc.update(principal, rev["path"], bv, rev["content"], title=rev["title"])

def toggle_task(
    svc,
    principal: Principal,
    path: str,
    line: int | None = None,
    *,
    index: int | None = None,
    base_version: int | None = None,
) -> dict:
    """Flip a single markdown task checkbox (``- [ ]`` <-> ``- [x]``), then save
    through the CAS update path. Target by 1-based ``line`` or by 0-based
    ``index`` (the Nth checkbox in document order — matches the rendered
    ``data-ti`` attribute used by click-to-toggle in the viewer)."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    doc = svc.get(path)
    lines = doc["content"].split("\n")
    if index is not None:
        tasks = [i for i, ln in enumerate(lines) if _TASK_LINE_RE.match(ln)]
        if int(index) < 0 or int(index) >= len(tasks):
            raise ValidationError("task index is out of range.")
        idx = tasks[int(index)]
    elif line is not None:
        idx = int(line) - 1
    else:
        raise ValidationError("line or index is required.")
    if idx < 0 or idx >= len(lines):
        raise ValidationError("line is out of range.")
    m = _TASK_LINE_RE.match(lines[idx])
    if not m:
        raise ValidationError("no task checkbox on that line.")
    lines[idx] = m.group(1) + (" " if m.group(2).lower() == "x" else "x") + m.group(3)
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, "\n".join(lines))

def patch_tags(
    svc,
    principal: Principal,
    path: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add/remove tags by rewriting the frontmatter ``tags`` list (body untouched).
    Returns the document's resulting tags. Tags written inline as ``#hashtags``
    in the body are re-derived on save, so they cannot be removed this way."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    doc = svc.get(path)
    current = set(doc["tags"])
    add_set = {str(t).strip().lstrip("#") for t in (add or []) if str(t).strip()}
    remove_set = {str(t).strip().lstrip("#") for t in (remove or []) if str(t).strip()}
    target = sorted((current | add_set) - remove_set)
    if target == sorted(current):  # no net change — stay idempotent, skip the version bump
        return {"path": doc["path"], "version": doc["version"], "tags": sorted(current)}
    new_content = set_frontmatter_tags(doc["content"], target)
    updated = svc.update(principal, doc["path"], doc["version"], new_content, tags=target)
    return {"path": updated["path"], "version": updated["version"], "tags": updated["tags"]}

def merge_tags(svc, principal: Principal, sources: list[str], dest: str) -> dict:
    """Vault-wide tag cleanup (editor/admin only): rewrite every document's frontmatter
    ``tags`` so each of ``sources`` becomes ``dest``. Each affected document is updated
    through patch_tags (its own CAS revision), so this is not one transaction. Tags
    written inline as ``#hashtags`` in the body are NOT rewritten (patch_tags only
    manages the frontmatter list) — edit the body for those. Returns the dest, the
    normalized sources, and how many documents were touched."""
    if not principal.can_write:
        raise ForbiddenError(f"Role '{principal.role}' cannot rewrite tags (read/search only).")
    dest = str(dest or "").strip().lstrip("#")
    if not dest:
        raise ValidationError("dest tag must not be empty.")
    src = sorted(
        {str(s).strip().lstrip("#") for s in (sources or []) if str(s).strip()} - {dest}
    )
    if not src:
        raise ValidationError("no source tags to merge (after removing the dest tag).")
    ph = ",".join("?" * len(src))
    with svc.db.reader() as conn:
        paths = [
            r["path"]
            for r in conn.execute(
                f"SELECT DISTINCT d.path FROM tags t JOIN documents d ON d.id=t.doc_id "
                f"WHERE t.tag IN ({ph}) AND d.is_deleted=0 ORDER BY d.path",
                src,
            )
        ]
    changed = 0
    skipped: list[dict] = []
    pending: list[dict] = []
    for p in paths:
        try:
            before = svc.get(p)["version"]
            # Route through the service method so tests/callers can monkeypatch it.
            after = svc.patch_tags(principal, p, add=[dest], remove=src)
            if after["version"] != before:
                changed += 1
        except (ConflictError, NotFoundError) as exc:
            skipped.append({"path": p, "code": exc.code, "message": exc.message})
        except Exception as exc:
            from .documents import ProjectionPendingError

            if not isinstance(exc, ProjectionPendingError):  # pragma: no cover - unexpected
                raise
            # The DB mutation committed; surface the recoverable file projection
            # separately and continue the vault-wide operation.
            changed += 1
            pending.append({"path": p, "reason": exc.result.reason})
    return {
        "ok": True,
        "dest": dest,
        "sources": src,
        "docs_affected": len(paths),
        "docs_changed": changed,
        "docs_skipped": len(skipped),
        "skipped": skipped,
        "projection_pending": pending,
    }

def rename_tag(svc, principal: Principal, old: str, new: str) -> dict:
    """Rename one frontmatter tag across the whole vault (editor/admin only) — a
    single-source merge_tags. See merge_tags for the inline-hashtag caveat."""
    return merge_tags(svc, principal, [old], new)

def _validate_prop_key(key: str) -> str:
    key = (key or "").strip()
    if not key or not _PROP_KEY_RE.match(key):
        raise ValidationError("property key must be letters/digits/_/- only.")
    if key.lower() in _PROP_RESERVED:
        raise ValidationError(f"'{key}' is managed elsewhere (use the title/tags editors).")
    return key

def _norm_prop_value(value: str | list[str]) -> str | list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return str(value).strip()

def set_property(
    svc,
    principal: Principal,
    path: str,
    key: str,
    value: str | list[str],
    base_version: int | None = None,
) -> dict:
    """Set/replace one frontmatter property (body + other keys untouched), through
    the CAS update path. An empty value removes the key."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    key = _validate_prop_key(key)
    doc = svc.get(path)
    val = _norm_prop_value(value)
    if not val:
        new_content = remove_frontmatter_property(doc["content"], key)
    else:
        new_content = set_frontmatter_property(
            doc["content"], key, val if isinstance(val, str) or len(val) > 1 else val[0]
        )
    if new_content == doc["content"]:
        return svc.get(doc["path"])
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, new_content)

def remove_property(
    svc, principal: Principal, path: str, key: str, base_version: int | None = None
) -> dict:
    """Remove one frontmatter property (no-op if absent), through CAS update."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    key = _validate_prop_key(key)
    doc = svc.get(path)
    new_content = remove_frontmatter_property(doc["content"], key)
    if new_content == doc["content"]:
        return svc.get(doc["path"])
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, new_content)

def replace_properties(
    svc,
    principal: Principal,
    path: str,
    props: list[tuple[str, list[str]]],
    base_version: int | None = None,
) -> dict:
    """Replace the whole editable property set in one revision: drops omitted keys,
    sets the rest. ``title``/``tags`` and the body are preserved. ``props`` is an
    ordered list of (key, values); empty value-lists drop the key."""
    if not principal.can_write:
        raise ForbiddenError(
            f"Role '{principal.role}' cannot modify documents (read/search only)."
        )
    cleaned: list[tuple[str, list[str]]] = []
    seen_keys: set[str] = set()
    for key, values in props:
        key = _validate_prop_key(key)
        if key.lower() in seen_keys:
            raise ValidationError(f"duplicate property key '{key}'.")
        seen_keys.add(key.lower())
        vals = [str(v).strip() for v in values if str(v).strip()]
        cleaned.append((key, vals))
    doc = svc.get(path)
    content = doc["content"]
    keep = {k.lower() for k, _ in cleaned}
    for existing_key, _ in document_properties(content):
        if existing_key.lower() not in keep:
            content = remove_frontmatter_property(content, existing_key)
    for key, vals in cleaned:
        if not vals:
            content = remove_frontmatter_property(content, key)
        else:
            content = set_frontmatter_property(
                content, key, vals[0] if len(vals) == 1 else vals
            )
    if content == doc["content"]:
        return svc.get(doc["path"])
    bv = doc["version"] if base_version is None else int(base_version)
    return svc.update(principal, doc["path"], bv, content)

