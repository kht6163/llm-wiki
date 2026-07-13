"""Observable CLI boundary coverage for parser, orchestration, and shutdown paths."""
from __future__ import annotations

import asyncio
import importlib.metadata
import runpy
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from starlette.applications import Starlette

from llm_wiki import _cli_impl
from llm_wiki import snapshot as snapshot_writer
from llm_wiki.config import ConfigError, Settings
from llm_wiki.process_lock import ProjectLock, ProjectLockError
from llm_wiki.services.errors import WikiError


def _args(**overrides):
    values = {
        "host": None,
        "gui_port": None,
        "mcp_port": None,
        "no_recover": False,
    }
    values.update(overrides)
    return NS(**values)


def _reindex_report(**overrides):
    report = {
        "created": 0,
        "updated": 0,
        "renamed": 0,
        "unchanged": 1,
        "embedded": 0,
        "missing_files": [],
    }
    report.update(overrides)
    return report


def _import_report(**overrides):
    report = {
        "plan": [],
        "attachments": {"copied": 0, "skipped": 0},
        "created": 0,
        "revived": 0,
        "skipped": 0,
        "renamed": 0,
        "overwritten": 0,
        "scanned": 0,
        "embedded": 0,
        "warnings": [],
        "errors": [],
        "broken_links": [],
    }
    report.update(overrides)
    return report


def test_parser_accepts_every_command_and_preserves_options():
    parser = _cli_impl._build_parser()
    cases = [
        (["serve", "--host", "0.0.0.0", "--gui-port", "9000", "--mcp-port", "9001", "--no-recover"], "serve"),
        (["init-db"], "init-db"),
        (["create-admin", "--username", "root", "--password", "secret", "--force"], "create-admin"),
        (["create-user", "--username", "ed", "--password", "secret", "--role", "viewer"], "create-user"),
        (["create-api-key", "--username", "ed", "--name", "robot"], "create-api-key"),
        (["reindex", "--reembed"], "reindex"),
        (["import", "--from", "/src", "--into", "notes", "--on-conflict", "rename", "--include", "*.md", "--no-recurse", "--import-attachments", "--no-embed", "--dry-run", "--force"], "import"),
        (["backup", "--out", "copy.db"], "backup"),
        (["snapshot", "--out", "wiki.tar", "--force"], "snapshot"),
        (["restore", "--in", "wiki.tar", "--force"], "restore"),
        (["prune", "--keep", "3", "--older-than-days", "0", "--no-vacuum", "--force"], "prune"),
        (["db-check", "--quick", "--fix-orphan-vectors"], "db-check"),
    ]
    parsed = [parser.parse_args(argv) for argv, _ in cases]
    assert [item.cmd for item in parsed] == [command for _, command in cases]
    assert vars(parsed[0]) == {
        "cmd": "serve", "host": "0.0.0.0", "gui_port": 9000,
        "mcp_port": 9001, "no_recover": True,
    }
    assert parsed[6].include == ["*.md"] and parsed[6].no_embed is True
    assert parsed[10].older_than_days == 0 and parsed[10].no_vacuum is True


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: cmd"),
        (["create-api-key"], "the following arguments are required: --username"),
        (["import", "--from", "/src"], "the following arguments are required: --into"),
        (["create-user", "--role", "owner"], "invalid choice: 'owner'"),
        (["serve", "--gui-port", "not-a-port"], "invalid int value: 'not-a-port'"),
    ],
)
def test_parser_errors_exit_two_and_write_usage_to_stderr(capsys, argv, message):
    with pytest.raises(SystemExit) as exc_info:
        _cli_impl._build_parser().parse_args(argv)
    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert captured.out == ""
    assert captured.err.startswith("usage: llm-wiki")
    assert message in captured.err


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("serve", "_serve"), ("init-db", "_init_db"),
        ("create-admin", "_create_admin"), ("create-user", "_create_user"),
        ("create-api-key", "_create_api_key"), ("reindex", "_reindex"),
        ("import", "_import"), ("backup", "_backup"),
        ("snapshot", "_snapshot"), ("restore", "_restore"),
        ("prune", "_prune"), ("db-check", "_db_check"),
    ],
)
def test_dispatch_routes_command_and_configures_logging(monkeypatch, command, handler):
    settings = NS(log_level="DEBUG", log_file="wiki.log")
    configured = []
    monkeypatch.setattr("llm_wiki.config.get_settings", lambda: settings)
    monkeypatch.setattr("llm_wiki.logconf.configure_logging", lambda *args: configured.append(args))
    monkeypatch.setattr(_cli_impl, handler, lambda *args: 17)
    assert _cli_impl._dispatch(NS(cmd=command)) == 17
    assert configured == [("DEBUG", "wiki.log")]


def test_dispatch_tolerates_bad_logging_config_and_rejects_unknown(monkeypatch):
    monkeypatch.setattr("llm_wiki.config.get_settings", lambda: (_ for _ in ()).throw(ConfigError("bad env")))
    assert _cli_impl._dispatch(NS(cmd="unknown")) == 2


