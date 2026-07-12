"""Shared pure helpers for the web UI (used by app assembly and route modules)."""
from __future__ import annotations

import difflib
import re
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from markupsafe import Markup

from ..metrics import BUILD_INFO

_UPLOAD_CHUNK = 64 * 1024
WS_SESSION_RECHECK_S = 30.0

# Explicit Content-Type for the (fixed, safe) attachment extension set so a served
# file is never sniffed into a different type. Anything unexpected falls back to a
# non-renderable octet-stream rather than letting the browser guess.
_ATTACH_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp", ".pdf": "application/pdf",
}


class _AuthRequired(Exception):
    """Raised by the ``require_*`` route dependencies when there's no valid session.
    A registered handler turns it into a /login redirect (pages) or 401 JSON (/api),
    so individual routes no longer repeat the unauthenticated branch."""


async def _read_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read an upload in chunks, aborting as soon as it exceeds ``limit`` so a
    multi-GB body can't be buffered into memory before the size check. Returns None
    on overflow; peak memory stays at ~limit + one chunk."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _diff_lines(a_text: str, b_text: str) -> list[dict]:
    """Unified line diff classified for template rendering (text is escaped by Jinja)."""
    out: list[dict] = []
    for line in difflib.unified_diff(a_text.splitlines(), b_text.splitlines(), lineterm="", n=3):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append({"cls": cls, "text": line})
    return out


_ISO_UTC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})Z$")


def _human_dt(value: object) -> object:
    """Render a stored UTC ISO timestamp (``2026-06-16T00:44:33Z``) as a
    ``<time>`` element that static/datetime.js localizes to the viewer's
    timezone as ``YYYY-MM-DD HH:MM:SS``. Without JS it still shows the cleaned
    UTC value; anything that isn't our exact ISO format passes through."""
    if not value:
        return value
    s = str(value)
    m = _ISO_UTC_RE.match(s)
    if not m:
        return value
    utc_text = f"{m.group(1)} {m.group(2)}"
    return Markup('<time class="dt" datetime="{}">{}</time>').format(s, utc_text)


# Activity-feed time windows -> an ISO-8601 lower bound on the audit `ts` (which is
# stored UTC, so lexical >= comparison is correct). "all" means no lower bound.
_ACTIVITY_WINDOWS = ("today", "24h", "7d", "30d", "all")


def _window_since(window: str) -> str | None:
    now = datetime.now(UTC)
    if window == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "24h":
        start = now - timedelta(days=1)
    elif window == "30d":
        start = now - timedelta(days=30)
    elif window == "all":
        return None
    else:  # default / "7d"
        start = now - timedelta(days=7)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_build_info(embedder) -> None:
    """Publish static runtime facts as the llmwiki_build_info metric (set once)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            ver = version("llm-wiki")
        except PackageNotFoundError:
            ver = "unknown"
        BUILD_INFO.info({
            "version": ver,
            "embedding_model": embedder.model_name,
            "embedding_dim": str(embedder.dim),
        })
    except Exception:
        pass  # info metric is best-effort; never block app construction
