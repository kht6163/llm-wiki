"""#10 single-command full snapshot/restore (DB + vault + manifest as one .tar)."""
import io
import json
import sqlite3
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import _cli_impl
from llm_wiki import snapshot as snapshot_writer
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services.auth import Principal, create_user

TEST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _settings(tmp_path, name, **over):
    return Settings(
        vault_path=tmp_path / f"vault_{name}", db_path=tmp_path / name / "wiki.db",
        embedding_model=TEST_MODEL, gui_port=8090, mcp_port=8091,
        session_secret="test-secret", **over,
    )


def _seed(settings):
    ctx = build_context(settings, full=True)
    uid = create_user(ctx.db, "ed", "secret12", "editor")
    ctx.docs.create(Principal(uid, "ed", "editor", via="web"), "note.md", "# Note\n\nhello world")
    return ctx


def test_snapshot_restore_roundtrip(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "src"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    tar = tmp_path / "snap.tar"
    assert _cli_impl._snapshot(SimpleNamespace(out=str(tar), force=False)) == 0
    assert tar.exists()

    # Restore into a fresh, empty target.
    dst = _settings(tmp_path, "dst")
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: dst)
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: build_context(dst, full=False))
    assert _cli_impl._restore(SimpleNamespace(in_=str(tar), force=False)) == 0

    rctx = build_context(dst, full=False)
    got = rctx.docs.get("note.md")
    assert "hello world" in got["content"]
    assert (dst.vault_path / "note.md").exists()      # vault projected too


def test_snapshot_db_and_managed_files_share_one_generation(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "consistent"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    out = tmp_path / "consistent.tar"
    vacuum_complete = threading.Event()
    resume_writer = threading.Event()
    real_tar_open = tarfile.open
    worker_errors: list[BaseException] = []

    def open_after_vacuum(*args, **kwargs):
        if kwargs.get("mode", args[1] if len(args) > 1 else "r") == "w":
            vacuum_complete.set()
            if not resume_writer.wait(timeout=10):
                raise AssertionError("snapshot writer did not resume")
        return real_tar_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", open_after_vacuum)

    def snapshot() -> None:
        try:
            assert _cli_impl._snapshot(SimpleNamespace(out=str(out), force=False)) == 0
        except BaseException as exc:
            worker_errors.append(exc)

    worker = threading.Thread(target=snapshot, name="snapshot-writer")
    worker.start()
    assert vacuum_complete.wait(timeout=10), "snapshot never completed VACUUM INTO"
    try:
        src.docs.update(
            Principal(1, "ed", "editor", via="web"),
            "note.md",
            1,
            "# Note\n\nupdated after database snapshot",
            embed=False,
        )
    finally:
        resume_writer.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_errors == []
    with real_tar_open(out, "r") as archive:
        archived_db = archive.extractfile("wiki.db")
        archived_note = archive.extractfile("vault/note.md")
        assert archived_db is not None
        assert archived_note is not None
        db_copy = tmp_path / "archived.db"
        db_copy.write_bytes(archived_db.read())
        vault_body = archived_note.read().decode("utf-8")
    with sqlite3.connect(db_copy) as conn:
        db_body = conn.execute(
            "SELECT r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.path='note.md'"
        ).fetchone()[0]
    assert db_body == "# Note\n\nhello world"
    assert vault_body == db_body


def test_restore_refuses_nonempty_target_without_force(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "s2"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    tar = tmp_path / "s2.tar"
    _cli_impl._snapshot(SimpleNamespace(out=str(tar), force=False))

    # Target already has content (reuse src's own paths) -> refuse without --force.
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: src.settings)
    assert _cli_impl._restore(SimpleNamespace(in_=str(tar), force=False)) == 1


def test_snapshot_refuses_overwrite_without_force(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "s3"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    tar = tmp_path / "s3.tar"
    tar.write_text("preexisting")
    assert _cli_impl._snapshot(SimpleNamespace(out=str(tar), force=False)) == 1
    assert _cli_impl._snapshot(SimpleNamespace(out=str(tar), force=True)) == 0


def test_snapshot_retries_an_unstable_attachment(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "retry"))
    attachment = src.settings.vault_path / "asset.bin"
    attachment.write_bytes(b"stable")
    real_stage = snapshot_writer._stage_attachment_once
    attempts = 0

    def unstable_twice(path, root, staged):
        nonlocal attempts
        attempts += 1
        return None if attempts < 3 else real_stage(path, root, staged)

    monkeypatch.setattr(snapshot_writer, "_stage_attachment_once", unstable_twice)
    out = tmp_path / "retry.tar"
    report = snapshot_writer.write_snapshot(
        src.db, src.settings.vault_path, out, force=False
    )

    assert attempts == 3
    assert report.file_count == 2
    with tarfile.open(out, "r") as archive:
        archived = archive.extractfile("vault/asset.bin")
        assert archived is not None
        assert archived.read() == b"stable"


def test_snapshot_streams_large_attachment_without_path_read_bytes(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "streaming"))
    attachment = src.settings.vault_path / "large.bin"
    content = b"0123456789abcdef" * (160 * 1024)
    attachment.write_bytes(content)
    real_os_read = snapshot_writer.os.read
    read_sizes: list[int] = []

    def forbid_read_bytes(path):
        raise AssertionError(f"unbounded Path.read_bytes used for {path.name}")

    def bounded_read(fd, size):
        read_sizes.append(size)
        return real_os_read(fd, size)

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    monkeypatch.setattr(snapshot_writer.os, "read", bounded_read)
    out = tmp_path / "streaming.tar"
    snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=False)

    assert read_sizes
    assert max(read_sizes) <= 1024 * 1024
    with tarfile.open(out, "r") as archive:
        archived = archive.extractfile("vault/large.bin")
        assert archived is not None
        assert archived.read() == content