@pytest.mark.parametrize(
    ("error", "expected_rc", "expected_output"),
    [
        (ConfigError("bad settings"), 2, "configuration error: bad settings"),
        (WikiError("bad request"), 1, "error: bad request"),
        (KeyboardInterrupt(), 0, ""),
    ],
)
def test_run_translates_domain_errors_to_cli_envelopes(monkeypatch, capsys, error, expected_rc, expected_output):
    monkeypatch.setattr(_cli_impl, "_dispatch", lambda _args: (_ for _ in ()).throw(error))
    assert _cli_impl.run(["init-db"]) == expected_rc
    assert capsys.readouterr().out.strip() == expected_output


def test_run_returns_dispatch_result(monkeypatch):
    monkeypatch.setattr(_cli_impl, "_dispatch", lambda args: 9 if args.cmd == "init-db" else 8)
    assert _cli_impl.run(["init-db"]) == 9


def test_init_db_reports_bound_paths_and_model(monkeypatch, capsys):
    ctx = NS(settings=NS(db_path="wiki.db", vault_path="vault", embedding_model="e5"), embedder=NS(dim=768))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    assert _cli_impl._init_db() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Initialized database at wiki.db", "Vault: vault", "Embedding model: e5 (dim=768)"
    ]


def test_init_and_serve_explain_embedding_revision_rebuild(monkeypatch, capsys):
    error = RuntimeError(
        "Database embedding binding predates model revision tracking; "
        "run `llm-wiki reindex --reembed`."
    )
    monkeypatch.setattr(
        _cli_impl, "build_context", lambda *args, **kwargs: (_ for _ in ()).throw(error)
    )

    assert _cli_impl._init_db() == 1
    assert _cli_impl._serve_locked(_args(), _settings()) == 1
    output = capsys.readouterr().out
    assert output.count("embedding index requires a rebuild") == 2
    assert output.count("uv run llm-wiki reindex --reembed") == 2

    unrelated = RuntimeError("unrelated startup failure")
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(unrelated),
    )
    with pytest.raises(RuntimeError, match="unrelated startup failure"):
        _cli_impl._init_db()
    with pytest.raises(RuntimeError, match="unrelated startup failure"):
        _cli_impl._serve_locked(_args(), _settings())

    monkeypatch.setattr(_cli_impl, "_dispatch", lambda _args: (_ for _ in ()).throw(error))
    assert _cli_impl.run(["init-db"]) == 1
    monkeypatch.setattr(
        _cli_impl,
        "_dispatch",
        lambda _args: (_ for _ in ()).throw(unrelated),
    )
    with pytest.raises(RuntimeError, match="unrelated startup failure"):
        _cli_impl.run(["init-db"])


def test_os_actor_has_fallback(monkeypatch):
    monkeypatch.setattr(_cli_impl.getpass, "getuser", lambda: "operator")
    assert _cli_impl._os_actor() == "cli:operator"
    monkeypatch.setattr(_cli_impl.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("no login")))
    assert _cli_impl._os_actor() == "-"


def test_create_admin_rejects_duplicate_and_force_uses_interactive_credentials(monkeypatch, capsys):
    ctx = NS(db=object())
    created = []
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    monkeypatch.setattr(_cli_impl.users_svc, "count_admins", lambda _db: 1)
    assert _cli_impl._create_admin(NS(username="new", password="pw", force=False)) == 1
    assert "already exists" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "  root  ")
    monkeypatch.setattr(_cli_impl.getpass, "getpass", lambda prompt: "secret")
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:operator")
    monkeypatch.setattr(_cli_impl, "create_user", lambda *args, **kwargs: created.append((args, kwargs)) or 42)
    assert _cli_impl._create_admin(NS(username=None, password=None, force=True)) == 0
    assert created == [((ctx.db, "root", "secret", "admin"), {"audit_actor": "cli:operator", "audit_via": "cli"})]
    assert "Created admin 'root' (id=42)." in capsys.readouterr().out


def test_create_user_interactive_and_explicit_paths(monkeypatch, capsys):
    ctx = NS(db=object())
    calls = []
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    monkeypatch.setattr("builtins.input", lambda prompt: " viewer ")
    monkeypatch.setattr(_cli_impl.getpass, "getpass", lambda prompt: "secret")
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:operator")
    monkeypatch.setattr(_cli_impl, "create_user", lambda *args, **kwargs: calls.append((args, kwargs)) or 7)
    assert _cli_impl._create_user(NS(username=None, password=None, role="viewer")) == 0
    assert _cli_impl._create_user(NS(username="ed", password="pw", role="editor")) == 0
    assert [call[0][1:4] for call in calls] == [("viewer", "secret", "viewer"), ("ed", "pw", "editor")]
    assert "Created user 'ed' (id=7, role=editor)." in capsys.readouterr().out


class _ReaderDB:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    @contextmanager
    def reader(self):
        db = self
        class Conn:
            def execute(self, sql, params=()):
                db.executed.append((sql, params))
                return NS(fetchone=lambda: db.row)
        yield Conn()


def test_create_api_key_reports_missing_user_and_mints_for_active_user(monkeypatch, capsys):
    db = _ReaderDB()
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=db))
    assert _cli_impl._create_api_key(NS(username="ghost", name="robot")) == 1
    assert "No such active user: ghost" in capsys.readouterr().out
    db.row = {"id": 4, "username": "ed", "role": "editor", "credential_version": 3}
    received = {}
    def mint(*args, **kwargs):
        received.update(db=args[0], principal=args[1], name=args[2], **kwargs)
        return "lw_secret"
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:operator")
    monkeypatch.setattr(_cli_impl, "create_api_key", mint)
    assert _cli_impl._create_api_key(NS(username="ed", name="robot")) == 0
    assert received["principal"].username == "ed" and received["principal"].credential_version == 3
    assert received["audit_detail"] == "user=ed" and received["name"] == "robot"
    assert capsys.readouterr().out.splitlines() == ["lw_secret", "(store this now — it is not shown again)"]


