"""CLI implementation: serve (web + MCP), init-db, create-admin, create-user,
create-api-key, reindex."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import signal
from pathlib import Path

import uvicorn

from .config import get_settings
from .mcp_server import create_mcp_server
from .runtime import build_context
from .services import users as users_svc
from .services.auth import create_api_key, create_user
from .services.errors import WikiError
from .web import create_web_app


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

    b = sub.add_parser("backup", help="Write a consistent (WAL-safe) copy of the database.")
    b.add_argument("--out", required=True, help="Destination .db path for the snapshot.")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
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
    if args.cmd == "backup":
        return _backup(args)
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
    return 0


def _serve(args) -> int:
    settings = get_settings()
    if args.host:
        settings.host = args.host
    if args.gui_port:
        settings.gui_port = args.gui_port
    if args.mcp_port:
        settings.mcp_port = args.mcp_port

    print("Loading embedding model… (first run downloads it from HuggingFace)")
    ctx = build_context(settings, full=True)
    if not args.no_recover:
        n = ctx.docs.recover_pending()
        if n:
            print(f"Recovered {n} pending file(s) from the last run.")

    web_app = create_web_app(ctx)
    mcp_app = create_mcp_server(ctx).streamable_http_app()

    print(f"Web UI : http://{settings.host}:{settings.gui_port}")
    print(f"MCP    : http://{settings.host}:{settings.mcp_port}/mcp  (Authorization: Bearer <api_key>)")
    asyncio.run(_serve_both(settings, web_app, mcp_app))
    return 0


async def _serve_both(settings, web_app, mcp_app) -> None:
    web_cfg = uvicorn.Config(web_app, host=settings.host, port=settings.gui_port, log_level="info")
    mcp_cfg = uvicorn.Config(mcp_app, host=settings.host, port=settings.mcp_port, log_level="info")
    servers = [uvicorn.Server(web_cfg), uvicorn.Server(mcp_cfg)]
    for s in servers:
        s.install_signal_handlers = lambda: None  # type: ignore[attr-defined]  # we manage signals for both at once

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    await asyncio.gather(*(s.serve() for s in servers))
