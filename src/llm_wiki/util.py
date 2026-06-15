"""Small shared helpers: time, hashing, and vault-relative path handling."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

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
        parts.append(seg)
    if not parts:
        raise PathError("path is empty")
    rel = "/".join(parts)
    if not rel.lower().endswith(".md"):
        rel += ".md"
    return rel


def path_norm(rel: str) -> str:
    """Case-insensitive normalization key used for uniqueness + link resolution."""
    return rel.lower()


def folder_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def basename_stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


def safe_join(vault: Path | str, rel: str) -> Path:
    """Resolve ``rel`` under ``vault`` and guarantee it does not escape it."""
    vault_p = Path(vault).resolve()
    target = (vault_p / rel).resolve()
    if target != vault_p and vault_p not in target.parents:
        raise PathError("path escapes the vault")
    return target
