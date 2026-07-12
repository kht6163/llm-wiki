"""Bulk Obsidian/markdown directory import helpers for DocumentService.

Extracted from documents.py so DocumentService stays a thin coordinator.
Callers continue to use DocumentService.import_from_directory (delegates here).
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from .. import graph
from ..markdown_utils import SCHEME_RE, _mask
from ..util import (
    PathError,
    normalize_folder_path,
    normalize_rel_path,
    path_norm,
    sha256_hex,
)
from . import audit
from .auth import Principal
from .errors import ConflictError, ForbiddenError, ValidationError, WikiError

if TYPE_CHECKING:
    from .documents import DocumentService

# ---- bulk import (Obsidian/markdown directory ingest) ----------------------
IMPORT_MAX_BYTES = 50 * 1024 * 1024  # per-file ceiling for one note
IMPORT_DEFAULT_INCLUDE = ("*.md", "*.markdown", "*.mdown", "*.mkd")
IMPORT_MD_EXTS = {".md", ".markdown", ".mdown", ".mkd"}
_IMPORT_RENAME_MAX_SUFFIX = 10_000
# Directories that legitimately appear inside an external vault but must never be
# ingested (app/editor metadata, VCS, dependency trees, our own scratch/trash).
IMPORT_EXCLUDED_DIRS = {
    ".obsidian",
    ".trash",
    ".tmp",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "_templates",  # TEMPLATES_DIR
}
# Assets the importer may copy when --import-attachments is on (same allow-list as
# interactive uploads, so they pass save_attachment's validation unchanged).
IMPORT_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".pdf"}

# Obsidian embed `![[target]]` (incl. `![[a.png|300]]`, `![[note#heading]]`).
_EMBED_RE = re.compile(r"!\[\[([^\[\]\n]+?)\]\]")
# Standard markdown image `![alt](url "title")`.
_IMG_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")


def _replace_outside_code(pattern: re.Pattern, repl: Callable[[re.Match], str], text: str) -> str:
    """Like ``pattern.sub(repl, text)`` but skips matches inside fenced/inline code and
    frontmatter — match positions are found against a code-masked copy, then applied to
    the original text. Used by the importer so normalizing Obsidian ``![[embeds]]`` never
    rewrites a literal embed shown inside a code block (matches markdown_utils' masking)."""
    masked = _mask(text)
    out: list[str] = []
    last = 0
    for m in pattern.finditer(masked):
        out.append(text[last : m.start()])
        out.append(repl(m))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def walk_import_files(
    src: Path, include: tuple[str, ...], recurse: bool, warn: Callable[[str], None]
) -> Iterator[tuple[Path, str]]:
    """Yield (abs_path, source-relative POSIX path) for importable markdown files:
    prunes excluded/attachment dirs, never follows symlinks, and matches the
    ``include`` globs case-insensitively against the relative path."""
    from . import documents as dm

    def included(rel: str) -> bool:
        low = rel.lower()
        return any(fnmatch.fnmatchcase(low, pat.lower()) for pat in include)

    def consider(ap: Path, rel: str) -> tuple[Path, str] | None:
        if ap.is_symlink():
            warn(f"skipped {rel} (source symlink, not followed)")
            return None
        if not included(rel):
            return None
        return (ap, rel)

    if recurse:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            base = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in IMPORT_EXCLUDED_DIRS
                and d != dm.ATTACH_DIR
                and d != dm.TEMPLATES_DIR
                and not (base / d).is_symlink()
            )
            for fn in sorted(filenames):
                ap = base / fn
                got = consider(ap, ap.relative_to(src).as_posix())
                if got:
                    yield got
    else:
        for ap in sorted(src.iterdir()):
            if ap.is_dir():
                continue
            got = consider(ap, ap.name)
            if got:
                yield got