def test_snapshot_fails_after_three_unstable_attachment_reads(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "unstable"))
    (src.settings.vault_path / "asset.bin").write_bytes(b"changing")
    attempts = 0

    def always_unstable(path, root, staged):
        nonlocal attempts
        attempts += 1
        return None

    monkeypatch.setattr(snapshot_writer, "_stage_attachment_once", always_unstable)
    out = tmp_path / "unstable.tar"
    with pytest.raises(RuntimeError, match="attachment changed"):
        snapshot_writer.write_snapshot(
            src.db, src.settings.vault_path, out, force=False
        )

    assert attempts == 3
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


def test_snapshot_excludes_tmp_and_synthesizes_managed_files_without_vault(tmp_path):
    src = _seed(_settings(tmp_path, "tmp-exclusion"))
    scratch = src.settings.vault_path / ".tmp"
    scratch.mkdir(exist_ok=True)
    (scratch / "partial.bin").write_bytes(b"partial")
    (src.settings.vault_path / "kept.bin").write_bytes(b"kept")
    with_vault = tmp_path / "with-vault.tar"
    snapshot_writer.write_snapshot(
        src.db, src.settings.vault_path, with_vault, force=False
    )
    with tarfile.open(with_vault, "r") as archive:
        names = archive.getnames()
    assert "vault/kept.bin" in names
    assert not any(name.startswith("vault/.tmp/") for name in names)

    without_vault = tmp_path / "without-vault.tar"
    snapshot_writer.write_snapshot(
        src.db, tmp_path / "missing-vault", without_vault, force=False
    )
    with tarfile.open(without_vault, "r") as archive:
        note = archive.extractfile("vault/note.md")
        assert note is not None
        assert note.read().decode() == "# Note\n\nhello world"


def test_snapshot_rejects_unsafe_attachment_symlink(tmp_path):
    src = _seed(_settings(tmp_path, "unsafe"))
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    (src.settings.vault_path / "escape.bin").symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe vault path"):
        snapshot_writer.write_snapshot(
            src.db, src.settings.vault_path, tmp_path / "unsafe.tar", force=False
        )


@pytest.mark.parametrize("disable_nofollow", [False, True])
def test_snapshot_rejects_attachment_replaced_by_symlink_after_path_check(
    tmp_path, monkeypatch, disable_nofollow
):
    src = _seed(_settings(tmp_path, "symlink-race"))
    attachment = src.settings.vault_path / "race.bin"
    attachment.write_bytes(b"safe attachment")
    outside = tmp_path / "outside-secret.bin"
    secret = b"must never enter snapshot"
    outside.write_bytes(secret)
    real_resolve = Path.resolve
    swapped = False
    if disable_nofollow:
        monkeypatch.setattr(snapshot_writer.os, "O_NOFOLLOW", 0)

    def swap_after_resolve(path, *args, **kwargs):
        nonlocal swapped
        resolved = real_resolve(path, *args, **kwargs)
        if path == attachment and not swapped:
            attachment.unlink()
            attachment.symlink_to(outside)
            swapped = True
        return resolved

    monkeypatch.setattr(Path, "resolve", swap_after_resolve)
    out = tmp_path / "symlink-race.tar"
    with pytest.raises((RuntimeError, ValueError)):
        snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=False)

    assert swapped
    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


