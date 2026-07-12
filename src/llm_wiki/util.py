"""Small shared helpers: time, hashing, and vault-relative path handling."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

# Media / non-document extensions that markdown links may point at; excluded
# from the link graph.
MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico",
    ".pdf", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".ogg", ".zip",
}


class PathError(ValueError):
    """Raised when a user-supplied path is unsafe or malformed."""


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, 'Z' suffix)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clamp_int(value, lo: int, hi: int) -> int:
    """Coerce ``value`` to int and clamp it into the inclusive range ``[lo, hi]``.
    The one-liner for sanitizing caller-supplied limits/top_k/offsets at trust
    boundaries (replaces the repeated ``max(lo, min(int(x), hi))``)."""
    return max(lo, min(int(value), hi))


_CJK_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff]")


def contains_cjk(text: str | None) -> bool:
    """True when ``text`` contains Hangul, CJK ideographs, or kana (BM25 alone
    can miss unspaced compounds; hybrid mode is the usual suggestion)."""
    return bool(text and _CJK_RE.search(text))


def word_count(text: str) -> dict:
    """Word and character counts for the status bar. CJK (한중일) is counted per
    character (no spaces between words); the remainder is counted by whitespace
    tokens. Characters include everything."""
    text = text or ""
    cjk = len(_CJK_RE.findall(text))
    latin = len(_CJK_RE.sub(" ", text).split())
    return {"words": cjk + latin, "chars": len(text)}


def normalize_client_ip(host: str | None) -> str:
    """Canonicalize a client address for use as a rate-limit / audit key. An
    IPv4-mapped IPv6 address (``::ffff:1.2.3.4``) collapses to its IPv4 form so the
    same caller can't dodge the limiter by switching address families. Non-IP hosts
    (a proxy name, missing client) pass through unchanged ('?' when absent)."""
    if not host:
        return "?"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return str(ip)


def normalize_rel_path(raw: str) -> str:
    """Normalize a user-supplied document path into a clean vault-relative POSIX
    path ending in ``.md``. Rejects absolute paths and parent traversal.
    """
    if raw is None:
        raise PathError("path is required")
    p = raw.strip().replace("\\", "/")
    if not p:
        raise PathError("path is empty")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise PathError("path may not contain '..'")
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in seg):
            # Control chars (esp. CR/LF) would otherwise survive into the .md projection,
            # logs, and response headers (e.g. Content-Disposition) — reject at the door.
            raise PathError("path may not contain control characters")
        parts.append(seg)
    if not parts:
        raise PathError("path is empty")
    rel = "/".join(parts)
    if not rel.lower().endswith(".md"):
        rel += ".md"
    return rel


def normalize_folder_path(raw: str) -> str:
    """Normalize a user-supplied folder path into a clean vault-relative POSIX path
    with no trailing slash and NO ``.md`` suffix. Rejects absolute paths and parent
    traversal. Returns ``""`` for the vault root. Mirrors ``normalize_rel_path`` but
    for directories (folders are an organizational namespace, not documents)."""
    if raw is None:
        return ""
    p = raw.strip().replace("\\", "/")
    parts: list[str] = []
    for seg in p.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            raise PathError("path may not contain '..'")
        parts.append(seg)
    return "/".join(parts)


def path_norm(rel: str) -> str:
    """Case-insensitive normalization key used for uniqueness + link resolution."""
    return rel.lower()


def folder_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def basename_stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def content_disposition_attachment(filename: str) -> str:
    """An ``attachment`` Content-Disposition value with an RFC 5987 UTF-8 ``filename*``
    plus a sanitized ASCII ``filename`` fallback. Strips control characters and quoting
    metacharacters, so a document name — possibly non-ASCII (Korean) — can neither break
    the header value nor inject extra header fields, while modern clients still get the
    correct UTF-8 name via ``filename*``."""
    ascii_name = "".join(
        c for c in filename.encode("ascii", "ignore").decode("ascii")
        if c >= " " and c != "\x7f"
    ).replace('"', "").replace("\\", "") or "document.md"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def safe_join(vault: Path | str, rel: str) -> Path:
    """Resolve ``rel`` under ``vault`` and guarantee it does not escape it."""
    vault_p = Path(vault).resolve()
    target = (vault_p / rel).resolve()
    if target != vault_p and vault_p not in target.parents:
        raise PathError("path escapes the vault")
    return target
