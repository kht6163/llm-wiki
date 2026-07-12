"""Adversarial snapshot coverage through the public write/restore operations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import snapshot
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest.fixture
def snapshot_source(tmp_path):
    root = tmp_path / "snapshot-source"
    settings = Settings(
        vault_path=root / "vault",
        db_path=root / "data" / "wiki.db",
        embedding_model=TEST_MODEL,
        gui_port=8180,
        mcp_port=8181,
        session_secret="test-secret",
    )
    ctx = build_context(settings, full=True)
    user_id = create_user(ctx.db, "snapshot-editor", "secret12", "editor")
    ctx.docs.create(
        Principal(user_id, "snapshot-editor", "editor", via="web"),
        "note.md",
        "# Snapshot\n\nbody",
    )
    (settings.vault_path / "asset.bin").write_bytes(b"asset")
    archive = root / "valid.tar"
    snapshot.write_snapshot(ctx.db, settings.vault_path, archive, force=False)
    return ctx, archive


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    if not root.exists() and not root.is_symlink():
        return {}
    entries: dict[str, tuple[str, bytes | str | None]] = {}
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            entries[relative] = ("symlink", os.readlink(path))
        elif path.name == ".llm-wiki.lock" or path.name.endswith(
            ".restore-journal.json"
        ):
            continue
        elif path.is_dir():
            entries[relative] = ("directory", None)
        else:
            entries[relative] = ("file", path.read_bytes())
    return entries


def _restore_artifacts(db_path: Path, vault: Path) -> set[Path]:
    patterns = (
        f".{db_path.name}.restore-stage-*",
        f".{db_path.name}.restore-backup-*",
        f".{vault.name}.restore-stage-*",
        f".{vault.name}.restore-backup-*",
        f".{db_path.name}.restore-journal.json",
    )
    return {
        path
        for parent, pattern in (
            (db_path.parent, patterns[0]),
            (db_path.parent, patterns[1]),
            (vault.parent, patterns[2]),
            (vault.parent, patterns[3]),
            (db_path.parent, patterns[4]),
        )
        for path in parent.glob(pattern)
    }


def _read_members(archive: Path) -> list[tuple[tarfile.TarInfo, bytes | None]]:
    result = []
    with tarfile.open(archive, "r") as source:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            result.append((member, extracted.read() if extracted else None))
    return result


def _write_members(target: Path, members: list[tuple[tarfile.TarInfo, bytes | None]]) -> None:
    with tarfile.open(target, "w") as archive:
        for member, data in members:
            copied = tarfile.TarInfo(member.name)
            copied.type = member.type
            copied.linkname = member.linkname
            copied.size = len(data) if data is not None else 0
            archive.addfile(copied, io.BytesIO(data) if data is not None else None)


def _rewrite(source: Path, target: Path, transform) -> None:
    _write_members(target, transform(_read_members(source)))


def _edit_manifest(members, edit):
    result = []
    for member, data in members:
        if member.name == "manifest.json":
            manifest = json.loads(data)
            replacement = edit(manifest)
            data = json.dumps(manifest if replacement is None else replacement).encode()
        result.append((member, data))
    return result


def _restore_rejects_without_live_changes(archive: Path, root: Path, match: str):
    db_path = root / "live" / "wiki.db"
    vault = root / "live-vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original database")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    before = _tree_snapshot(root)
    with pytest.raises(ValueError, match=match):
        snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert _tree_snapshot(root) == before
    assert _restore_artifacts(db_path, vault) == set()


def test_write_rejects_unsafe_and_inconsistent_source_generations(
    snapshot_source, tmp_path, monkeypatch
):
    ctx, _ = snapshot_source
    vault = ctx.settings.vault_path

    control = vault / "bad\\name.bin"
    control.write_bytes(b"bad")
    with pytest.raises(ValueError, match="unsafe vault path"):
        snapshot.write_snapshot(ctx.db, vault, tmp_path / "control.tar", force=False)
    control.unlink()

    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET path='../escape.md' WHERE path='note.md'")
    with pytest.raises(ValueError, match="unsafe vault path"):
        snapshot.write_snapshot(ctx.db, vault, tmp_path / "traversal.tar", force=False)
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET path='note.md' WHERE path='../escape.md'")

    existing = tmp_path / "existing.tar"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        snapshot.write_snapshot(ctx.db, vault, existing, force=False)
    assert existing.read_bytes() == b"keep"

    real_resolve = Path.resolve

    def escape_attachment(path, *args, **kwargs):
        if path == vault / "asset.bin":
            return tmp_path / "outside.bin"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escape_attachment)
    with pytest.raises(ValueError, match="unsafe vault path"):
        snapshot.write_snapshot(ctx.db, vault, tmp_path / "outside.tar", force=False)


def test_write_detects_attachment_replacement_after_streaming(
    snapshot_source, tmp_path, monkeypatch
):
    ctx, _ = snapshot_source
    attachment = ctx.settings.vault_path / "asset.bin"
    real_read = snapshot.os.read
    swaps = 0

    def replace_at_eof(fd, size):
        nonlocal swaps
        data = real_read(fd, size)
        if not data:
            replacement = tmp_path / f"replacement-{swaps}"
            replacement.write_bytes(f"changed-{swaps}".encode())
            os.replace(replacement, attachment)
            swaps += 1
        return data

    monkeypatch.setattr(snapshot.os, "read", replace_at_eof)
    out = tmp_path / "changed.tar"
    with pytest.raises(RuntimeError, match="attachment changed"):
        snapshot.write_snapshot(ctx.db, ctx.settings.vault_path, out, force=False)
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


def test_write_rejects_duplicate_managed_paths_and_missing_revision(snapshot_source, tmp_path):
    ctx, _ = snapshot_source
    with ctx.db.writer() as conn:
        row = conn.execute(
            "SELECT id,body,content_hash,created_at FROM revisions LIMIT 1"
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO documents(path,path_norm,title,version,content_hash,is_deleted,created_at,updated_at) "
            "SELECT 'NOTE.md','deliberately-distinct',title,version,content_hash,0,created_at,updated_at "
            "FROM documents WHERE path='note.md'"
        )
        duplicate_id = cur.lastrowid
        conn.execute(
            "INSERT INTO revisions(doc_id,version,body,content_hash,author_id,created_at) "
            "VALUES(?,?,?,?,NULL,?)",
            (duplicate_id, 1, row["body"], row["content_hash"], row["created_at"]),
        )
    with pytest.raises(ValueError, match="duplicate normalized snapshot path"):
        snapshot.write_snapshot(
            ctx.db, ctx.settings.vault_path, tmp_path / "duplicate.tar", force=False
        )
    with ctx.db.writer() as conn:
        conn.execute("DELETE FROM documents WHERE id=?", (duplicate_id,))

    with sqlite3.connect(ctx.settings.db_path) as conn:
        conn.execute("UPDATE documents SET version=999 WHERE path='note.md'")
    with pytest.raises(RuntimeError, match="no revision"):
        snapshot.write_snapshot(
            ctx.db, ctx.settings.vault_path, tmp_path / "missing-revision.tar", force=False
        )
    with sqlite3.connect(ctx.settings.db_path) as conn:
        conn.execute("UPDATE documents SET version=1 WHERE path='note.md'")


def test_write_verification_requires_every_staged_member(snapshot_source, tmp_path, monkeypatch):
    ctx, _ = snapshot_source
    real_extract = tarfile.TarFile.extractfile

    def hide_attachment(archive, member, *args, **kwargs):
        name = member if isinstance(member, str) else member.name
        if archive.mode == "r" and name == "vault/asset.bin":
            return None
        return real_extract(archive, member, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", hide_attachment)
    out = tmp_path / "missing-verified-member.tar"
    with pytest.raises(RuntimeError, match="verification failed"):
        snapshot.write_snapshot(ctx.db, ctx.settings.vault_path, out, force=False)
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unsafe-name", "unsafe archive member"),
        ("missing-manifest", "missing manifest"),
        ("bad-json", "invalid manifest"),
        ("bad-format", "invalid manifest"),
        ("bad-version", "unsupported snapshot format"),
        ("missing-db", "missing wiki"),
        ("files-not-list", "invalid manifest files"),
        ("entry-not-dict", "invalid manifest entry"),
        ("bad-path", "invalid manifest path"),
        ("duplicate-path", "duplicate or unsafe"),
        ("bad-kind", "invalid manifest kind"),
        ("bad-size", "invalid manifest size"),
        ("bad-hash", "invalid manifest hash"),
    ],
)
def test_restore_rejects_descriptor_and_manifest_corruption(
    snapshot_source, tmp_path, case, message
):
    _, valid = snapshot_source
    damaged = tmp_path / f"{case}.tar"

    def transform(members):
        if case == "unsafe-name":
            members[0][0].name = "bad\\name"
            return members
        if case == "missing-manifest":
            return [item for item in members if item[0].name != "manifest.json"]
        if case == "missing-db":
            return [item for item in members if item[0].name != "wiki.db"]
        if case == "bad-json":
            return [
                (member, b"{" if member.name == "manifest.json" else data)
                for member, data in members
            ]

        def edit(manifest):
            if case == "bad-format":
                manifest["format"] = "another-format"
            elif case == "bad-version":
                manifest["format_version"] = 3
            elif case == "files-not-list":
                manifest["files"] = {}
            elif case == "entry-not-dict":
                manifest["files"] = ["not-an-entry"]
            elif case == "bad-path":
                manifest["files"][0]["path"] = "note.md"
            elif case == "duplicate-path":
                manifest["files"].append(dict(manifest["files"][0]))
            elif case == "bad-kind":
                manifest["files"][0]["kind"] = "other"
            elif case == "bad-size":
                manifest["files"][0]["size"] = True
            elif case == "bad-hash":
                manifest["files"][0]["sha256"] = "g" * 64

        return _edit_manifest(members, edit)

    _rewrite(valid, damaged, transform)
    _restore_rejects_without_live_changes(damaged, tmp_path / "target", message)


@pytest.mark.parametrize("member_name", ["manifest.json", "vault/note.md"])
def test_restore_rejects_unreadable_archive_members(
    snapshot_source, tmp_path, monkeypatch, member_name
):
    _, archive = snapshot_source
    real_extract = tarfile.TarFile.extractfile

    def hide_member(tar, member, *args, **kwargs):
        name = member if isinstance(member, str) else member.name
        if name == member_name:
            return None
        return real_extract(tar, member, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", hide_member)
    message = "missing manifest" if member_name == "manifest.json" else "payload is missing"
    _restore_rejects_without_live_changes(
        archive, tmp_path / member_name.replace("/", "-"), message
    )


def _mutate_database_member(members, scratch: Path, mutate) -> list:
    result = []
    database = b""
    for member, data in members:
        if member.name == "wiki.db":
            scratch.write_bytes(data)
            mutate(scratch)
            data = scratch.read_bytes()
            database = data
        elif member.name == "manifest.json":
            manifest = json.loads(data)
            manifest["database"] = {
                "size": len(database),
                "sha256": hashlib.sha256(database).hexdigest(),
            }
            data = json.dumps(manifest).encode()
        result.append((member, data))
    return result


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("invalid-db", "invalid snapshot database"),
        ("schema-missing", "unsupported snapshot schema"),
        ("revision-missing", "no current revision"),
        ("schema-mismatch", "schema version mismatch"),
        ("doc-count", "document count mismatch"),
        ("managed-missing", "missing a managed document"),
        ("managed-unreadable", "invalid staged managed document"),
        ("managed-extra", "undeclared managed document"),
    ],
)
def test_restore_rejects_database_and_projection_mismatches(
    snapshot_source, tmp_path, case, message
):
    _, valid = snapshot_source
    damaged = tmp_path / f"database-{case}.tar"
    scratch = tmp_path / f"scratch-{case}.db"

    def transform(members):
        if case == "invalid-db":
            return [
                (member, b"not sqlite" if member.name == "wiki.db" else data)
                for member, data in members
            ]
        if case in {"schema-missing", "revision-missing", "schema-mismatch"}:

            def mutate(path):
                with sqlite3.connect(path) as conn:
                    if case == "schema-missing":
                        conn.execute("DELETE FROM meta WHERE k='schema_version'")
                    elif case == "revision-missing":
                        conn.execute("UPDATE documents SET version=999 WHERE path='note.md'")
                    else:
                        conn.execute(
                            "UPDATE meta SET v=CAST(v AS INTEGER)-1 WHERE k='schema_version'"
                        )

            return _mutate_database_member(members, scratch, mutate)
        if case == "doc-count":
            return _edit_manifest(members, lambda manifest: manifest.update(doc_count=2))
        if case == "managed-missing":
            return _edit_manifest(
                members,
                lambda manifest: manifest["files"][0].update(kind="attachment"),
            )
        if case == "managed-unreadable":
            bad = b"\xff\xfe"
            result = []
            for member, data in members:
                if member.name == "vault/note.md":
                    data = bad
                elif member.name == "manifest.json":
                    manifest = json.loads(data)
                    entry = next(e for e in manifest["files"] if e["path"] == "vault/note.md")
                    entry["size"] = len(bad)
                    entry["sha256"] = hashlib.sha256(bad).hexdigest()
                    data = json.dumps(manifest).encode()
                result.append((member, data))
            return result
        if case == "managed-extra":
            body = b"extra"
            extra = {
                "path": "vault/extra.md",
                "kind": "managed",
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            result = _edit_manifest(members, lambda manifest: manifest["files"].append(extra))
            result.append((tarfile.TarInfo("vault/extra.md"), body))
            return result
        raise AssertionError(case)

    _rewrite(valid, damaged, transform)
    _restore_rejects_without_live_changes(damaged, tmp_path / "target", message)


def test_restore_rejects_corrupt_tar_stream(tmp_path):
    archive = tmp_path / "corrupt.tar"
    archive.write_bytes(b"not a tar archive")
    _restore_rejects_without_live_changes(archive, tmp_path / "target", "invalid snapshot archive")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_restore_rejects_fifo_archive_without_blocking(tmp_path, monkeypatch):
    archive = tmp_path / "snapshot.fifo"
    os.mkfifo(archive)
    monkeypatch.setattr(
        snapshot.tarfile,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("FIFO reached tarfile.open")
        ),
    )

    with pytest.raises(ValueError, match="regular file"):
        snapshot.restore_snapshot(
            archive, tmp_path / "fifo.db", tmp_path / "fifo-vault", force=False
        )


def test_restore_rejects_symlink_archive(tmp_path):
    target = tmp_path / "target.tar"
    target.write_bytes(b"not important")
    archive = tmp_path / "snapshot.tar"
    archive.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        snapshot.restore_snapshot(
            archive, tmp_path / "link.db", tmp_path / "link-vault", force=False
        )


@pytest.mark.parametrize("layout", ["db-in-vault", "vault-in-db", "symlink-db-in-vault"])
def test_restore_rejects_overlapping_targets_before_creating_artifacts(
    snapshot_source, tmp_path, layout
):
    _, archive = snapshot_source
    root = tmp_path / layout
    root.mkdir()
    if layout == "db-in-vault":
        vault = root / "vault"
        db_path = vault / "data" / "wiki.db"
    elif layout == "vault-in-db":
        db_path = root / "database-root"
        vault = db_path / "vault"
    else:
        vault = root / "vault"
        (vault / "data").mkdir(parents=True)
        db_parent = root / "db-link"
        db_parent.symlink_to(vault / "data", target_is_directory=True)
        db_path = db_parent / "wiki.db"
    if layout == "vault-in-db":
        vault.mkdir(parents=True, exist_ok=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"original database")
        vault.mkdir(parents=True, exist_ok=True)
    (vault / "original.txt").write_text("original vault")
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="overlap"):
        snapshot.restore_snapshot(archive, db_path, vault, force=True)

    assert _tree_snapshot(root) == before
    assert not snapshot.restore_journal_path(db_path).exists()
    assert not (db_path.parent / ".llm-wiki.lock").exists()
    assert _restore_artifacts(db_path, vault) == set()


def test_restore_layout_allows_adjacent_database_and_vault(tmp_path):
    root = tmp_path / "adjacent"
    snapshot.validate_restore_layout(root / "wiki.db", root / "vault")


def test_restore_passes_stable_archive_descriptor_to_tarfile(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    real_open = snapshot.tarfile.open
    observed = []

    def inspect_open(*args, **kwargs):
        if kwargs.get("mode") == "r:":
            observed.append(kwargs.get("fileobj"))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(snapshot.tarfile, "open", inspect_open)
    pending = snapshot.restore_snapshot(
        archive, tmp_path / "descriptor.db", tmp_path / "descriptor-vault", force=False
    )
    pending.finalize()

    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].closed


def test_restore_rejects_archive_changed_during_read(snapshot_source, tmp_path, monkeypatch):
    _, archive = snapshot_source
    real_open = snapshot.tarfile.open

    def mutate_after_read(*args, **kwargs):
        opened = real_open(*args, **kwargs)
        real_close = opened.close

        def close():
            real_close()
            with archive.open("ab") as output:
                output.write(b"changed")

        opened.close = close
        return opened

    monkeypatch.setattr(snapshot.tarfile, "open", mutate_after_read)
    with pytest.raises(ValueError, match="changed while reading"):
        snapshot.restore_snapshot(
            archive, tmp_path / "changed.db", tmp_path / "changed-vault", force=False
        )


def test_snapshot_v2_manifest_authenticates_database(snapshot_source):
    _, archive = snapshot_source
    with tarfile.open(archive, "r") as source:
        manifest_file = source.extractfile("manifest.json")
        database_file = source.extractfile("wiki.db")
        assert manifest_file is not None and database_file is not None
        manifest = json.load(manifest_file)
        database = database_file.read()

    assert manifest["format_version"] == 2
    assert manifest["database"] == {
        "size": len(database),
        "sha256": hashlib.sha256(database).hexdigest(),
    }


def test_restore_rejects_database_digest_mismatch(snapshot_source, tmp_path):
    _, valid = snapshot_source
    damaged = tmp_path / "database-digest.tar"

    def corrupt(members):
        return [
            (member, data[:-1] + bytes([data[-1] ^ 1]))
            if member.name == "wiki.db" and data
            else (member, data)
            for member, data in members
        ]

    _rewrite(valid, damaged, corrupt)
    _restore_rejects_without_live_changes(
        damaged, tmp_path / "digest-target", "database verification failed"
    )


def test_restore_v1_compatibility_uses_integrity_and_semantic_checks(
    snapshot_source, tmp_path
):
    _, valid = snapshot_source
    legacy = tmp_path / "legacy-v1.tar"

    def downgrade(members):
        def edit(manifest):
            manifest["format_version"] = 1
            manifest.pop("database")

        return _edit_manifest(members, edit)

    _rewrite(valid, legacy, downgrade)
    pending = snapshot.restore_snapshot(
        legacy, tmp_path / "legacy.db", tmp_path / "legacy-vault", force=False
    )
    pending.finalize()

    assert pending.doc_count == 1


def test_restore_full_integrity_check_rejects_unrelated_page_corruption(
    snapshot_source, tmp_path
):
    _, valid = snapshot_source
    damaged = tmp_path / "unrelated-page-corruption.tar"
    scratch = tmp_path / "unrelated.db"
    corrupted_database = b""

    def corrupt(members):
        nonlocal corrupted_database
        result = []
        for member, data in members:
            if member.name == "wiki.db":
                scratch.write_bytes(data)
                with sqlite3.connect(scratch) as conn:
                    conn.execute("CREATE TABLE unrelated_payload(value BLOB)")
                    conn.execute("INSERT INTO unrelated_payload VALUES (zeroblob(8000))")
                    root_page = conn.execute(
                        "SELECT rootpage FROM sqlite_master WHERE name='unrelated_payload'"
                    ).fetchone()[0]
                database = scratch.read_bytes()
                page_size = int.from_bytes(database[16:18], "big") or 65536
                offset = (root_page - 1) * page_size
                corrupted_database = database[:offset] + b"\xff" + database[offset + 1:]
                data = corrupted_database
            elif member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["database"] = {
                    "size": len(corrupted_database),
                    "sha256": hashlib.sha256(corrupted_database).hexdigest(),
                }
                data = json.dumps(manifest).encode()
            result.append((member, data))
        return result

    _rewrite(valid, damaged, corrupt)
    _restore_rejects_without_live_changes(
        damaged, tmp_path / "integrity-target", "integrity check failed"
    )


@pytest.mark.parametrize(
    ("constant", "value", "message"),
    [
        ("MAX_SNAPSHOT_ARCHIVE_BYTES", 1, "archive size limit"),
        ("MAX_SNAPSHOT_MEMBERS", 2, "member count limit"),
        ("MAX_SNAPSHOT_MANIFEST_BYTES", 1, "manifest size limit"),
        ("MAX_SNAPSHOT_MEMBER_BYTES", 1, "member size limit"),
        ("MAX_SNAPSHOT_TOTAL_BYTES", 1, "total size limit"),
    ],
)
def test_restore_enforces_archive_extraction_budgets(
    snapshot_source, tmp_path, monkeypatch, constant, value, message
):
    _, archive = snapshot_source
    monkeypatch.setattr(snapshot, constant, value)
    _restore_rejects_without_live_changes(
        archive, tmp_path / f"budget-{constant}", message
    )


def test_restore_report_rejects_actions_after_finalize(snapshot_source, tmp_path):
    _, archive = snapshot_source
    pending = snapshot.restore_snapshot(
        archive, tmp_path / "once.db", tmp_path / "once-vault", force=False
    )
    pending.finalize()

    assert pending.finalize() == ()
    with pytest.raises(RuntimeError, match="no longer pending"):
        pending.rollback(RuntimeError("too late"))


def test_attachment_staging_handles_identity_and_open_failures(
    tmp_path, monkeypatch
):
    root = tmp_path / "vault"
    root.mkdir()
    attachment = root / "asset.bin"
    attachment.write_bytes(b"asset")
    staged = tmp_path / "staged"
    visible = os.lstat(attachment)
    real_fstat = snapshot.os.fstat

    monkeypatch.setattr(
        snapshot.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=visible.st_mode,
            st_dev=visible.st_dev,
            st_ino=visible.st_ino + 1,
            st_size=visible.st_size,
            st_mtime_ns=visible.st_mtime_ns,
            st_ctime_ns=visible.st_ctime_ns,
        ),
    )
    assert snapshot._stage_attachment_once(attachment, root, staged) is None

    monkeypatch.setattr(snapshot.os, "fstat", real_fstat)
    monkeypatch.setattr(snapshot.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open")))
    assert snapshot._stage_attachment_once(attachment, root, staged) is None


def test_snapshot_walk_skips_real_directories(snapshot_source, tmp_path):
    ctx, _ = snapshot_source
    folder = ctx.settings.vault_path / "assets"
    folder.mkdir()
    (folder / "nested.bin").write_bytes(b"nested")
    out = tmp_path / "nested.tar"

    snapshot.write_snapshot(ctx.db, ctx.settings.vault_path, out, force=False)

    with tarfile.open(out) as archive:
        assert "vault/assets/nested.bin" in archive.getnames()
    pending = snapshot.restore_snapshot(
        out, tmp_path / "nested.db", tmp_path / "nested-vault", force=False
    )
    pending.finalize()


def test_copy_archive_file_enforces_streaming_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_MEMBER_BYTES", 1)
    with pytest.raises(ValueError, match="member size limit"):
        snapshot._copy_archive_file(io.BytesIO(b"too large"), tmp_path / "payload")


def test_manifest_stream_limit_defends_against_size_mismatch(monkeypatch):
    member = tarfile.TarInfo("manifest.json")
    member.size = 1

    class Archive:
        def __iter__(self):
            return iter([member])

        def extractfile(self, requested):
            assert requested is member
            return io.BytesIO(b"{}")

    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_MANIFEST_BYTES", 1)
    with pytest.raises(ValueError, match="manifest size limit"):
        snapshot._read_restore_manifest(Archive())


@pytest.mark.parametrize(
    ("database", "message"),
    [
        (None, "invalid manifest database"),
        ({"size": True, "sha256": "0" * 64}, "database size"),
        ({"size": 1, "sha256": None}, "database hash"),
        ({"size": 1, "sha256": "0" * 63}, "database hash"),
        ({"size": 1, "sha256": "g" * 64}, "database hash"),
    ],
)
def test_restore_rejects_invalid_v2_database_descriptor(
    snapshot_source, tmp_path, database, message
):
    _, valid = snapshot_source
    damaged = tmp_path / f"bad-database-{hash(str(database))}.tar"
    _rewrite(
        valid,
        damaged,
        lambda members: _edit_manifest(
            members, lambda manifest: manifest.update(database=database)
        ),
    )
    _restore_rejects_without_live_changes(damaged, tmp_path / damaged.stem, message)


@pytest.mark.parametrize("mode", ["integrity", "schema"])
def test_staged_database_reports_integrity_and_sql_errors(tmp_path, monkeypatch, mode):
    class Result:
        def fetchall(self):
            return [("broken",)]

    class Connection:
        row_factory = None

        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                if mode == "integrity":
                    return Result()
                return SimpleNamespace(fetchall=lambda: [("ok",)])
            raise sqlite3.OperationalError("schema read failed")

        def close(self):
            pass

    monkeypatch.setattr(snapshot.sqlite3, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(ValueError, match="integrity check|invalid snapshot database"):
        snapshot._validate_staged_database(tmp_path / "db", tmp_path, {}, {})


def test_restore_staging_setup_failure_cleans_database_and_releases_lock(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    db_path = tmp_path / "setup" / "wiki.db"
    vault = tmp_path / "setup-vault"
    monkeypatch.setattr(
        snapshot.tempfile,
        "mkdtemp",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("vault staging failed")),
    )

    with pytest.raises(OSError, match="vault staging failed"):
        snapshot.restore_snapshot(archive, db_path, vault, force=False)

    assert not list(db_path.parent.glob(f".{db_path.name}.restore-stage-*"))
    with snapshot.ProjectLock(db_path):
        pass


def test_stable_archive_descriptor_setup_and_close_failures(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar"
    archive.write_bytes(b"archive")
    visible = os.lstat(archive)
    real_fstat = snapshot.os.fstat

    monkeypatch.setattr(
        snapshot.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=visible.st_mode,
            st_dev=visible.st_dev,
            st_ino=visible.st_ino + 1,
            st_size=visible.st_size,
            st_mtime_ns=visible.st_mtime_ns,
            st_ctime_ns=visible.st_ctime_ns,
        ),
    )
    with pytest.raises(ValueError, match="stable regular file"):
        with snapshot._open_stable_snapshot_archive(archive):
            pass

    monkeypatch.setattr(snapshot.os, "fstat", real_fstat)
    monkeypatch.setattr(
        snapshot.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open failed")),
    )
    with pytest.raises(ValueError, match="stable regular file"):
        with snapshot._open_stable_snapshot_archive(archive):
            pass


def test_stable_archive_closes_source_when_final_fstat_fails(tmp_path, monkeypatch):
    archive = tmp_path / "archive.tar"
    archive.write_bytes(b"archive")
    real_fstat = snapshot.os.fstat
    calls = 0

    def fail_final_fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final stat failed")
        return real_fstat(fd)

    monkeypatch.setattr(snapshot.os, "fstat", fail_final_fstat)
    with pytest.raises(ValueError, match="stable regular file"):
        with snapshot._open_stable_snapshot_archive(archive):
            pass


def _coverage_restore_journal(tmp_path, state="prepared"):
    db_path = tmp_path / "data" / "wiki.db"
    vault = tmp_path / "vault"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    vault.parent.mkdir(parents=True, exist_ok=True)
    targets = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        vault,
    )
    records = tuple(
        snapshot._RestoreTarget(
            target,
            target.with_name(f".{target.name}.restore-backup-coverage"),
            False,
        )
        for target in targets
    )
    lock = snapshot.ProjectLock(db_path).acquire()
    return snapshot._RestoreJournal(
        records,
        targets,
        lock,
        snapshot.restore_journal_path(db_path),
        state,
    )


def test_restore_journal_persist_and_rollback_fsync_failures(tmp_path, monkeypatch):
    original_fsync = snapshot._fsync_directory
    journal = _coverage_restore_journal(tmp_path / "persist")
    monkeypatch.setattr(
        snapshot,
        "_fsync_directory",
        lambda path: (_ for _ in ()).throw(OSError("journal fsync failed")),
    )
    with pytest.raises(OSError, match="journal fsync failed"):
        journal.persist("pending")
    journal.process_lock.release()
    monkeypatch.setattr(snapshot, "_fsync_directory", original_fsync)

    for failure_call, message in [(2, "rollback fsync failed"), (4, "finish failed")]:
        journal = _coverage_restore_journal(tmp_path / message.replace(" ", "-"))
        calls = 0

        def fail_selected(path, selected=failure_call, error_message=message):
            nonlocal calls
            calls += 1
            if calls == selected:
                raise OSError(error_message)
            return original_fsync(path)

        monkeypatch.setattr(snapshot, "_fsync_directory", fail_selected)
        errors = journal._rollback_files()
        assert [str(error) for error in errors] == [message]
        journal.process_lock.release()
        monkeypatch.setattr(snapshot, "_fsync_directory", original_fsync)


def test_restore_journal_rollback_reports_persist_failure(tmp_path, monkeypatch):
    journal = _coverage_restore_journal(tmp_path)
    monkeypatch.setattr(
        journal,
        "persist",
        lambda state: (_ for _ in ()).throw(OSError("persist failed")),
    )

    assert [str(error) for error in journal._rollback_files()] == ["persist failed"]
    journal.process_lock.release()


@pytest.mark.parametrize("case", ["large", "json", "top", "target-type", "target-value"])
def test_recovery_rejects_invalid_durable_journals(tmp_path, case, monkeypatch):
    db_path = tmp_path / case / "wiki.db"
    vault = tmp_path / f"{case}-vault"
    db_path.parent.mkdir(parents=True)
    targets = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), vault]
    data = {
        "format": snapshot._RESTORE_JOURNAL_FORMAT,
        "version": snapshot._RESTORE_JOURNAL_VERSION,
        "state": "prepared",
        "replacement_targets": [str(path) for path in targets],
        "targets": [
            {
                "target": str(path),
                "backup": str(
                    path.with_name(f".{path.name}.restore-backup-coverage")
                ),
                "had_original": False,
            }
            for path in targets
        ],
    }
    journal_path = snapshot.restore_journal_path(db_path)
    if case == "large":
        journal_path.write_bytes(b"x" * 20)
        monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_MANIFEST_BYTES", 10)
    elif case == "json":
        journal_path.write_bytes(b"{")
    else:
        if case == "top":
            data["format"] = "wrong"
        elif case == "target-type":
            data["targets"][0] = "wrong"
        else:
            data["targets"][0]["backup"] = str(tmp_path / "outside")
        journal_path.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="restore journal"):
        snapshot.recover_pending_restore(db_path, vault)


def test_recovery_completes_interrupted_finalize(snapshot_source, tmp_path):
    _, archive = snapshot_source
    db_path = tmp_path / "finalizing" / "wiki.db"
    vault = tmp_path / "finalizing-vault"
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=False)
    assert pending._journal is not None
    pending._journal.persist("finalizing")
    pending._journal.process_lock.release()

    assert snapshot.recover_pending_restore(db_path, vault) == "finalized"
    assert not snapshot.restore_journal_path(db_path).exists()


def test_recovery_reports_interrupted_finalize_cleanup_failure(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    db_path = tmp_path / "finalize-warning" / "wiki.db"
    vault = tmp_path / "finalize-warning-vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original")
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert pending._journal is not None
    backup = next(
        item.backup for item in pending._journal.targets if item.had_original
    )
    pending._journal.persist("finalizing")
    pending._journal.process_lock.release()
    real_remove = snapshot._remove_path

    def fail_backup_cleanup(path):
        if path == backup:
            raise OSError("backup cleanup failed")
        return real_remove(path)

    monkeypatch.setattr(snapshot, "_remove_path", fail_backup_cleanup)
    with pytest.raises(OSError, match="backup cleanup failed"):
        snapshot.recover_pending_restore(db_path, vault)


def test_recovery_reports_missing_backup(snapshot_source, tmp_path):
    _, archive = snapshot_source
    db_path = tmp_path / "missing-backup" / "wiki.db"
    vault = tmp_path / "missing-backup-vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original")
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert pending._journal is not None
    backup = next(
        item.backup for item in pending._journal.targets if item.had_original
    )
    backup.unlink()
    pending._journal.process_lock.release()

    with pytest.raises(snapshot.RestoreRollbackError, match="rollback could not complete"):
        snapshot.recover_pending_restore(db_path, vault)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_recovery_rejects_fifo_journal_before_read(tmp_path, monkeypatch):
    db_path = tmp_path / "fifo-journal" / "wiki.db"
    vault = tmp_path / "fifo-journal-vault"
    db_path.parent.mkdir(parents=True)
    journal = snapshot.restore_journal_path(db_path)
    os.mkfifo(journal)
    real_read_bytes = Path.read_bytes

    def reject_fifo_read(path):
        if path == journal:
            raise AssertionError("FIFO journal reached Path.read_bytes")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_fifo_read)
    with pytest.raises(ValueError, match="regular file"):
        snapshot.recover_pending_restore(db_path, vault)


def test_recovery_rejects_symlink_journal(tmp_path):
    db_path = tmp_path / "link-journal" / "wiki.db"
    vault = tmp_path / "link-journal-vault"
    db_path.parent.mkdir(parents=True)
    external = tmp_path / "external-journal.json"
    external.write_text("{}")
    snapshot.restore_journal_path(db_path).symlink_to(external)

    with pytest.raises(ValueError, match="regular file"):
        snapshot.recover_pending_restore(db_path, vault)


def test_recovery_rejects_backup_replaced_by_symlink_without_touching_live_target(
    snapshot_source, tmp_path
):
    _, archive = snapshot_source
    db_path = tmp_path / "backup-link" / "wiki.db"
    vault = tmp_path / "backup-link-vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original database")
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert pending._journal is not None
    backup = next(
        item.backup for item in pending._journal.targets if item.target == db_path
    )
    backup.unlink()
    external = tmp_path / "external-database"
    external.write_bytes(b"external must remain untouched")
    backup.symlink_to(external)
    replacement = db_path.read_bytes()
    pending._journal.process_lock.release()

    with pytest.raises(ValueError, match="backup.*symlink|identity"):
        snapshot.recover_pending_restore(db_path, vault)

    assert not db_path.is_symlink()
    assert db_path.read_bytes() == replacement
    assert external.read_bytes() == b"external must remain untouched"
    assert snapshot.restore_journal_path(db_path).exists()


def test_recovery_rejects_live_target_identity_change_without_deleting_it(
    snapshot_source, tmp_path
):
    _, archive = snapshot_source
    db_path = tmp_path / "live-identity" / "wiki.db"
    vault = tmp_path / "live-identity-vault"
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=False)
    assert pending._journal is not None
    replacement = b"operator replacement must remain"
    swapped = db_path.with_name("swapped.db")
    swapped.write_bytes(replacement)
    os.replace(swapped, db_path)
    pending._journal.process_lock.release()

    with pytest.raises(ValueError, match="live target identity changed"):
        snapshot.recover_pending_restore(db_path, vault)

    assert db_path.read_bytes() == replacement
    assert snapshot.restore_journal_path(db_path).exists()


def test_recovery_preserves_journal_when_rolling_back_target_and_backup_are_missing(
    snapshot_source, tmp_path
):
    _, archive = snapshot_source
    db_path = tmp_path / "missing-both" / "wiki.db"
    vault = tmp_path / "missing-both-vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original database")
    pending = snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert pending._journal is not None
    record = next(
        item for item in pending._journal.targets if item.target == db_path
    )
    pending._journal.persist("rolling_back")
    db_path.unlink()
    record.backup.unlink()
    pending._journal.process_lock.release()

    with pytest.raises(snapshot.RestoreRollbackError, match="rollback could not complete"):
        snapshot.recover_pending_restore(db_path, vault)

    assert snapshot.restore_journal_path(db_path).exists()


def test_restore_journal_rejects_live_symlink_invalid_backup_type_and_identity(
    tmp_path,
):
    external = tmp_path / "external"
    external.write_bytes(b"external")

    live_link = _coverage_restore_journal(tmp_path / "live-link")
    live_link.targets[0].target.symlink_to(external)
    with pytest.raises(ValueError, match="live target.*symlink"):
        live_link._validate_paths()
    live_link.process_lock.release()

    bad_type = _coverage_restore_journal(tmp_path / "bad-type")
    bad_type.targets[0].backup.mkdir()
    with pytest.raises(ValueError, match="invalid type"):
        bad_type._validate_paths()
    bad_type.process_lock.release()

    changed = _coverage_restore_journal(tmp_path / "changed")
    changed.targets[0].backup.write_bytes(b"replacement backup")
    changed.targets[0].backup_identity = (0, 0, stat.S_IFREG)
    with pytest.raises(ValueError, match="identity changed"):
        changed._validate_paths()
    changed.process_lock.release()


def test_rolling_back_with_restored_target_and_missing_backup_can_finish(tmp_path):
    journal = _coverage_restore_journal(tmp_path, state="rolling_back")
    record = journal.targets[0]
    record.had_original = True
    record.target.parent.mkdir(parents=True, exist_ok=True)
    record.target.write_bytes(b"already restored")

    assert journal._rollback_files() == []
    assert not journal.path.exists()
    journal.process_lock.release()


def test_stable_journal_descriptor_identity_size_mutation_and_open_failures(
    tmp_path, monkeypatch
):
    journal = tmp_path / "journal.json"
    journal.write_bytes(b"{}")
    visible = os.lstat(journal)
    real_fstat = snapshot.os.fstat
    real_open = snapshot.os.open

    monkeypatch.setattr(
        snapshot.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=visible.st_mode,
            st_dev=visible.st_dev,
            st_ino=visible.st_ino + 1,
            st_size=visible.st_size,
            st_mtime_ns=visible.st_mtime_ns,
            st_ctime_ns=visible.st_ctime_ns,
        ),
    )
    with pytest.raises(ValueError, match="stable regular file"):
        snapshot._read_restore_journal(journal)

    calls = 0

    def changed_after_read(fd):
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size + 1,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns,
            )
        return result

    monkeypatch.setattr(snapshot.os, "fstat", changed_after_read)
    with pytest.raises(ValueError, match="changed while reading"):
        snapshot._read_restore_journal(journal)

    monkeypatch.setattr(snapshot.os, "fstat", real_fstat)
    monkeypatch.setattr(snapshot, "MAX_SNAPSHOT_MANIFEST_BYTES", 1)
    real_read = snapshot.os.read
    bounded_visible = SimpleNamespace(
        st_mode=visible.st_mode,
        st_dev=visible.st_dev,
        st_ino=visible.st_ino,
        st_size=1,
        st_mtime_ns=visible.st_mtime_ns,
        st_ctime_ns=visible.st_ctime_ns,
    )
    monkeypatch.setattr(snapshot.os, "lstat", lambda path: bounded_visible)
    monkeypatch.setattr(snapshot.os, "fstat", lambda fd: bounded_visible)
    monkeypatch.setattr(
        snapshot.os,
        "read",
        lambda fd, size: real_read(fd, 2),
    )
    with pytest.raises(ValueError, match="too large"):
        snapshot._read_restore_journal(journal)

    monkeypatch.setattr(snapshot.os, "open", real_open)
    monkeypatch.setattr(
        snapshot.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("open failed")),
    )
    with pytest.raises(ValueError, match="stable regular file"):
        snapshot._read_restore_journal(journal)


def test_restore_reports_published_target_removal_failure_and_preserves_originals(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    db_path = tmp_path / "publish" / "wiki.db"
    vault = tmp_path / "publish-vault"
    db_path.parent.mkdir()
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    before = _tree_snapshot(tmp_path)
    real_replace = snapshot.os.replace
    real_unlink = Path.unlink

    def fail_vault_publish(source, destination):
        if Path(destination) == vault and ".restore-stage-" in Path(source).name:
            raise OSError("vault publish failed")
        return real_replace(source, destination)

    def fail_published_db_removal(path, *args, **kwargs):
        if path == db_path:
            raise OSError("published database removal failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshot.os, "replace", fail_vault_publish)
    monkeypatch.setattr(Path, "unlink", fail_published_db_removal)
    with pytest.raises(snapshot.RestoreRollbackError) as caught:
        snapshot.restore_snapshot(archive, db_path, vault, force=True)

    assert str(caught.value.publish_error) == "vault publish failed"
    assert [str(error) for error in caught.value.rollback_errors] == [
        "published database removal failed"
    ]
    assert caught.value.backup_paths == ()
    assert _tree_snapshot(tmp_path) == before
    assert _restore_artifacts(db_path, vault) == {
        snapshot.restore_journal_path(db_path)
    }


def test_restore_surfaces_cleanup_failure_after_successful_publication(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    expected_root = tmp_path / "expected"
    expected_db = expected_root / "data" / "wiki.db"
    expected_vault = expected_root / "vault"
    snapshot.restore_snapshot(archive, expected_db, expected_vault, force=False)

    actual_root = tmp_path / "actual"
    db_path = actual_root / "data" / "wiki.db"
    vault = actual_root / "vault"
    real_mkstemp = snapshot.tempfile.mkstemp
    real_unlink = Path.unlink
    staged_databases: list[Path] = []

    def capture_staged_database(*args, **kwargs):
        fd, raw_path = real_mkstemp(*args, **kwargs)
        if ".restore-stage-" in Path(raw_path).name:
            staged_databases.append(Path(raw_path))
        return fd, raw_path

    def fail_stage_cleanup(path, *args, **kwargs):
        if staged_databases and path == staged_databases[0]:
            raise OSError("stage cleanup failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshot.tempfile, "mkstemp", capture_staged_database)
    monkeypatch.setattr(Path, "unlink", fail_stage_cleanup)
    with pytest.raises(OSError, match="stage cleanup failed"):
        snapshot.restore_snapshot(archive, db_path, vault, force=False)

    assert len(staged_databases) == 1
    assert not staged_databases[0].exists()
    assert _tree_snapshot(actual_root) == {".": ("directory", None), "data": ("directory", None)}
    assert _restore_artifacts(db_path, vault) == set()


def test_restore_preserves_primary_error_when_exact_staging_tree_cleanup_fails(
    snapshot_source, tmp_path, monkeypatch
):
    _, valid = snapshot_source
    damaged = tmp_path / "invalid-database.tar"
    _rewrite(
        valid,
        damaged,
        lambda members: [
            (member, b"not sqlite" if member.name == "wiki.db" else data)
            for member, data in members
        ],
    )
    root = tmp_path / "live"
    db_path = root / "data" / "wiki.db"
    vault = root / "vault"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"original database")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    before_db = _tree_snapshot(db_path.parent)
    before_vault = _tree_snapshot(vault)

    real_mkdtemp = snapshot.tempfile.mkdtemp
    real_rmtree = snapshot.shutil.rmtree
    staged_vaults: list[Path] = []

    def capture_staged_vault(*args, **kwargs):
        raw_path = real_mkdtemp(*args, **kwargs)
        staged_vaults.append(Path(raw_path))
        return raw_path

    def fail_exact_staged_vault(path, *args, **kwargs):
        if staged_vaults and Path(path) == staged_vaults[0]:
            raise OSError("staged vault cleanup failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(snapshot.tempfile, "mkdtemp", capture_staged_vault)
    monkeypatch.setattr(snapshot.shutil, "rmtree", fail_exact_staged_vault)
    with pytest.raises(
        snapshot.RestorePreparationError, match="invalid snapshot database"
    ) as caught:
        snapshot.restore_snapshot(damaged, db_path, vault, force=True)

    assert str(caught.value.primary_error).endswith("invalid snapshot database")
    assert [str(error) for error in caught.value.cleanup_errors] == [
        "staged vault cleanup failed"
    ]
    assert len(staged_vaults) == 1
    leftover = staged_vaults[0]
    assert caught.value.staging_paths == (leftover,)
    assert leftover.exists()
    assert _tree_snapshot(leftover) == {".": ("directory", None)}
    assert _tree_snapshot(db_path.parent) == before_db
    assert _tree_snapshot(vault) == before_vault
    assert _restore_artifacts(db_path, vault) == {leftover}

    real_rmtree(leftover)
    assert _restore_artifacts(db_path, vault) == set()