class _Docs:
    def __init__(self, report, progress_points=()):
        self.report = report
        self.progress_points = progress_points
        self.reindex_args = None

    def reindex_all(self, **kwargs):
        self.reindex_args = kwargs
        for point in self.progress_points:
            kwargs["progress"](*point)
        return self.report


def test_reindex_normal_path_reports_progress_and_clean_result(monkeypatch, capsys):
    docs = _Docs(_reindex_report(), progress_points=[(0, 0), (1, 2), (2, 2)])
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(docs=docs))
    assert _cli_impl._reindex(NS(reembed=False)) == 0
    out = capsys.readouterr().out
    assert "Reindexing vault…" in out and "embedding 0/0 chunks (100%)" in out
    assert "embedding 1/2 chunks (50%)" in out and "conflicts=0" in out
    assert docs.reindex_args["reembed"] is False


def test_reindex_rebinds_and_reports_every_nonconverged_item(monkeypatch, capsys):
    report = _reindex_report(
        renamed=1, renames=["old.md -> new.md"], skipped_deleted=["dead.md"],
        missing_files=["gone.md"], skipped_conflicts=[{"path": "race.md", "reason": "changed", "attempts": 3}],
        recovered_pending=2, retried=1,
    )
    docs = _Docs(report)
    db = _ReaderDB()
    db.rebound = []
    db.rebind_model = lambda *args: db.rebound.append(args)
    embedder = NS(dim=3, pipeline="pipe")
    ctx = NS(db=db, docs=docs, embedder=embedder, settings=NS(embedding_model="new-model"))
    values = iter(["old-model", "2"])
    monkeypatch.setattr(_cli_impl, "get_meta", lambda conn, key: next(values))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    assert _cli_impl._reindex(NS(reembed=True)) == 1
    out = capsys.readouterr().out
    assert "Rebinding embedding model: old-model (dim 2) -> new-model (dim 3)" in out
    assert "renamed: old.md -> new.md" in out and "tombstoned" in out and "gone.md" in out
    assert "race.md: changed (attempts=3)" in out
    assert db.rebound == [("new-model", 3, "pipe", "")]


def test_reindex_same_or_unbound_model_does_not_claim_rebinding(monkeypatch, capsys):
    docs = _Docs(_reindex_report())
    db = _ReaderDB()
    db.rebind_model = lambda *args: None
    ctx = NS(db=db, docs=docs, embedder=NS(dim=3, pipeline="pipe"), settings=NS(embedding_model="same"))
    values = iter([None, None])
    monkeypatch.setattr(_cli_impl, "get_meta", lambda conn, key: next(values))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    assert _cli_impl._reindex(NS(reembed=True)) == 0
    assert "Rebinding embedding model" not in capsys.readouterr().out


def _import_args(**overrides):
    values = dict(from_=None, into="", on_conflict="skip", include=None,
                  no_recurse=False, import_attachments=False, no_embed=False,
                  dry_run=False, force=False)
    values.update(overrides)
    return NS(**values)


def test_import_validates_source_overwrite_and_each_vault_overlap(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "missing"
    assert _cli_impl._import(_import_args(from_=str(missing))) == 2
    src = tmp_path / "src"
    src.mkdir()
    assert _cli_impl._import(_import_args(from_=str(src), on_conflict="overwrite")) == 1
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(settings=NS(vault_path=vault)))
    assert _cli_impl._import(_import_args(from_=str(vault))) == 2
    child = vault / "child"
    child.mkdir()
    assert _cli_impl._import(_import_args(from_=str(child))) == 2
    outer = tmp_path / "outer"
    outer.mkdir()
    nested_vault = outer / "vault"
    nested_vault.mkdir()
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(settings=NS(vault_path=nested_vault)))
    assert _cli_impl._import(_import_args(from_=str(outer))) == 2
    assert capsys.readouterr().out.count("configuration error") == 4