def test_snapshot_rejects_duplicate_unicode_normalized_attachment_paths(tmp_path):
    src = _seed(_settings(tmp_path, "duplicate"))
    (src.settings.vault_path / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin").write_bytes(b"one")
    (src.settings.vault_path / "cafe\N{COMBINING ACUTE ACCENT}.bin").write_bytes(b"two")

    with pytest.raises(ValueError, match="duplicate normalized snapshot path"):
        snapshot_writer.write_snapshot(
            src.db, src.settings.vault_path, tmp_path / "duplicate.tar", force=False
        )


def test_snapshot_force_preserves_existing_output_when_write_fails(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "preserve"))
    out = tmp_path / "preserve.tar"
    out.write_bytes(b"old snapshot")

    def fail_to_open(*args, **kwargs):
        raise tarfile.TarError("simulated archive failure")

    monkeypatch.setattr(snapshot_writer.tarfile, "open", fail_to_open)
    with pytest.raises(tarfile.TarError, match="simulated archive failure"):
        snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=True)

    assert out.read_bytes() == b"old snapshot"
    assert not Path(f"{out}.tmp").exists()


def test_snapshot_does_not_publish_an_archive_with_bad_file_hash(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "hash-verification"))
    (src.settings.vault_path / "asset.bin").write_bytes(b"original")
    out = tmp_path / "hash-verification.tar"
    real_add = snapshot_writer._add_staged_file

    def corrupt_attachment(tar, file):
        if file.path == "vault/asset.bin":
            file.source.write_bytes(b"corrupted")
        real_add(tar, file)

    monkeypatch.setattr(snapshot_writer, "_add_staged_file", corrupt_attachment)
    with pytest.raises(RuntimeError, match="snapshot file verification failed"):
        snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=False)

    assert not out.exists()
    assert not Path(f"{out}.tmp").exists()


def test_snapshot_manifest_describes_every_vault_file(tmp_path):
    src = _seed(_settings(tmp_path, "manifest"))
    (src.settings.vault_path / "asset.bin").write_bytes(b"x")
    out = tmp_path / "manifest.tar"
    snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=False)

    with tarfile.open(out, "r") as archive:
        manifest_file = archive.extractfile("manifest.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
    assert manifest["files"] == [
        {
            "path": "vault/note.md",
            "kind": "managed",
            "size": 19,
            "sha256": "e526b90caee6e5ac7d5ded99131859c70d82918d37185ef3d0b08e53f039a15b",
        },
        {
            "path": "vault/asset.bin",
            "kind": "attachment",
            "size": 1,
            "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        },
    ]


def test_backup_creates_valid_db_copy(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace as NS
    src = _seed(_settings(tmp_path, "bkp"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    out = tmp_path / "backup.db"
    assert _cli_impl._backup(NS(out=str(out))) == 0
    assert out.exists()
    # The copy is a real, queryable SQLite database carrying the seeded doc.
    conn = sqlite3.connect(str(out))
    try:
        n = conn.execute("SELECT COUNT(*) FROM documents WHERE is_deleted=0").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_backup_refuses_overwrite(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    src = _seed(_settings(tmp_path, "bkp2"))
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: src)
    out = tmp_path / "exists.db"
    out.write_text("preexisting")
    assert _cli_impl._backup(NS(out=str(out))) == 1   # won't clobber an existing file
    assert out.read_text() == "preexisting"


def test_restore_rejects_future_schema(tmp_path, monkeypatch):
    bad = tmp_path / "future.tar"
    with tarfile.open(bad, "w") as tar:
        manifest = {"format": "llm-wiki-snapshot", "format_version": 1,
                    "schema_version": _cli_impl.SCHEMA_VERSION + 99, "embedding_model": TEST_MODEL,
                    "doc_count": 0}
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: _settings(tmp_path, "fut"))
    assert _cli_impl._restore(SimpleNamespace(in_=str(bad), force=True)) == 1