def import_from_directory(
    svc: DocumentService,
    principal: Principal,
    source_dir: str | Path,
    into: str = "",
    *,
    on_conflict: str = "skip",
    include: tuple[str, ...] = IMPORT_DEFAULT_INCLUDE,
    recurse: bool = True,
    import_attachments: bool = False,
    embed: bool = True,
    dry_run: bool = False,
) -> dict:
    """Bulk-ingest an external directory of markdown/Obsidian notes into the vault,
    routing every note through ``create()``/``update()`` so each gets a real
    revision, audit row, index entry, link backfill, and ``.md`` projection.

    Per file: classify the target (against the DB *and* an in-batch claim set so
    intra-batch case collisions resolve deterministically), then write — except in
    ``dry_run``, which classifies identically but skips every write, so the plan it
    prints exactly predicts the real run. Obsidian ``![[embeds]]`` are normalized
    to standard markdown (asset embeds → image links, note embeds → wikilinks) so
    they never enter the link graph as dangling ``.md`` references. Best-effort:
    one file's conflict / OS error is captured in ``errors`` and the rest proceed.
    """
    from . import documents as dm

    if not principal.can_write:
        raise ForbiddenError(f"Role '{principal.role}' cannot import documents (read/search only).")
    if on_conflict not in ("skip", "overwrite", "rename"):
        raise ValidationError("on_conflict must be 'skip', 'overwrite', or 'rename'.")
    src = Path(source_dir).expanduser().resolve()
    if not src.is_dir():
        raise ValidationError(f"source directory not found: {src}")
    vault = svc.vault.resolve()
    if src == vault or vault in src.parents or src in vault.parents:
        raise ValidationError("source directory overlaps the vault (self-import).")
    try:
        into_norm = normalize_folder_path(into)
    except PathError as e:
        raise ValidationError(str(e)) from None

    report: dict = {
        "created": 0,
        "revived": 0,
        "overwritten": 0,
        "skipped": 0,
        "renamed": 0,
        "scanned": 0,
        "embedded": 0,
        "attachments": {"copied": 0, "skipped": 0},
        "plan": [],
        "warnings": [],
        "errors": [],
        "broken_links": [],
        "dry_run": dry_run,
    }
    warn = report["warnings"].append
    claimed: set[str] = set()  # path_norm of targets created/planned this run
    imported: set[str] = set()  # path_norm actually written (broken-link report)
    asset_cache: dict[str, str] = {}  # resolved asset abs-path -> attachment url

    # -- attachment copy (only with import_attachments) -----------------
    def copy_asset(relpath: str, md_abs: Path) -> str | None:
        ref = relpath.split("#", 1)[0].strip()
        # Resolve the reference both relative to the markdown file (standard
        # markdown) and relative to the source root (Obsidian's vault-relative
        # style), taking the first that lands on a real file inside the source.
        # .resolve() collapses any symlink, so the escape check rejects links that
        # point outside --from. (Full Obsidian shortest-path search is out of scope.)
        candidate = None
        escaped = False
        for base in (md_abs.parent, src):
            try:
                c = (base / ref).resolve()
            except OSError:
                continue
            if c != src and src not in c.parents:
                escaped = True
                continue
            if c.is_file():
                candidate = c
                break
        if candidate is None:
            if escaped:
                warn(f"asset {relpath} (in {md_abs.name}) escapes the source dir; left as-is")
            else:
                warn(f"missing asset {relpath} referenced by {md_abs.name} (left as broken link)")
            report["attachments"]["skipped"] += 1
            return None
        key = str(candidate)
        if key in asset_cache:
            return asset_cache[key]
        ext = candidate.suffix.lower()
        if ext not in IMPORT_ATTACH_EXTS:
            warn(f"unsupported asset {relpath} ({ext or 'no ext'}) in {md_abs.name}; left as-is")
            report["attachments"]["skipped"] += 1
            return None
        try:
            data = candidate.read_bytes()
        except OSError:
            report["attachments"]["skipped"] += 1
            return None
        if not data or len(data) > dm.ATTACH_MAX_BYTES:
            warn(f"asset {relpath} in {md_abs.name} is empty or too large; left as-is")
            report["attachments"]["skipped"] += 1
            return None
        # Content-addressed: an identical asset already in the vault (e.g. a prior
        # import) is a no-op. Only count/plan/audit a genuinely new write so the
        # report reflects what actually hit disk (and a re-run reports copied=0).
        sub = dm._attachment_subname(candidate.name, ext, data)
        url = "/attachments/" + quote(sub)
        newly = not (svc.vault / dm.ATTACH_DIR / sub).exists()
        if newly and not dry_run:
            res = svc.save_attachment(principal, candidate.name, data)
            url = res["url"]
            audit.record_tx(
                svc.db,
                actor=principal.username,
                via=principal.via,
                action="attachment_upload",
                target=res["path"],
            )
        asset_cache[key] = url
        if newly:
            report["attachments"]["copied"] += 1
            report["plan"].append(
                {
                    "src": relpath,
                    "target": f"{dm.ATTACH_DIR}/{sub}",
                    "action": "attach",
                    "reason": None,
                }
            )
        return url

    # -- embed/asset normalization (always runs) ------------------------
    def normalize_body(raw: str, md_abs: Path) -> str:
        def embed_repl(m: re.Match) -> str:
            inner = m.group(1).strip()
            head = inner.split("|", 1)[0]
            target = head.split("#", 1)[0].strip()
            if not target:
                return m.group(0)
            last = target.rsplit("/", 1)[-1]
            ext = ("." + last.rsplit(".", 1)[-1].lower()) if "." in last else ""
            if ext == "" or ext in IMPORT_MD_EXTS:
                # Note transclusion -> a plain wikilink (resolves by name, no '!').
                anchor = ("#" + head.split("#", 1)[1]) if "#" in head else ""
                return f"[[{target}{anchor}]]"
            # Asset embed -> a standard image link (never a graph wikilink).
            url = copy_asset(target, md_abs) if import_attachments else None
            return f"![{last}]({url or target})"

        out = _replace_outside_code(_EMBED_RE, embed_repl, raw)
        if import_attachments:

            def img_repl(m: re.Match) -> str:
                alt, url = m.group(1), m.group(2).strip()
                if not url or url[0] in "#/" or url.startswith("//") or SCHEME_RE.match(url):
                    return m.group(0)
                new = copy_asset(url, md_abs)
                return f"![{alt}]({new})" if new else m.group(0)

            out = _replace_outside_code(_IMG_RE, img_repl, out)
        return out

    # -- target path: extension-normalize, prefix --into, validate ------
    def target_for(source_rel: str) -> str:
        p, low = source_rel, source_rel.lower()
        for e in (".markdown", ".mdown", ".mkd"):
            if low.endswith(e):
                p = p[: -len(e)] + ".md"
                break
        combined = f"{into_norm}/{p}" if into_norm else p
        return normalize_rel_path(combined)

    def free_variant(target_rel: str) -> str:
        base = target_rel[:-3]  # strip the guaranteed lowercase '.md'
        with svc.db.reader() as conn:
            for n in range(2, dm._IMPORT_RENAME_MAX_SUFFIX + 1):
                cand = f"{base}-{n}.md"
                cnorm = path_norm(cand)
                if cnorm in claimed:
                    continue
                if conn.execute("SELECT 1 FROM documents WHERE path_norm=?", (cnorm,)).fetchone():
                    continue
                return cand
        raise ValidationError(
            f"no free rename variant for {target_rel} ({dm._IMPORT_RENAME_MAX_SUFFIX} tried)."
        )

    # -- per-file classify + (optionally) write -------------------------
    def handle(md_abs: Path, source_rel: str) -> None:
        try:
            size = md_abs.stat().st_size
        except OSError:
            warn(f"skipped {source_rel} (vanished before read)")
            return
        if size > dm.IMPORT_MAX_BYTES:
            warn(f"skipped {source_rel} (file too large)")
            if not dry_run:
                audit.record_tx(
                    svc.db,
                    actor=principal.username,
                    via=principal.via,
                    action="doc_import_skip",
                    target=source_rel,
                    outcome="skipped",
                    detail="file too large",
                )
            return
        try:
            raw = md_abs.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            report["errors"].append({"path": source_rel, "error": str(e)})
            return
        if "�" in raw:
            warn(f"encoding replaced in {source_rel} (invalid UTF-8)")  # U+FFFD present
        if not raw.strip():
            warn(f"skipped {source_rel} (empty)")
            return

        target_rel = target_for(source_rel)
        content = normalize_body(raw, md_abs)
        chash = sha256_hex(content)
        norm = path_norm(target_rel)

        with svc.db.reader() as conn:
            row = conn.execute(
                "SELECT id, version, is_deleted, content_hash FROM documents WHERE path_norm=?",
                (norm,),
            ).fetchone()
        in_batch = norm in claimed
        live = bool(row and not row["is_deleted"])

        # Idempotent re-run: identical content already live -> no-op skip.
        if live and row["content_hash"] == chash:
            report["plan"].append(
                {
                    "src": source_rel,
                    "target": target_rel,
                    "action": "skip",
                    "reason": "unchanged",
                }
            )
            report["skipped"] += 1
            return

        final_rel = target_rel
        base_version: int | None = None
        reason: str | None = None
        if not row and not in_batch:
            action = "create"
        elif row and row["is_deleted"] and not in_batch:  # tombstone
            if on_conflict == "rename":
                final_rel, action, reason = free_variant(target_rel), "rename", "tombstone"
            else:
                action, reason = "revive", "tombstone"
        else:  # live conflict (DB row or already claimed this batch)
            if in_batch:
                warn(
                    f"case collision: {source_rel} maps to an already-imported path "
                    f"({target_rel}); applying on_conflict={on_conflict}"
                )
            if on_conflict == "skip":
                report["plan"].append(
                    {
                        "src": source_rel,
                        "target": target_rel,
                        "action": "skip",
                        "reason": "exists",
                    }
                )
                report["skipped"] += 1
                if not dry_run:
                    audit.record_tx(
                        svc.db,
                        actor=principal.username,
                        via=principal.via,
                        action="doc_import_skip",
                        target=target_rel,
                        outcome="conflict",
                        detail="exists",
                    )
                return
            if on_conflict == "overwrite":
                action = "overwrite"
                base_version = row["version"] if live else None
            else:
                final_rel, action = free_variant(target_rel), "rename"

        report["plan"].append(
            {"src": source_rel, "target": final_rel, "action": action, "reason": reason}
        )
        claimed.add(path_norm(final_rel))

        if dry_run:
            report[
                {
                    "create": "created",
                    "revive": "revived",
                    "overwrite": "overwritten",
                    "rename": "renamed",
                }[action]
            ] += 1
            if embed:  # predict the post-commit embed the real run would do
                report["embedded"] += 1
            return

        try:
            if action == "overwrite":
                svc.update(principal, final_rel, base_version, content, embed=embed)
                report["overwritten"] += 1
            else:  # create / revive / rename all create() at final_rel
                svc.create(principal, final_rel, content, embed=embed)
                report[
                    "created"
                    if action == "create"
                    else "revived"
                    if action == "revive"
                    else "renamed"
                ] += 1
        except ConflictError as e:
            report["errors"].append({"path": source_rel, "error": e.message})
            return
        imported.add(path_norm(final_rel))
        if embed:
            report["embedded"] += 1

    # -- walk + process -------------------------------------------------
    for md_abs, source_rel in walk_import_files(src, include, recurse, warn):
        report["scanned"] += 1
        try:
            handle(md_abs, source_rel)
        except PathError as e:
            warn(f"skipped {source_rel} ({e})")
        except WikiError as e:
            report["errors"].append({"path": source_rel, "error": e.message})
        except OSError as e:
            report["errors"].append({"path": source_rel, "error": str(e)})

    if not dry_run and imported:
        with svc.db.reader() as conn:
            broken = graph.list_broken_links(conn, 2000)
        report["broken_links"] = [b for b in broken if path_norm(b["src_path"]) in imported]
    return report