def test_import_forwards_operator_options_and_uses_errors_for_exit(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    calls = []
    reports = iter([_import_report(errors=[{"path": "bad.md", "error": "bad"}]), _import_report()])
    class Docs:
        def import_from_directory(self, principal, source, **kwargs):
            calls.append((principal, source, kwargs))
            return next(reports)
    ctx = NS(settings=NS(vault_path=tmp_path / "vault"), docs=Docs())
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:operator")
    monkeypatch.setattr(_cli_impl, "_print_import_report", lambda *args: None)
    rich = _import_args(from_=str(src), into="notes", include=["*.md", "docs/**"],
                        no_recurse=True, import_attachments=True, no_embed=True,
                        dry_run=True, on_conflict="overwrite")
    assert _cli_impl._import(rich) == 1
    assert _cli_impl._import(_import_args(from_=str(src))) == 0
    principal, source, kwargs = calls[0]
    assert principal.user_id is None and principal.username == "cli:operator" and principal.via == "cli"
    assert source == src.resolve()
    assert kwargs == {"into": "notes", "on_conflict": "overwrite", "recurse": False,
                      "import_attachments": True, "embed": False, "dry_run": True,
                      "include": ("*.md", "docs/**")}
    assert "include" not in calls[1][2]


@pytest.mark.parametrize("dry_run", [True, False])
def test_import_report_renders_actions_summaries_and_diagnostics(capsys, tmp_path, dry_run):
    report = _import_report(
        plan=[
            {"action": "rename", "src": "old.md", "target": "new.md"},
            {"action": "skip", "src": "same.md", "target": "same.md", "reason": "exists"},
            {"action": "custom", "src": "x.md", "target": "x.md"},
            {"action": "attach", "src": "pic.png", "target": "_attachments/pic.png"},
        ],
        attachments={"copied": 1, "skipped": 2}, created=1, revived=2,
        skipped=3, renamed=4, overwritten=5, scanned=6, embedded=7,
        warnings=["encoding replaced"], errors=[{"path": "bad.md", "error": "unreadable"}],
        broken_links=["missing"],
    )
    _cli_impl._print_import_report(report, tmp_path, "/notes/", dry_run, True)
    out = capsys.readouterr().out
    assert ("Importing" if dry_run else "Imported") in out
    assert "RENAME    old.md -> new.md" in out and "SKIP      same.md (exists)" in out
    assert "CUSTOM    x.md" in out and "ATTACH    pic.png -> _attachments/pic.png" in out
    assert "WARNING: encoding replaced" in out and "ERROR: bad.md: unreadable" in out
    assert ("Would create 1" if dry_run else "created=1") in out
    assert ("unresolved" in out) is (not dry_run)


def test_import_report_root_without_attachments(capsys):
    _cli_impl._print_import_report(_import_report(), Path("src"), "///", False, False)
    out = capsys.readouterr().out
    assert "vault/(root)" in out and "attachments" not in out


class _PruneDB:
    def __init__(self):
        self.sql = []
    @contextmanager
    def reader(self):
        yield NS(execute=lambda sql: self.sql.append(sql))


def test_prune_dry_run_skips_audit_and_vacuum(monkeypatch, capsys):
    db = _PruneDB()
    revision_calls = []
    docs = NS(prune_revisions=lambda **kwargs: revision_calls.append(kwargs) or {"deletable_revisions": 4, "keep": 2})
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=db, docs=docs))
    monkeypatch.setattr(_cli_impl.audit, "prune", lambda *args, **kwargs: pytest.fail("audit pruning should be disabled"))
    assert _cli_impl._prune(NS(force=False, keep=2, older_than_days=0, no_vacuum=False)) == 0
    assert revision_calls == [{"keep": 2, "apply": False}]
    assert "Dry run" in capsys.readouterr().out and db.sql == []


def test_prune_applies_audit_and_optional_vacuum(monkeypatch, capsys):
    db = _PruneDB()
    revision_calls = []
    docs = NS(prune_revisions=lambda **kwargs: revision_calls.append(kwargs) or {"deletable_revisions": 4, "keep": 2})
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=db, docs=docs))
    audit_calls = []
    monkeypatch.setattr(_cli_impl.audit, "prune", lambda db, **kwargs: audit_calls.append(kwargs) or {"deletable_events": 3, "cutoff": "2026-01-01"})
    assert _cli_impl._prune(NS(force=True, keep=2, older_than_days=30, no_vacuum=False)) == 0
    assert revision_calls == [{"keep": 2, "apply": True}]
    assert audit_calls == [{"older_than_days": 30, "apply": True}] and db.sql == ["VACUUM"]
    assert "audit_log: 3" in capsys.readouterr().out
    db.sql.clear()
    assert _cli_impl._prune(NS(force=True, keep=2, older_than_days=30, no_vacuum=True)) == 0
    assert revision_calls == [{"keep": 2, "apply": True}, {"keep": 2, "apply": True}]
    assert audit_calls == [
        {"older_than_days": 30, "apply": True},
        {"older_than_days": 30, "apply": True},
    ]
    assert db.sql == []


@pytest.mark.parametrize(
    ("report", "quick", "fragments"),
    [
        ({"ok": True, "check": "quick_check"}, True, ["integrity (quick_check): ok", "foreign keys: ok", "orphan vectors: ok"]),
        ({"ok": False, "check": "integrity_check", "integrity": ["ok", "page corrupt"],
          "foreign_key_violations": [{"table": "chunks", "rowid": 4, "parent": "documents", "fkid": 0}],
          "orphan_vectors": 2}, False, ["integrity (integrity_check): FAILED", "page corrupt", "table=chunks rowid=4", "orphan vectors: 2"]),
        ({"ok": False, "check": "integrity_check", "integrity": ["ok"],
          "foreign_key_violations": [], "orphan_vectors": 0},
         False, ["integrity (integrity_check): ok", "foreign keys: ok", "orphan vectors: ok"]),
    ],
)
def test_db_check_reports_each_integrity_dimension(monkeypatch, capsys, report, quick, fragments):
    integrity_calls = []
    db = NS(
        integrity_check=lambda **kwargs: integrity_calls.append(kwargs) or report,
        delete_orphan_vectors=lambda: 5,
    )
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=db))
    fix = report["ok"]
    assert _cli_impl._db_check(NS(quick=quick, fix_orphan_vectors=fix)) == (0 if report["ok"] else 1)
    assert integrity_calls == [{"quick": quick}]
    out = capsys.readouterr().out
    assert all(fragment in out for fragment in fragments)
    assert ("orphan vectors: removed 5" in out) is fix


