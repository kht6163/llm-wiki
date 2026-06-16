"""CLI implementation: serve (web + MCP), init-db, create-admin, create-user,
create-api-key, reindex."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import io
import json
import signal
import tarfile
import tempfile
from pathlib import Path

import uvicorn

from .config import ConfigError, get_settings
from .db import SCHEMA_VERSION, get_meta
from .mcp_server import create_mcp_server
from .runtime import build_context
from .services import users as users_svc
from .services.auth import Principal, create_api_key, create_user
from .services.errors import WikiError
from .util import now_iso
from .web import create_web_app

SNAPSHOT_FORMAT = "llm-wiki-snapshot"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="llm-wiki", description="Obsidian-like markdown wiki with a web UI and an HTTP MCP server.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the web UI and the MCP server.")
    s.add_argument("--host")
    s.add_argument("--gui-port", type=int)
    s.add_argument("--mcp-port", type=int)
    s.add_argument("--no-recover", action="store_true", help="Skip re-projecting pending files on startup.")

    sub.add_parser("init-db", help="Create the database schema and bind the embedding model.")

    a = sub.add_parser("create-admin", help="Create the first admin user.")
    a.add_argument("--username")
    a.add_argument("--password")
    a.add_argument("--force", action="store_true", help="Create even if an admin already exists.")

    u = sub.add_parser("create-user", help="Create a user.")
    u.add_argument("--username")
    u.add_argument("--password")
    u.add_argument("--role", default="editor", choices=["admin", "editor", "viewer"])

    k = sub.add_parser("create-api-key", help="Mint an MCP API key for a user (printed once).")
    k.add_argument("--username", required=True)
    k.add_argument("--name", default="cli")

    r = sub.add_parser("reindex", help="Reconcile the DB with the on-disk vault (external edits).")
    r.add_argument("--reembed", action="store_true", help="Recompute all embeddings.")

    im = sub.add_parser(
        "import", help="Bulk-import an external directory of markdown/Obsidian notes.")
    im.add_argument("--from", dest="from_", required=True,
                    help="Source directory of markdown/Obsidian notes.")
    im.add_argument("--into", required=True,
                    help="Target vault folder (pass --into '' for the root).")
    im.add_argument("--on-conflict", choices=["skip", "overwrite", "rename"], default="skip",
                    help="What to do when a live document already occupies the target path.")
    im.add_argument("--include", action="append",
                    help="Glob(s) of source-relative paths to import (default: markdown files).")
    im.add_argument("--no-recurse", action="store_true",
                    help="Only import files directly in --from (don't descend).")
    im.add_argument("--import-attachments", action="store_true",
                    help="Also copy referenced images/files into _attachments and rewrite links.")
    im.add_argument("--no-embed", action="store_true",
                    help="Skip embedding now (leaves docs for a later 'reindex --reembed').")
    im.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")
    im.add_argument("--force", action="store_true",
                    help="Required to actually overwrite (with --on-conflict overwrite).")

    b = sub.add_parser("backup", help="Write a consistent (WAL-safe) copy of the database.")
    b.add_argument("--out", required=True, help="Destination .db path for the snapshot.")

    sn = sub.add_parser("snapshot", help="Full snapshot (DB + vault + manifest) as a single .tar.")
    sn.add_argument("--out", required=True, help="Destination .tar path.")
    sn.add_argument("--force", action="store_true", help="Overwrite an existing .tar.")

    rs = sub.add_parser("restore", help="Restore a full snapshot .tar (DB + vault).")
    rs.add_argument("--in", dest="in_", required=True, help="Snapshot .tar to restore from.")
    rs.add_argument("--force", action="store_true",
                    help="Overwrite even if the target DB/vault is not empty.")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except ConfigError as e:
        print(f"configuration error: {e}")
        return 2
    except WikiError as e:
        print(f"error: {e.message}")
        return 1
    except KeyboardInterrupt:
        return 0


def _dispatch(args) -> int:
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "init-db":
        return _init_db()
    if args.cmd == "create-admin":
        return _create_admin(args)
    if args.cmd == "create-user":
        return _create_user(args)
    if args.cmd == "create-api-key":
        return _create_api_key(args)
    if args.cmd == "reindex":
        return _reindex(args)
    if args.cmd == "import":
        return _import(args)
    if args.cmd == "backup":
        return _backup(args)
    if args.cmd == "snapshot":
        return _snapshot(args)
    if args.cmd == "restore":
        return _restore(args)
    return 2


def _init_db() -> int:
    ctx = build_context(full=True)
    print(f"Initialized database at {ctx.settings.db_path}")
    print(f"Vault: {ctx.settings.vault_path}")
    print(f"Embedding model: {ctx.settings.embedding_model} (dim={ctx.embedder.dim})")
    return 0


def _create_admin(args) -> int:
    ctx = build_context(full=False)
    if users_svc.count_admins(ctx.db) > 0 and not args.force:
        print("An admin already exists. Use 'create-user', or pass --force to add another admin.")
        return 1
    username = args.username or input("Admin username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    uid = create_user(ctx.db, username, password, "admin")
    print(f"Created admin '{username}' (id={uid}).")
    return 0


def _create_user(args) -> int:
    ctx = build_context(full=False)
    username = args.username or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    uid = create_user(ctx.db, username, password, args.role)
    print(f"Created user '{username}' (id={uid}, role={args.role}).")
    return 0


def _create_api_key(args) -> int:
    ctx = build_context(full=False)
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (args.username,)).fetchone()
    if not row:
        print(f"No such user: {args.username}")
        return 1
    token = create_api_key(ctx.db, row["id"], args.name)
    print(token)
    print("(store this now — it is not shown again)", flush=True)
    return 0


def _reindex(args) -> int:
    ctx = build_context(full=True)
    print("Reindexing vault…")
    res = ctx.docs.reindex_all(reembed=args.reembed)
    print(f"created={res['created']} updated={res['updated']} unchanged={res['unchanged']} "
          f"embedded={res['embedded']}")
    if res["missing_files"]:
        print(f"WARNING: {len(res['missing_files'])} document(s) have no file on disk:")
        for m in res["missing_files"]:
            print(f"  - {m}")
    return 0


_IMPORT_LABELS = {"create": "CREATE", "revive": "REVIVE", "skip": "SKIP",
                  "rename": "RENAME", "overwrite": "OVERWRITE"}


def _import(args) -> int:
    """Thin wrapper over DocumentService.import_from_directory: validate the source,
    gate destructive overwrite, attribute to an admin/editor, then print the report."""
    src = Path(args.from_).expanduser()
    if not src.is_dir():
        print(f"configuration error: source directory not found: {src}")
        return 2
    if args.on_conflict == "overwrite" and not args.dry_run and not args.force:
        print("error: overwrite mode is destructive; re-run with --dry-run to preview "
              "or pass --force.")
        return 1
    ctx = build_context(full=True)
    vault = Path(ctx.settings.vault_path).resolve()
    srcr = src.resolve()
    if srcr == vault or vault in srcr.parents or srcr in vault.parents:
        print(f"configuration error: --from overlaps the vault (self-import): {src}")
        return 2

    # Attribute the import to a real user so revisions/audit carry an author (prefer
    # an admin); via='cli-import' distinguishes a bulk import from manual CLI edits.
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE is_active=1 AND role IN ('admin','editor') "
            "ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id LIMIT 1").fetchone()
    if not row:
        print("error: no admin or editor user to attribute the import to; run "
              "'create-admin' first.")
        return 1
    principal = Principal(row["id"], row["username"], row["role"], via="cli-import")

    kwargs: dict = dict(
        into=args.into, on_conflict=args.on_conflict, recurse=not args.no_recurse,
        import_attachments=args.import_attachments, embed=not args.no_embed,
        dry_run=args.dry_run)
    if args.include:
        kwargs["include"] = tuple(args.include)
    report = ctx.docs.import_from_directory(principal, srcr, **kwargs)
    _print_import_report(report, srcr, args.into, args.dry_run, args.import_attachments)
    return 1 if report["errors"] else 0


def _print_import_report(report: dict, src: Path, into: str, dry_run: bool,
                         show_attach: bool) -> None:
    dest = into.strip("/") if into and into.strip("/") else "(root)"
    print(f"{'Importing' if dry_run else 'Imported'} from {src} into vault/{dest}:")
    docs = [p for p in report["plan"] if p["action"] != "attach"]
    for p in sorted(docs, key=lambda x: x["target"].lower()):
        tag = _IMPORT_LABELS.get(p["action"], p["action"].upper())
        if p["action"] == "rename":
            print(f"  {tag:<9} {p['src']} -> {p['target']}")
        elif p.get("reason"):
            print(f"  {tag:<9} {p['target']} ({p['reason']})")
        else:
            print(f"  {tag:<9} {p['target']}")
    if show_attach:
        for p in sorted((q for q in report["plan"] if q["action"] == "attach"),
                        key=lambda x: x["target"].lower()):
            print(f"  {'ATTACH':<9} {p['src']} -> {p['target']}")

    a = report["attachments"]
    if dry_run:
        line = (f"Would create {report['created']}, revive {report['revived']}, "
                f"skip {report['skipped']}, rename {report['renamed']}, "
                f"overwrite {report['overwritten']} ({report['scanned']} files scanned)")
        line += (f"; attachments {a['copied']} copied, {a['skipped']} skipped."
                 if show_attach else ".")
    else:
        line = (f"created={report['created']} revived={report['revived']} "
                f"skipped={report['skipped']} renamed={report['renamed']} "
                f"overwritten={report['overwritten']} ({report['scanned']} scanned)")
        if show_attach:
            line += f" · attachments {a['copied']}/{a['skipped']}"
        line += f" · embedded={report['embedded']}"
    print(line)

    for w in report["warnings"]:
        print(f"  WARNING: {w}")
    for e in report["errors"]:
        print(f"  ERROR: {e['path']}: {e['error']}")
    if not dry_run and report["broken_links"]:
        print(f"{len(report['broken_links'])} link(s) created by this import are still "
              f"unresolved.")


def _backup(args) -> int:
    ctx = build_context(full=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"refusing to overwrite existing file: {out}")
        return 1
    # VACUUM INTO produces a transactionally-consistent snapshot even while WAL is
    # active — unlike a naive file copy, which can capture a torn .db + .db-wal.
    with ctx.db.reader() as conn:
        conn.execute("VACUUM INTO ?", (str(out),))
    print(f"Database backed up to {out}")
    print(f"NOTE: also back up the vault directory for a complete snapshot: {ctx.settings.vault_path}")
    print("      (or use 'llm-wiki snapshot' to capture DB + vault together)")
    return 0


def _snapshot(args) -> int:
    """Single-file snapshot: a WAL-consistent DB copy + the whole vault (minus the
    .tmp scratch dir) + a manifest, packed as one .tar. The companion of `restore`."""
    ctx = build_context(full=False)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"refusing to overwrite existing file (use --force): {out}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    vault = Path(ctx.settings.vault_path)

    with ctx.db.reader() as conn:
        schema_version = get_meta(conn, "schema_version")
        manifest = {
            "format": SNAPSHOT_FORMAT,
            "format_version": 1,
            "schema_version": int(schema_version) if schema_version else None,
            "embedding_model": get_meta(conn, "embedding_model"),
            "embedding_dim": (lambda v: int(v) if v else None)(get_meta(conn, "embedding_dim")),
            "doc_count": conn.execute(
                "SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0],
            "created_at": now_iso(),
        }

    with tempfile.TemporaryDirectory() as td:
        snap_db = Path(td) / "wiki.db"
        # VACUUM INTO: a transactionally-consistent copy even with WAL active.
        with ctx.db.reader() as conn:
            conn.execute("VACUUM INTO ?", (str(snap_db),))
        files = 0
        with tarfile.open(out, "w") as tar:
            tar.add(snap_db, arcname="wiki.db")
            if vault.exists():
                for f in sorted(vault.rglob("*")):
                    rel = f.relative_to(vault)
                    if rel.parts and rel.parts[0] == ".tmp":  # scratch dir for atomic writes
                        continue
                    if f.is_file():
                        tar.add(f, arcname=str(Path("vault") / rel))
                        files += 1
            data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    print(f"Snapshot written to {out}")
    print(f"  schema v{manifest['schema_version']} · {manifest['doc_count']} docs · "
          f"{files} vault file(s) · model {manifest['embedding_model']}")
    return 0


def _restore(args) -> int:
    """Restore a snapshot .tar over the configured DB + vault. Validates the manifest
    before touching anything, refuses a non-empty target without --force, then
    re-projects any pending docs so DB and vault converge."""
    settings = get_settings()
    src = Path(args.in_)
    if not src.exists():
        print(f"no such snapshot: {src}")
        return 1
    db_path, vault = Path(settings.db_path), Path(settings.vault_path)

    # 1) Read + validate the manifest BEFORE clobbering anything.
    try:
        with tarfile.open(src, "r") as tar:
            mf = tar.extractfile("manifest.json")
            manifest = json.load(mf) if mf else None
    except (tarfile.TarError, KeyError, json.JSONDecodeError):
        manifest = None
    if not manifest or manifest.get("format") != SNAPSHOT_FORMAT:
        print("not a recognizable llm-wiki snapshot (missing/invalid manifest.json).")
        return 1
    snap_sv = manifest.get("schema_version")
    if snap_sv is not None and int(snap_sv) > SCHEMA_VERSION:
        print(f"snapshot schema_version {snap_sv} is newer than this build supports "
              f"({SCHEMA_VERSION}); upgrade llm-wiki before restoring.")
        return 1

    # 2) Refuse to overwrite a populated target unless forced.
    db_nonempty = db_path.exists() and db_path.stat().st_size > 0
    vault_nonempty = vault.exists() and any(vault.iterdir())
    if (db_nonempty or vault_nonempty) and not args.force:
        print("target database or vault is not empty; pass --force to overwrite.")
        return 1

    # 3) Extract (path-traversal-safe), clearing stale WAL sidecars first.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    for sfx in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + sfx)
        if sidecar.exists():
            sidecar.unlink()
    with tarfile.open(src, "r") as tar:
        for member in tar.getmembers():
            if member.name == "manifest.json" or not member.isfile():
                continue
            if member.name == "wiki.db":
                dest = db_path
            elif member.name.startswith("vault/"):
                safe = _safe_join(vault, member.name[len("vault/"):])
                if safe is None:
                    print(f"  skipped unsafe path in snapshot: {member.name}")
                    continue
                dest = safe
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            with open(dest, "wb") as out_f:
                out_f.write(extracted.read())

    # 4) Warn on a model mismatch and re-project pending docs.
    if manifest.get("embedding_model") and manifest["embedding_model"] != settings.embedding_model:
        print(f"WARNING: snapshot embedding_model '{manifest['embedding_model']}' differs from "
              f"configured '{settings.embedding_model}'. Run 'llm-wiki reindex --reembed'.")
    ctx = build_context(full=False)  # opens the restored DB and applies any migrations
    recovered = ctx.docs.recover_pending()
    print(f"Restored from {src}: schema v{snap_sv} · {manifest.get('doc_count')} docs.")
    if recovered:
        print(f"  re-projected {recovered} pending document(s).")
    return 0


def _safe_join(base: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``base``, or None if it escapes (tar path traversal)."""
    base = base.resolve()
    target = (base / rel).resolve()
    if target == base or base in target.parents:
        return target
    return None


