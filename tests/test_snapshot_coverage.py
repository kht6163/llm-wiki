"""Adversarial snapshot coverage through the public write/restore operations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from llm_wiki import snapshot
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@pytest.fixture(scope="module")
def snapshot_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("snapshot-coverage-source")
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
    with pytest.raises(ValueError, match=match):
        snapshot.restore_snapshot(archive, db_path, vault, force=True)
    assert db_path.read_bytes() == b"original database"
    assert (vault / "original.txt").read_text() == "original"


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
    assert swaps == snapshot._ATTACHMENT_ATTEMPTS
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
                manifest["format_version"] = 2
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
    for member, data in members:
        if member.name == "wiki.db":
            scratch.write_bytes(data)
            mutate(scratch)
            data = scratch.read_bytes()
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
    real_replace = snapshot.os.replace
    real_remove = snapshot._remove_path

    def fail_vault_publish(source, destination):
        if Path(destination) == vault and ".restore-stage-" in Path(source).name:
            raise OSError("vault publish failed")
        return real_replace(source, destination)

    def fail_published_db_removal(path):
        if path == db_path:
            raise OSError("published database removal failed")
        return real_remove(path)

    monkeypatch.setattr(snapshot.os, "replace", fail_vault_publish)
    monkeypatch.setattr(snapshot, "_remove_path", fail_published_db_removal)
    with pytest.raises(snapshot.RestoreRollbackError) as caught:
        snapshot.restore_snapshot(archive, db_path, vault, force=True)

    assert str(caught.value.publish_error) == "vault publish failed"
    assert [str(error) for error in caught.value.rollback_errors] == [
        "published database removal failed"
    ]
    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"


def test_restore_surfaces_cleanup_failure_after_successful_publication(
    snapshot_source, tmp_path, monkeypatch
):
    _, archive = snapshot_source
    db_path = tmp_path / "cleanup" / "wiki.db"
    vault = tmp_path / "cleanup-vault"
    real_remove = snapshot._remove_path

    def fail_stage_cleanup(path):
        if ".restore-stage-" in path.name:
            raise OSError("stage cleanup failed")
        return real_remove(path)

    monkeypatch.setattr(snapshot, "_remove_path", fail_stage_cleanup)
    with pytest.raises(OSError, match="stage cleanup failed"):
        snapshot.restore_snapshot(archive, db_path, vault, force=False)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0] == 1
    assert (vault / "note.md").read_text() == "# Snapshot\n\nbody"