def test_backup_refuses_existing_and_cleans_partial_on_sqlite_error(monkeypatch, tmp_path, capsys):
    out = tmp_path / "nested" / "copy.db"
    out.parent.mkdir()
    out.write_text("existing")
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=object()))
    assert _cli_impl._backup(NS(out=str(out))) == 1
    out.unlink()
    class BrokenDB:
        @contextmanager
        def reader(self):
            out.write_text("partial")
            yield NS(execute=lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: NS(db=BrokenDB()))
    assert _cli_impl._backup(NS(out=str(out))) == 1
    assert not out.exists() and "backup failed: disk full" in capsys.readouterr().out


def test_backup_executes_wal_safe_copy_and_reports_vault(monkeypatch, tmp_path, capsys):
    db = _ReaderDB()
    ctx = NS(db=db, settings=NS(vault_path="vault"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    out = tmp_path / "nested" / "copy.db"
    assert _cli_impl._backup(NS(out=str(out))) == 0
    assert db.executed == [("VACUUM INTO ?", (str(out),))]
    text = capsys.readouterr().out
    assert f"Database backed up to {out}" in text and "vault directory" in text


def test_snapshot_refuses_overwrite_then_forwards_force(monkeypatch, tmp_path, capsys):
    out = tmp_path / "wiki.tar"
    out.write_text("old")
    ctx = NS(db="db", settings=NS(vault_path=tmp_path / "vault"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    assert _cli_impl._snapshot(NS(out=str(out), force=False)) == 1
    calls = []
    report = NS(schema_version=9, doc_count=4, file_count=6, embedding_model="e5")
    monkeypatch.setattr(_cli_impl, "write_snapshot", lambda *args, **kwargs: calls.append((args, kwargs)) or report)
    assert _cli_impl._snapshot(NS(out=str(out), force=True)) == 0
    assert calls == [(('db', tmp_path / "vault", out), {"force": True})]
    assert "schema v9 · 4 docs · 6 vault file(s) · model e5" in capsys.readouterr().out


@pytest.mark.parametrize("error", [ValueError("invalid manifest"), RuntimeError("vault changed")])
def test_snapshot_reports_validation_failures_without_traceback(
    monkeypatch, tmp_path, capsys, error
):
    out = tmp_path / "wiki.tar"
    ctx = NS(db="db", settings=NS(vault_path=tmp_path / "vault"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kwargs: ctx)
    monkeypatch.setattr(
        _cli_impl,
        "write_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    assert _cli_impl._snapshot(NS(out=str(out), force=False)) == 1
    assert capsys.readouterr().out.strip() == f"snapshot failed: {error}"


def test_restore_handles_missing_busy_and_invalid_snapshot(monkeypatch, tmp_path, capsys):
    settings = NS(db_path=tmp_path / "wiki.db", vault_path=tmp_path / "vault", embedding_model="e5")
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    missing = tmp_path / "missing.tar"
    assert _cli_impl._restore(NS(in_=str(missing), force=False)) == 1
    src = tmp_path / "wiki.tar"
    src.touch()
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(FileExistsError()))
    assert _cli_impl._restore(NS(in_=str(src), force=False)) == 1
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad manifest")))
    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 1
    out = capsys.readouterr().out
    assert "no such snapshot" in out and "pass --force" in out and "restore failed: bad manifest" in out


def test_restore_warns_recovers_and_reports_success(monkeypatch, tmp_path, capsys):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(db_path=tmp_path / "wiki.db", vault_path=tmp_path / "vault", embedding_model="configured")
    report = NS(backup_cleanup_warnings=[tmp_path / "saved.db"], embedding_model="snapshot-model",
                schema_version=8, doc_count=12, finalize=lambda: None)
    calls = []
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: calls.append((args, kwargs)) or report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: NS(
            db=NS(close=lambda: None), docs=NS(recover_pending=lambda: 3)
        ),
    )
    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 0
    assert calls == [((src, Path(settings.db_path), Path(settings.vault_path)), {"force": True})]
    out = capsys.readouterr().out
    assert "backup cleanup failed" in out and "reindex --reembed" in out
    assert "schema v8 · 12 docs" in out and "re-projected 3 pending" in out


def test_restore_clean_success_has_no_optional_warnings(monkeypatch, tmp_path, capsys):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(db_path=tmp_path / "wiki.db", vault_path=tmp_path / "vault", embedding_model="same")
    report = NS(backup_cleanup_warnings=[], embedding_model="same", schema_version=8,
                doc_count=0, finalize=lambda: None)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: NS(
            db=NS(close=lambda: None), docs=NS(recover_pending=lambda: 0)
        ),
    )
    assert _cli_impl._restore(NS(in_=str(src), force=False)) == 0
    out = capsys.readouterr().out
    assert "WARNING" not in out and "re-projected" not in out


def test_serve_holds_the_same_project_lock_used_by_restore(monkeypatch, tmp_path):
    settings = NS(
        db_path=tmp_path / "data" / "wiki.db", vault_path=tmp_path / "vault"
    )
    observed = []

    def serve_locked(args, locked_settings):
        assert locked_settings is settings
        with pytest.raises(ProjectLockError, match="already active"):
            ProjectLock(settings.db_path).acquire()
        observed.append(True)
        return 0

    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "_apply_serve_overrides", lambda value, args: value)
    monkeypatch.setattr(_cli_impl, "_serve_locked", serve_locked)

    assert _cli_impl._serve(NS()) == 0
    assert observed == [True]


def test_serve_rejects_database_inside_vault_before_lock_creation(
    monkeypatch, tmp_path, capsys
):
    vault = tmp_path / "vault"
    settings = NS(db_path=vault / "data" / "wiki.db", vault_path=vault)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "_apply_serve_overrides", lambda value, args: value)

    assert _cli_impl._serve(NS()) == 1
    assert "overlap" in capsys.readouterr().out
    assert not (settings.db_path.parent / ".llm-wiki.lock").exists()


def test_serve_reports_startup_recovery_io_failure(monkeypatch, tmp_path, capsys):
    settings = NS(
        db_path=tmp_path / "data" / "wiki.db", vault_path=tmp_path / "vault"
    )
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "_apply_serve_overrides", lambda value, args: value)
    monkeypatch.setattr(
        _cli_impl,
        "_recover_pending_restore",
        lambda *args: (_ for _ in ()).throw(OSError("journal fsync failed")),
    )

    assert _cli_impl._serve(NS()) == 1
    assert "serve failed: journal fsync failed" in capsys.readouterr().out


