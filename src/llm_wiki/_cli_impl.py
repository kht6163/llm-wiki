"""CLI implementation: serve (web + MCP), init-db, create-admin, create-user,
create-api-key, reindex."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import signal
import sqlite3
from pathlib import Path
from typing import cast

import uvicorn
from pydantic import ValidationError

from .config import ConfigError, get_settings
from .db import SCHEMA_VERSION as DB_SCHEMA_VERSION
from .db import get_meta
from .mcp_server import create_mcp_server
from .process_lock import ProjectLock, ProjectLockError
from .runtime import build_context
from .services import audit
from .services import users as users_svc
from .services.auth import Principal, create_api_key, create_user
from .services.errors import WikiError
from .snapshot import (
    _recover_pending_restore,
    restore_snapshot,
    validate_restore_layout,
    write_snapshot,
)
from .web import create_web_app
from .web.security import RequestBodyLimitMiddleware

SCHEMA_VERSION = DB_SCHEMA_VERSION


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

    pr = sub.add_parser("prune", help="Delete old revisions / audit rows and reclaim space.")
    pr.add_argument("--keep", type=int, default=20,
                    help="Revisions to keep per document (most recent N; min 1). Default 20.")
    pr.add_argument("--older-than-days", type=int, default=90,
                    help="Delete audit_log rows older than this many days (0 = keep all). Default 90.")
    pr.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM after deleting.")
    pr.add_argument("--force", action="store_true",
                    help="Actually delete. Without it, prints a dry-run preview only.")

    dc = sub.add_parser("db-check", help="Check the database for corruption (integrity + foreign keys + orphan vectors).")
    dc.add_argument("--quick", action="store_true",
                    help="Use the faster PRAGMA quick_check (skips per-index ordering scan).")
    dc.add_argument("--fix-orphan-vectors", action="store_true",
                    help="Delete chunk_vectors rows whose chunk no longer exists (safe repair).")
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
    # Configure the llm_wiki logger tree for EVERY command, not just serve, so the
    # INFO logs of maintenance commands (reindex/import/prune/restore…) reach stderr and
    # the optional LOG_FILE instead of being dropped by Python's last-resort handler.
    from .config import get_settings
    from .logconf import configure_logging
    try:
        s = get_settings()
        configure_logging(s.log_level, s.log_file)
    except ConfigError:
        pass  # bad config surfaces with a clear message when the command builds context
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
    if args.cmd == "prune":
        return _prune(args)
    if args.cmd == "db-check":
        return _db_check(args)
    return 2


def _init_db() -> int:
    ctx = build_context(full=True)
    print(f"Initialized database at {ctx.settings.db_path}")
    print(f"Vault: {ctx.settings.vault_path}")
    print(f"Embedding model: {ctx.settings.embedding_model} (dim={ctx.embedder.dim})")
    return 0


def _os_actor() -> str:
    """Best-effort OS login name to attribute a CLI credential action to. The audit
    row also records via='cli', so this just answers 'which operator on the host ran
    it'. Falls back to '-' when the login name can't be resolved."""
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "-"


def _create_admin(args) -> int:
    ctx = build_context(full=False)
    if users_svc.count_admins(ctx.db) > 0 and not args.force:
        print("An admin already exists. Use 'create-user', or pass --force to add another admin.")
        return 1
    username = args.username or input("Admin username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    uid = create_user(
        ctx.db,
        username,
        password,
        "admin",
        audit_actor=_os_actor(),
        audit_via="cli",
    )
    print(f"Created admin '{username}' (id={uid}).")
    return 0


def _create_user(args) -> int:
    ctx = build_context(full=False)
    username = args.username or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    uid = create_user(
        ctx.db,
        username,
        password,
        args.role,
        audit_actor=_os_actor(),
        audit_via="cli",
    )
    print(f"Created user '{username}' (id={uid}, role={args.role}).")
    return 0


def _create_api_key(args) -> int:
    ctx = build_context(full=False)
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id,username,role,credential_version FROM users "
            "WHERE username=? AND is_active=1",
            (args.username,),
        ).fetchone()
    if not row:
        print(f"No such active user: {args.username}")
        return 1
    principal = Principal(
        row["id"],
        row["username"],
        row["role"],
        via="cli",
        credential_version=row["credential_version"],
    )
    token = create_api_key(
        ctx.db,
        principal,
        args.name,
        audit_actor=_os_actor(),
        audit_via="cli",
        audit_detail=f"user={args.username}",
    )
    print(token)
    print("(store this now — it is not shown again)", flush=True)
    return 0


def _reindex(args) -> int:
    if args.reembed:
        # --reembed is also the supported way to CHANGE the embedding model. The normal
        # build (full=True) calls db.initialize(), which refuses a model change to protect
        # the fixed vector dimension — so it would crash before we could re-embed. Build
        # with full=False to skip that guard, then rebind: recreate the vector table at the
        # current model's dimension and update the binding, before re-embedding every doc.
        ctx = build_context(full=False)
        with ctx.db.reader() as conn:
            prev_model = get_meta(conn, "embedding_model")
            prev_dim = get_meta(conn, "embedding_dim")
        new_dim = ctx.embedder.dim  # loads the (possibly new) model
        if prev_model and prev_model != ctx.settings.embedding_model:
            print(f"Rebinding embedding model: {prev_model} (dim {prev_dim}) -> "
                  f"{ctx.settings.embedding_model} (dim {new_dim})")
        ctx.db.rebind_model(
            ctx.settings.embedding_model, new_dim, ctx.embedder.pipeline
        )
        print("Reindexing vault (re-embedding all documents)…")
    else:
        ctx = build_context(full=True)
        print("Reindexing vault…")
    def _progress(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        end = "\n" if done >= total else ""
        print(f"\r  embedding {done}/{total} chunks ({pct}%)…", end=end, flush=True)

    res = ctx.docs.reindex_all(reembed=args.reembed, progress=_progress)
    print(f"created={res['created']} updated={res['updated']} renamed={res['renamed']} "
          f"unchanged={res['unchanged']} embedded={res['embedded']}")
    conflicts = res.get("skipped_conflicts", [])
    print(f"recovered_pending={res.get('recovered_pending', 0)} "
          f"retried={res.get('retried', 0)} conflicts={len(conflicts)}")
    for mv in res.get("renames", []):
        print(f"  renamed: {mv}")
    if res.get("skipped_deleted"):
        print(f"WARNING: {len(res['skipped_deleted'])} tombstoned document(s) were skipped:")
        for path in res["skipped_deleted"]:
            print(f"  - {path}")
    if res["missing_files"]:
        print(f"WARNING: {len(res['missing_files'])} document(s) have no file on disk:")
        for m in res["missing_files"]:
            print(f"  - {m}")
    if conflicts:
        print(f"ERROR: {len(conflicts)} path(s) did not converge:")
        for conflict in conflicts:
            print(f"  - {conflict['path']}: {conflict['reason']} "
                  f"(attempts={conflict['attempts']})")
        return 1
    return 0


_IMPORT_LABELS = {"create": "CREATE", "revive": "REVIVE", "skip": "SKIP",
                  "rename": "RENAME", "overwrite": "OVERWRITE"}


def _import(args) -> int:
    """Thin wrapper over DocumentService.import_from_directory: validate the source,
    gate destructive overwrite, attribute to the local CLI operator, then print the report."""
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

    # The host CLI is a trusted write surface, but no wiki user authenticated this
    # command. Keep nullable user FKs anonymous instead of impersonating whichever
    # admin/editor happens to sort first; the audit actor identifies the OS operator.
    principal = Principal(cast(int, None), _os_actor(), "editor", via="cli")

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


def _prune(args) -> int:
    ctx = build_context(full=False)
    apply = args.force
    rev = ctx.docs.prune_revisions(keep=args.keep, apply=apply)
    print(f"revisions: {rev['deletable_revisions']} prunable "
          f"(keeping the latest {rev['keep']} per document)")
    if args.older_than_days > 0:
        aud = audit.prune(ctx.db, older_than_days=args.older_than_days, apply=apply)
        print(f"audit_log: {aud['deletable_events']} row(s) older than "
              f"{args.older_than_days}d (before {aud['cutoff']})")
    if not apply:
        print("\nDry run — nothing deleted. Re-run with --force to apply.")
        return 0
    if not args.no_vacuum:
        print("Reclaiming space (VACUUM)…")
        with ctx.db.reader() as conn:
            conn.execute("VACUUM")
    print("Prune complete.")
    return 0


def _db_check(args) -> int:
    ctx = build_context(full=False)
    # An opt-in repair runs first so the subsequent check reflects the fixed state.
    if args.fix_orphan_vectors:
        removed = ctx.db.delete_orphan_vectors()
        print(f"orphan vectors: removed {removed}")
    report = ctx.db.integrity_check(quick=args.quick)
    if report["ok"]:
        print(f"integrity ({report['check']}): ok")
        print("foreign keys: ok")
        print("orphan vectors: ok")
        return 0
    print(f"integrity ({report['check']}): "
          f"{'ok' if report['integrity'] == ['ok'] else 'FAILED'}")
    for line in report["integrity"]:
        if line != "ok":
            print(f"  - {line}")
    fk = report["foreign_key_violations"]
    if fk:
        print(f"foreign keys: {len(fk)} violation(s)")
        for v in fk:
            print(f"  - table={v['table']} rowid={v['rowid']} "
                  f"parent={v['parent']} fkid={v['fkid']}")
    else:
        print("foreign keys: ok")
    orphans = report["orphan_vectors"]
    if orphans:
        print(f"orphan vectors: {orphans} (run with --fix-orphan-vectors to remove)")
    else:
        print("orphan vectors: ok")
    return 1


def _backup(args) -> int:
    ctx = build_context(full=False)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"refusing to overwrite existing file: {out}")
        return 1
    # VACUUM INTO produces a transactionally-consistent snapshot even while WAL is
    # active — unlike a naive file copy, which can capture a torn .db + .db-wal.
    try:
        with ctx.db.reader() as conn:
            conn.execute("VACUUM INTO ?", (str(out),))
    except sqlite3.OperationalError as e:
        # Disk full / permission denied / I/O error: report it cleanly (not a raw
        # traceback) and remove any partial file so a failed backup can't masquerade
        # as a usable one.
        out.unlink(missing_ok=True)
        print(f"backup failed: {e}")
        return 1
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
    vault = Path(ctx.settings.vault_path)
    report = write_snapshot(ctx.db, vault, out, force=args.force)
    print(f"Snapshot written to {out}")
    print(f"  schema v{report.schema_version} · {report.doc_count} docs · "
          f"{report.file_count} vault file(s) · model {report.embedding_model}")
    return 0


def _restore(args) -> int:
    """Restore a validated snapshot as a full DB + vault replacement."""
    settings = get_settings()
    src = Path(args.in_)
    if not src.exists():
        print(f"no such snapshot: {src}")
        return 1
    db_path, vault = Path(settings.db_path), Path(settings.vault_path)
    try:
        report = restore_snapshot(src, db_path, vault, force=args.force)
    except FileExistsError:
        print("target database or vault is not empty; pass --force to overwrite.")
        return 1
    except ProjectLockError as exc:
        print(f"restore failed: {exc}")
        return 1
    except (OSError, ValueError) as exc:
        print(f"restore failed: {exc}")
        return 1

    ctx = None
    recovered = 0
    post_error: BaseException | None = None
    try:
        if report.embedding_model and report.embedding_model != settings.embedding_model:
            print(f"WARNING: snapshot embedding_model '{report.embedding_model}' differs from "
                  f"configured '{settings.embedding_model}'. Run 'llm-wiki reindex --reembed'.")
        ctx = build_context(full=False)  # opens the restored DB and applies any migrations
        recovered = ctx.docs.recover_pending()
    except BaseException as exc:
        post_error = exc
    finally:
        if ctx is not None:
            try:
                ctx.db.close()
            except BaseException as exc:
                if post_error is None:
                    post_error = exc
    if post_error is not None:
        try:
            report.rollback(post_error)
        except (OSError, ValueError, RuntimeError) as rollback_exc:
            print(f"restore failed: {rollback_exc}")
            return 1
        raise AssertionError("restore rollback unexpectedly returned") from post_error

    report.finalize()
    for backup in report.backup_cleanup_warnings:
        print(
            "WARNING: restored successfully but backup cleanup failed; "
            f"backup preserved at {backup}"
        )
    print(f"Restored from {src}: schema v{report.schema_version} · {report.doc_count} docs.")
    if recovered:
        print(f"  re-projected {recovered} pending document(s).")
    return 0


def _apply_serve_overrides(settings, args):
    """Merge --host/--gui-port/--mcp-port onto the loaded settings and RE-VALIDATE.
    Plain attribute assignment doesn't re-run Settings' validators (no
    validate_assignment), so without this a CLI override could slip an out-of-range
    port — or two identical ports — past the distinct-ports check and only fail at
    bind() time with an opaque 'address already in use'."""
    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.gui_port is not None:
        overrides["gui_port"] = args.gui_port
    if args.mcp_port is not None:
        overrides["mcp_port"] = args.mcp_port
    if not overrides:
        return settings
    merged = {**settings.model_dump(), **overrides}
    try:
        return type(settings).model_validate(merged)
    except ValidationError as e:
        raise ConfigError(f"Invalid --host/--gui-port/--mcp-port override:\n{e}") from e


def _create_mcp_http_app(ctx):
    """Build the exact MCP ASGI app served by the CLI."""
    mcp_app = create_mcp_server(ctx).streamable_http_app()
    mcp_app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=ctx.settings.request_max_bytes
    )

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
    return mcp_app


def _serve(args) -> int:
    settings = _apply_serve_overrides(get_settings(), args)
    try:
        validate_restore_layout(Path(settings.db_path), Path(settings.vault_path))
        with ProjectLock(settings.db_path) as process_lock:
            recovery = _recover_pending_restore(
                Path(settings.db_path), Path(settings.vault_path), process_lock
            )
            if recovery:
                print(f"Recovered interrupted restore ({recovery}).")
            return _serve_locked(args, settings)
    except (ProjectLockError, OSError, ValueError) as exc:
        print(f"serve failed: {exc}")
        return 1


def _serve_locked(args, settings) -> int:

    # Logging is already configured for all commands in _dispatch().
    print("Loading embedding model… (first run downloads it from HuggingFace)")
    ctx = build_context(settings, full=True, start_embed_worker=True)
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
    # Start the background embedder only after the boot sweep, so writes during startup
    # don't notify a thread that isn't running yet. From here, create/update flag
    # vector_dirty and hand the slow forward pass to this worker, off the request path.
    if ctx.embed_worker is not None:
        ctx.embed_worker.start()

    web_app = create_web_app(ctx)
    mcp_app = _create_mcp_http_app(ctx)

    print(f"Web UI : http://{settings.host}:{settings.gui_port}")
    print(f"MCP    : http://{settings.host}:{settings.mcp_port}/mcp  (Authorization: Bearer <api_key>)")
    try:
        asyncio.run(_serve_both(settings, web_app, mcp_app))
    finally:
        # Stop the embedder thread; anything still vector_dirty is embedded by the next
        # startup sweep, so a bounded join can't lose vectors. Give it the operator's
        # configured shutdown grace (not a hardcoded 10s) so a large in-flight sweep on
        # a big vault has the same room as the rest of shutdown.
        if ctx.embed_worker is not None:
            ctx.embed_worker.stop(timeout=settings.shutdown_grace_s)
    return 0


async def _serve_both(settings, web_app, mcp_app) -> None:
    # Bound the graceful-shutdown wait so an in-flight request (e.g. a multi-second
    # embed) can't keep the process alive past an orchestrator's kill grace, which
    # would escalate to SIGKILL and risk a torn WAL. A second signal forces an
    # immediate exit.
    grace = settings.shutdown_grace_s
    # Honor X-Forwarded-For/-Proto only from the configured proxy addresses, so the
    # app sees the real client IP (rate-limit keys + audit) and correct scheme behind
    # a reverse proxy. forwarded_allow_ips defaults to the same-host proxy ("127.0.0.1").
    fwd = settings.forwarded_allow_ips
    web_cfg = uvicorn.Config(web_app, host=settings.host, port=settings.gui_port,
                             log_level="info", timeout_graceful_shutdown=grace,
                             proxy_headers=True, forwarded_allow_ips=fwd)
    mcp_cfg = uvicorn.Config(mcp_app, host=settings.host, port=settings.mcp_port,
                             log_level="info", timeout_graceful_shutdown=grace,
                             proxy_headers=True, forwarded_allow_ips=fwd)
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