def _serve(args) -> int:
    settings = get_settings()
    if args.host:
        settings.host = args.host
    if args.gui_port:
        settings.gui_port = args.gui_port
    if args.mcp_port:
        settings.mcp_port = args.mcp_port

    from .logconf import configure_logging
    configure_logging(settings.log_level, settings.log_file)

    print("Loading embedding model… (first run downloads it from HuggingFace)")
    ctx = build_context(settings, full=True)
    ctx.embedder.warm()  # load weights now so the first request isn't slow / failing readiness
    if not args.no_recover:
        n = ctx.docs.recover_pending()
        if n:
            print(f"Recovered {n} pending file(s) from the last run.")
        # A crash can also leave committed docs flagged vector_dirty=1 whose embedding
        # never ran (it happens post-commit, off the write lock). Sweep them now so they
        # don't silently stay out of vector search until the next reindex.
        embedded = ctx.docs.embed_pending()
        if embedded:
            print(f"Re-embedded {embedded} document(s) left pending by the last run.")

    web_app = create_web_app(ctx)
    mcp_app = create_mcp_server(ctx).streamable_http_app()

    # Unauthenticated health + metrics routes on the MCP app (for orchestrators /
    # probes / Prometheus scraping the MCP port). The metrics registry is shared
    # across both servers, so this exposes the same data as the web /metrics.
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    from .metrics import render_latest

    async def _mcp_health(_req):
        return JSONResponse({"ok": True, "model_loaded": ctx.embedder.is_loaded})

    async def _mcp_metrics(_req):
        body, ctype = render_latest()
        return Response(content=body, media_type=ctype)

    mcp_app.router.routes.append(Route("/healthz", _mcp_health, methods=["GET"]))
    mcp_app.router.routes.append(Route("/metrics", _mcp_metrics, methods=["GET"]))

    print(f"Web UI : http://{settings.host}:{settings.gui_port}")
    print(f"MCP    : http://{settings.host}:{settings.mcp_port}/mcp  (Authorization: Bearer <api_key>)")
    asyncio.run(_serve_both(settings, web_app, mcp_app))
    return 0


async def _serve_both(settings, web_app, mcp_app) -> None:
    # Bound the graceful-shutdown wait so an in-flight request (e.g. a multi-second
    # embed) can't keep the process alive past an orchestrator's kill grace, which
    # would escalate to SIGKILL and risk a torn WAL. A second signal forces an
    # immediate exit.
    grace = settings.shutdown_grace_s
    web_cfg = uvicorn.Config(web_app, host=settings.host, port=settings.gui_port,
                             log_level="info", timeout_graceful_shutdown=grace)
    mcp_cfg = uvicorn.Config(mcp_app, host=settings.host, port=settings.mcp_port,
                             log_level="info", timeout_graceful_shutdown=grace)
    servers = [uvicorn.Server(web_cfg), uvicorn.Server(mcp_cfg)]
    for s in servers:
        s.install_signal_handlers = lambda: None  # type: ignore[attr-defined]  # we manage signals for both at once

    loop = asyncio.get_running_loop()
    signalled = False

    def _shutdown() -> None:
        nonlocal signalled
        for s in servers:
            if signalled:
                s.force_exit = True  # second signal -> drop in-flight requests now
            s.should_exit = True
        signalled = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    await asyncio.gather(*(s.serve() for s in servers))