def test_restore_report_finalize_keeps_lock_release_internal():
    observed = []
    journal = NS(finalize=lambda **kwargs: observed.append(kwargs) or ())
    report = snapshot_writer.RestoreReport(1, 0, _journal=journal)

    with pytest.raises(TypeError):
        report.finalize(release_lock=False)
    assert observed == []
    assert report.finalize() == ()
    assert observed == [{}]


def test_restore_reports_durable_finalize_failure_without_traceback(
    monkeypatch, tmp_path, capsys
):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(
        db_path=tmp_path / "wiki.db",
        vault_path=tmp_path / "vault",
        embedding_model="same",
    )
    report = NS(
        embedding_model="same",
        finalize=lambda: (_ for _ in ()).throw(OSError("finalize fsync failed")),
    )
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: NS(
            db=NS(close=lambda: None), docs=NS(recover_pending=lambda: 0)
        ),
    )

    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 1
    output = capsys.readouterr().out
    assert "restore finalization failed: finalize fsync failed" in output
    assert "restart serve or rerun restore" in output


def test_restore_and_serve_report_project_lock_contention(monkeypatch, tmp_path, capsys):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(db_path=tmp_path / "data" / "wiki.db", vault_path=tmp_path / "vault")
    error = ProjectLockError("already active")
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 1

    with ProjectLock(settings.db_path):
        assert _cli_impl._serve(NS(host=None, gui_port=None, mcp_port=None)) == 1

    assert capsys.readouterr().out.count("already active") == 2


def test_restore_defensively_rejects_a_rollback_that_returns(monkeypatch, tmp_path):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(
        db_path=tmp_path / "wiki.db",
        vault_path=tmp_path / "vault",
        embedding_model="same",
    )
    report = NS(embedding_model="same", rollback=lambda exc: None)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        _cli_impl, "build_context", lambda **kwargs: (_ for _ in ()).throw(OSError("fail"))
    )

    with pytest.raises(AssertionError, match="unexpectedly returned"):
        _cli_impl._restore(NS(in_=str(src), force=True))


def test_restore_rolls_back_when_postcheck_database_close_fails(
    monkeypatch, tmp_path, capsys
):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(
        db_path=tmp_path / "wiki.db",
        vault_path=tmp_path / "vault",
        embedding_model="same",
    )
    observed = []
    report = NS(
        embedding_model="same",
        rollback=lambda error: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: NS(
            docs=NS(recover_pending=lambda: 0),
            db=NS(
                close=lambda: (
                    observed.append("close"),
                    (_ for _ in ()).throw(OSError("close failed")),
                )[-1]
            ),
        ),
    )

    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 1
    assert observed == ["close"]
    assert "close failed" in capsys.readouterr().out


def test_restore_preserves_postcheck_error_when_database_close_also_fails(
    monkeypatch, tmp_path, capsys
):
    src = tmp_path / "wiki.tar"
    src.touch()
    settings = NS(
        db_path=tmp_path / "wiki.db",
        vault_path=tmp_path / "vault",
        embedding_model="same",
    )
    report = NS(
        embedding_model="same",
        rollback=lambda error: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "restore_snapshot", lambda *args, **kwargs: report)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: NS(
            docs=NS(
                recover_pending=lambda: (_ for _ in ()).throw(
                    RuntimeError("recovery failed")
                )
            ),
            db=NS(
                close=lambda: (_ for _ in ()).throw(OSError("secondary close failed"))
            ),
        ),
    )

    assert _cli_impl._restore(NS(in_=str(src), force=True)) == 1
    output = capsys.readouterr().out
    assert "recovery failed" in output
    assert "secondary close failed" not in output


def _settings(**overrides):
    values = dict(host="127.0.0.1", gui_port=8080, mcp_port=8081,
                  request_max_bytes=1024 * 1024, shutdown_grace_s=5,
                  forwarded_allow_ips="127.0.0.1")
    values.update(overrides)
    return Settings(**values)


def test_serve_overrides_revalidate_all_fields_and_preserve_original():
    settings = _settings()
    assert _cli_impl._apply_serve_overrides(settings, _args()) is settings
    merged = _cli_impl._apply_serve_overrides(settings, _args(host="0.0.0.0", gui_port=9000, mcp_port=9001))
    assert (merged.host, merged.gui_port, merged.mcp_port) == ("0.0.0.0", 9000, 9001)
    assert (settings.host, settings.gui_port, settings.mcp_port) == ("127.0.0.1", 8080, 8081)
    with pytest.raises(ConfigError, match="Invalid --host/--gui-port/--mcp-port override"):
        _cli_impl._apply_serve_overrides(settings, _args(gui_port=8081))


@pytest.mark.parametrize(("option", "field"), [("--gui-port", "gui_port"), ("--mcp-port", "mcp_port")])
def test_explicit_zero_port_override_exits_with_configuration_error(monkeypatch, capsys, option, field):
    settings = _settings()
    monkeypatch.setattr("llm_wiki.config.get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda *args, **kwargs: pytest.fail("invalid port must fail before context creation"),
    )
    assert _cli_impl.run(["serve", option, "0"]) == 2
    output = capsys.readouterr().out
    assert "configuration error: Invalid --host/--gui-port/--mcp-port override" in output
    assert f"{field} must be between 1 and 65535" in output


@pytest.mark.asyncio
async def test_mcp_http_app_exposes_body_limit_health_and_metrics(monkeypatch):
    app = Starlette()
    mcp = NS(streamable_http_app=lambda: app)
    ctx = NS(settings=NS(request_max_bytes=1234), embedder=NS(is_loaded=True))
    monkeypatch.setattr(_cli_impl, "create_mcp_server", lambda actual: mcp if actual is ctx else None)
    monkeypatch.setattr("llm_wiki.metrics.render_latest", lambda: (b"metric 1\n", "text/plain"))
    returned = _cli_impl._create_mcp_http_app(ctx)
    assert returned is app
    assert app.user_middleware[0].kwargs["max_bytes"] == 1234
    routes = {route.path: route for route in app.router.routes}
    health = await routes["/healthz"].endpoint(None)
    metrics = await routes["/metrics"].endpoint(None)
    assert health.status_code == 200 and health.body == b'{"ok":true,"model_loaded":true}'
    assert metrics.status_code == 200 and metrics.body == b"metric 1\n"
    assert metrics.media_type == "text/plain"


class _Worker:
    def __init__(self):
        self.events = []
    def start(self):
        self.events.append("start")
    def stop(self, *, timeout):
        self.events.append(("stop", timeout))


def test_serve_warms_recovers_runs_both_and_stops_worker(monkeypatch, capsys):
    settings = _settings()
    worker = _Worker()
    events = []
    docs = NS(recover_pending=lambda: 2, embed_pending=lambda: 3)
    embedder = NS(warm=lambda: events.append("warm"))
    ctx = NS(docs=docs, embedder=embedder, embed_worker=worker)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "build_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(_cli_impl, "create_web_app", lambda actual: "web")
    monkeypatch.setattr(_cli_impl, "_create_mcp_http_app", lambda actual: "mcp")
    async def serve_both(actual, web, mcp):
        events.append((actual, web, mcp))
    monkeypatch.setattr(_cli_impl, "_serve_both", serve_both)
    assert _cli_impl._serve(_args()) == 0
    assert events == ["warm", (settings, "web", "mcp")]
    assert worker.events == ["start", ("stop", settings.shutdown_grace_s)]
    out = capsys.readouterr().out
    assert "Recovered 2 pending" in out and "Re-embedded 3 document" in out
    assert f"http://{settings.host}:{settings.gui_port}" in out


def test_serve_stops_started_worker_after_server_error(monkeypatch):
    settings = _settings()
    worker = _Worker()
    docs = NS(recover_pending=lambda: pytest.fail("recovery disabled"), embed_pending=lambda: pytest.fail("recovery disabled"))
    ctx = NS(docs=docs, embedder=NS(warm=lambda: None), embed_worker=worker)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "build_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(_cli_impl, "create_web_app", lambda actual: "web")
    monkeypatch.setattr(_cli_impl, "_create_mcp_http_app", lambda actual: "mcp")
    async def fail(*args):
        raise RuntimeError("bind failed")
    monkeypatch.setattr(_cli_impl, "_serve_both", fail)
    with pytest.raises(RuntimeError, match="bind failed"):
        _cli_impl._serve(_args(no_recover=True))
    assert worker.events == ["start", ("stop", settings.shutdown_grace_s)]


def test_serve_normal_empty_recovery_without_worker_returns_cleanly(monkeypatch, capsys):
    settings = _settings()
    calls = []
    docs = NS(
        recover_pending=lambda: calls.append("recover") or 0,
        embed_pending=lambda: calls.append("embed") or 0,
    )
    ctx = NS(docs=docs, embedder=NS(warm=lambda: calls.append("warm")), embed_worker=None)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "build_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(_cli_impl, "create_web_app", lambda actual: "web")
    monkeypatch.setattr(_cli_impl, "_create_mcp_http_app", lambda actual: "mcp")
    async def serve_both(*args):
        calls.append("serve")
    monkeypatch.setattr(_cli_impl, "_serve_both", serve_both)
    assert _cli_impl._serve(_args()) == 0
    assert calls == ["warm", "recover", "embed", "serve"]
    out = capsys.readouterr().out
    assert "Recovered" not in out and "Re-embedded" not in out


class _Server:
    instances = []
    def __init__(self, config):
        self.config = config
        self.should_exit = False
        self.force_exit = False
        self.served = False
        self.install_signal_handlers = None
        self.instances.append(self)
    async def serve(self):
        self.served = True


class _Loop:
    def __init__(self, unsupported=False):
        self.unsupported = unsupported
        self.handlers = {}
    def add_signal_handler(self, sig, callback):
        if self.unsupported:
            raise NotImplementedError
        self.handlers[sig] = callback


@pytest.mark.asyncio
async def test_serve_both_builds_servers_and_first_then_second_signal(monkeypatch):
    _Server.instances.clear()
    loop = _Loop()
    configs = []
    monkeypatch.setattr(_cli_impl.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(_cli_impl.uvicorn, "Config", lambda app, **kwargs: configs.append((app, kwargs)) or NS(app=app, **kwargs))
    monkeypatch.setattr(_cli_impl.uvicorn, "Server", _Server)
    settings = _settings(host="0.0.0.0", gui_port=9000, mcp_port=9001, forwarded_allow_ips="10.0.0.1")
    await _cli_impl._serve_both(settings, "web", "mcp")
    assert [item[0] for item in configs] == ["web", "mcp"]
    assert all(item[1]["timeout_graceful_shutdown"] == 5 for item in configs)
    assert all(item[1]["proxy_headers"] is True and item[1]["forwarded_allow_ips"] == "10.0.0.1" for item in configs)
    assert all(server.served for server in _Server.instances)
    assert set(loop.handlers) == {_cli_impl.signal.SIGINT, _cli_impl.signal.SIGTERM}
    assert loop.handlers[_cli_impl.signal.SIGINT] is loop.handlers[_cli_impl.signal.SIGTERM]
    shutdown = loop.handlers[next(iter(loop.handlers))]
    shutdown()
    assert all(server.should_exit and not server.force_exit for server in _Server.instances)
    shutdown()
    assert all(server.force_exit for server in _Server.instances)
    assert all(server.install_signal_handlers() is None for server in _Server.instances)


class _BlockingServer(_Server):
    ready = None
    started = 0

    async def serve(self):
        self.served = True
        type(self).started += 1
        if type(self).started == 2:
            type(self).ready.set()
        while not self.should_exit:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_signals_change_both_servers_while_they_are_serving(monkeypatch):
    _Server.instances.clear()
    _BlockingServer.started = 0
    _BlockingServer.ready = asyncio.Event()
    loop = _Loop()
    monkeypatch.setattr(_cli_impl.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(_cli_impl.uvicorn, "Config", lambda app, **kwargs: NS(app=app, **kwargs))
    monkeypatch.setattr(_cli_impl.uvicorn, "Server", _BlockingServer)
    task = asyncio.create_task(_cli_impl._serve_both(_settings(), "web", "mcp"))
    await asyncio.wait_for(_BlockingServer.ready.wait(), timeout=1)
    assert not task.done() and all(server.served for server in _Server.instances)
    loop.handlers[_cli_impl.signal.SIGINT]()
    assert all(server.should_exit and not server.force_exit for server in _Server.instances)
    loop.handlers[_cli_impl.signal.SIGTERM]()
    assert all(server.force_exit for server in _Server.instances)
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_serve_both_tolerates_platform_without_signal_handlers(monkeypatch):
    _Server.instances.clear()
    monkeypatch.setattr(_cli_impl.asyncio, "get_running_loop", lambda: _Loop(unsupported=True))
    monkeypatch.setattr(_cli_impl.uvicorn, "Config", lambda app, **kwargs: NS(app=app, **kwargs))
    monkeypatch.setattr(_cli_impl.uvicorn, "Server", _Server)
    await _cli_impl._serve_both(_settings(), "web", "mcp")
    assert len(_Server.instances) == 2 and all(server.served for server in _Server.instances)


def test_console_wrapper_forwards_argv(monkeypatch):
    from llm_wiki import cli
    monkeypatch.setattr(_cli_impl, "run", lambda argv: 23 if argv == ["db-check"] else 24)
    assert cli.main(["db-check"]) == 23


def test_module_entrypoint_exits_with_main_result(monkeypatch):
    from llm_wiki import cli
    received = []
    monkeypatch.setattr(_cli_impl, "run", lambda argv: received.append(argv) or 31)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(cli.__file__, run_name="__main__")
    assert exc_info.value.code == 31
    assert received == [None]


def test_package_version_falls_back_without_installed_metadata(monkeypatch):
    package_init = Path(_cli_impl.__file__).with_name("__init__.py")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: (_ for _ in ()).throw(importlib.metadata.PackageNotFoundError(name)))
    namespace = runpy.run_path(str(package_init))
    assert namespace["__version__"] == "0.0.0"
