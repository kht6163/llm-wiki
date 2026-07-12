"""#10 single-command full snapshot/restore (DB + vault + manifest as one .tar)."""
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import _cli_impl
from llm_wiki import snapshot as snapshot_writer
from llm_wiki.config import Settings
from llm_wiki.process_lock import ProjectLock, ProjectLockError
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


def test_concurrent_snapshots_to_same_output_publish_once_without_clobber(tmp_path):
    src = _seed(_settings(tmp_path, "same-output"))
    out = tmp_path / "same-output.tar"
    start = threading.Barrier(3)
    results: list[str] = []

    def write() -> None:
        start.wait()
        try:
            snapshot_writer.write_snapshot(
                src.db, src.settings.vault_path, out, force=False
            )
        except FileExistsError:
            results.append("refused")
        else:
            results.append("published")

    workers = [threading.Thread(target=write) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=20)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(results) == ["published", "refused"]
    with tarfile.open(out, "r") as archive:
        assert archive.extractfile("wiki.db") is not None
        assert archive.extractfile("manifest.json") is not None
    assert not list(tmp_path.glob(f".{out.name}.snapshot-tmp-*"))


def test_snapshot_retries_an_unstable_attachment(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "retry"))
    attachment = src.settings.vault_path / "asset.bin"
    attachment.write_bytes(b"stable")
    real_stage = snapshot_writer._stage_attachment_once
    attempts = 0

    def unstable_twice(path, root, staged, budget):
        nonlocal attempts
        attempts += 1
        return None if attempts < 3 else real_stage(path, root, staged, budget)

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

    def always_unstable(path, root, staged, budget):
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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_snapshot_rejects_fifo_without_opening_it(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "fifo"))
    fifo = src.settings.vault_path / "blocked.bin"
    os.mkfifo(fifo)
    real_open = snapshot_writer.os.open

    def reject_fifo_open(path, flags, *args, **kwargs):
        if Path(path) == fifo:
            raise AssertionError("FIFO must be rejected before open")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(snapshot_writer.os, "open", reject_fifo_open)
    with pytest.raises(ValueError, match="regular file"):
        snapshot_writer.write_snapshot(
            src.db, src.settings.vault_path, tmp_path / "fifo.tar", force=False
        )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_snapshot_rejects_unix_socket_without_opening_it(tmp_path, monkeypatch):
    src = _seed(_settings(tmp_path, "socket"))
    socket_path = src.settings.vault_path / "blocked.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    real_open = snapshot_writer.os.open

    def reject_socket_open(path, flags, *args, **kwargs):
        if Path(path) == socket_path:
            raise AssertionError("socket must be rejected before open")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(snapshot_writer.os, "open", reject_socket_open)
    try:
        with pytest.raises(ValueError, match="regular file"):
            snapshot_writer.write_snapshot(
                src.db, src.settings.vault_path, tmp_path / "socket.tar", force=False
            )
    finally:
        listener.close()


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


def _rewrite_snapshot(src: Path, dst: Path, transform) -> None:
    with tarfile.open(src, "r") as archive:
        members = []
        for member in archive.getmembers():
            extracted = archive.extractfile(member) if member.isfile() else None
            members.append((member, extracted.read() if extracted else None))
    members = transform(members)
    with tarfile.open(dst, "w") as archive:
        for member, data in members:
            copied = tarfile.TarInfo(member.name)
            copied.type = member.type
            copied.linkname = member.linkname
            copied.size = len(data) if data is not None else 0
            archive.addfile(copied, io.BytesIO(data) if data is not None else None)


def _snapshot_for_restore(tmp_path: Path, name: str = "restore-source") -> Path:
    src = _seed(_settings(tmp_path, name))
    (src.settings.vault_path / "asset.bin").write_bytes(b"asset")
    out = tmp_path / f"{name}.tar"
    snapshot_writer.write_snapshot(src.db, src.settings.vault_path, out, force=False)
    return out


def test_restore_force_fully_replaces_targets_and_removes_sidecars(tmp_path):
    archive = _snapshot_for_restore(tmp_path)
    db_path = tmp_path / "target" / "wiki.db"
    vault = tmp_path / "target-vault"
    db_path.parent.mkdir()
    db_path.write_bytes(b"old database")
    vault.mkdir()
    (vault / "stale.md").write_text("stale")
    Path(f"{db_path}-wal").write_bytes(b"old wal")
    Path(f"{db_path}-shm").write_bytes(b"old shm")

    report = snapshot_writer.restore_snapshot(
        archive, db_path, vault, force=True
    )

    assert report.doc_count == 1
    assert (vault / "note.md").read_text() == "# Note\n\nhello world"
    assert (vault / "asset.bin").read_bytes() == b"asset"
    assert not (vault / "stale.md").exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize("field,value", [("size", 999), ("sha256", "0" * 64)])
def test_restore_rejects_manifest_content_mismatch_without_touching_targets(
    tmp_path, field, value
):
    original = _snapshot_for_restore(tmp_path, f"bad-{field}-source")
    damaged = tmp_path / f"bad-{field}.tar"

    def corrupt(members):
        result = []
        for member, data in members:
            if member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["files"][0][field] = value
                data = json.dumps(manifest).encode()
            result.append((member, data))
        return result

    _rewrite_snapshot(original, damaged, corrupt)
    db_path = tmp_path / f"target-{field}.db"
    vault = tmp_path / f"vault-{field}"
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")

    with pytest.raises(ValueError, match="manifest|verification"):
        snapshot_writer.restore_snapshot(damaged, db_path, vault, force=True)

    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"


@pytest.mark.parametrize("bad_kind", ["duplicate", "traversal", "symlink"])
def test_restore_rejects_unsafe_archive_members_without_touching_targets(
    tmp_path, bad_kind
):
    original = _snapshot_for_restore(tmp_path, f"unsafe-{bad_kind}-source")
    damaged = tmp_path / f"unsafe-{bad_kind}.tar"

    def add_bad_member(members):
        if bad_kind == "duplicate":
            member, data = next(item for item in members if item[0].name == "vault/note.md")
            members.append((member, data))
        elif bad_kind == "traversal":
            members.append((tarfile.TarInfo("vault/../escaped"), b"bad"))
        else:
            link = tarfile.TarInfo("vault/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            members.append((link, None))
        return members

    _rewrite_snapshot(original, damaged, add_bad_member)
    db_path = tmp_path / f"unsafe-{bad_kind}.db"
    vault = tmp_path / f"unsafe-{bad_kind}-vault"
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")

    with pytest.raises(ValueError, match="archive|member|duplicate|unsafe"):
        snapshot_writer.restore_snapshot(damaged, db_path, vault, force=True)

    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"


def test_restore_rejects_a_missing_manifest_payload(tmp_path):
    original = _snapshot_for_restore(tmp_path, "missing-source")
    damaged = tmp_path / "missing.tar"
    _rewrite_snapshot(
        original,
        damaged,
        lambda members: [item for item in members if item[0].name != "vault/note.md"],
    )
    db_path = tmp_path / "missing.db"
    vault = tmp_path / "missing-vault"

    with pytest.raises(ValueError, match="missing|manifest"):
        snapshot_writer.restore_snapshot(damaged, db_path, vault, force=True)
    assert not db_path.exists()
    assert not vault.exists()


def test_restore_rejects_an_archive_payload_missing_from_the_manifest(tmp_path):
    original = _snapshot_for_restore(tmp_path, "undeclared-source")
    damaged = tmp_path / "undeclared.tar"

    def add_undeclared(members):
        members.append((tarfile.TarInfo("vault/undeclared.bin"), b"undeclared"))
        return members

    _rewrite_snapshot(original, damaged, add_undeclared)

    with pytest.raises(ValueError, match="undeclared"):
        snapshot_writer.restore_snapshot(
            damaged, tmp_path / "undeclared.db", tmp_path / "undeclared-vault", force=True
        )


def test_restore_extraction_failure_leaves_live_targets_unchanged(tmp_path, monkeypatch):
    archive = _snapshot_for_restore(tmp_path, "extract-source")
    db_path = tmp_path / "extract.db"
    vault = tmp_path / "extract-vault"
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    real_copy = snapshot_writer._copy_archive_file
    calls = 0

    def fail_during_extract(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated extraction failure")
        return real_copy(source, target)

    monkeypatch.setattr(snapshot_writer, "_copy_archive_file", fail_during_extract)
    with pytest.raises(OSError, match="simulated extraction failure"):
        snapshot_writer.restore_snapshot(archive, db_path, vault, force=True)

    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"


def test_restore_second_publish_failure_rolls_back_every_live_target(
    tmp_path, monkeypatch
):
    archive = _snapshot_for_restore(tmp_path, "publish-source")
    db_path = tmp_path / "publish" / "wiki.db"
    vault = tmp_path / "publish-vault"
    db_path.parent.mkdir()
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    wal = Path(f"{db_path}-wal")
    shm = Path(f"{db_path}-shm")
    wal.write_bytes(b"original wal")
    shm.write_bytes(b"original shm")
    real_replace = snapshot_writer.os.replace

    def fail_vault_publish(source, destination):
        source_path = Path(source)
        if Path(destination) == vault and ".restore-stage-" in source_path.name:
            raise OSError("simulated vault publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(snapshot_writer.os, "replace", fail_vault_publish)
    with pytest.raises(OSError, match="simulated vault publish failure"):
        snapshot_writer.restore_snapshot(archive, db_path, vault, force=True)

    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"
    assert wal.read_bytes() == b"original wal"
    assert shm.read_bytes() == b"original shm"


def test_restore_backup_cleanup_failure_is_success_with_cli_warning(
    tmp_path, monkeypatch, capsys
):
    archive = _snapshot_for_restore(tmp_path, "cleanup-source")
    settings = _settings(tmp_path, "cleanup-target")
    settings.db_path.parent.mkdir()
    settings.db_path.write_bytes(b"original db")
    settings.vault_path.mkdir()
    (settings.vault_path / "original.txt").write_text("original")
    real_remove = snapshot_writer._remove_path
    preserved: list[Path] = []

    def fail_backup_cleanup(path):
        if ".restore-backup-" in path.name:
            preserved.append(path)
            raise OSError("simulated backup cleanup failure")
        return real_remove(path)

    monkeypatch.setattr(snapshot_writer, "_remove_path", fail_backup_cleanup)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(
        _cli_impl, "build_context", lambda **kw: build_context(settings, full=False)
    )

    assert _cli_impl._restore(SimpleNamespace(in_=str(archive), force=True)) == 0

    output = capsys.readouterr().out
    assert "WARNING: restored successfully but backup cleanup failed" in output
    assert preserved
    assert all(path.exists() for path in preserved)
    assert all(str(path) in output for path in preserved)
    assert any(path.is_file() and path.read_bytes() == b"original db" for path in preserved)
    assert any(
        path.is_dir() and (path / "original.txt").read_text() == "original"
        for path in preserved
    )
    assert (settings.vault_path / "note.md").exists()


def test_restore_report_does_not_claim_recovery_before_cli_recovery(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "report-source")
    report = snapshot_writer.restore_snapshot(
        archive, tmp_path / "report.db", tmp_path / "report-vault", force=False
    )

    assert not hasattr(report, "recovered")


@pytest.mark.parametrize("failure", ["build_context", "recover_pending"])
def test_restore_cli_rolls_back_when_post_publication_validation_fails(
    tmp_path, monkeypatch, capsys, failure
):
    archive = _snapshot_for_restore(tmp_path, f"post-publish-{failure}-source")
    settings = _settings(tmp_path, f"post-publish-{failure}-target")
    settings.db_path.parent.mkdir()
    settings.db_path.write_bytes(b"original database")
    settings.vault_path.mkdir()
    (settings.vault_path / "original.txt").write_text("original")
    original_build_context = build_context

    def fail_after_publication(*args, **kwargs):
        if failure == "build_context":
            raise RuntimeError("post-publication context failure")
        ctx = original_build_context(settings, full=False)

        def fail_recovery():
            raise RuntimeError("post-publication recovery failure")

        ctx.docs.recover_pending = fail_recovery
        return ctx

    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(_cli_impl, "build_context", fail_after_publication)

    assert _cli_impl._restore(SimpleNamespace(in_=str(archive), force=True)) == 1
    assert settings.db_path.read_bytes() == b"original database"
    assert (settings.vault_path / "original.txt").read_text() == "original"
    assert not (settings.vault_path / "note.md").exists()
    assert "restore failed:" in capsys.readouterr().out


def test_restore_handle_rolls_back_and_reports_cleanup_failures(
    tmp_path, monkeypatch
):
    archive = _snapshot_for_restore(tmp_path, "pending-rollback-source")
    db_path = tmp_path / "pending-target" / "wiki.db"
    vault = tmp_path / "pending-vault"
    db_path.parent.mkdir()
    db_path.write_bytes(b"original database")
    vault.mkdir()
    (vault / "original.txt").write_text("original")
    pending = snapshot_writer.restore_snapshot(archive, db_path, vault, force=True)
    real_remove = snapshot_writer._remove_path

    def fail_replacement_cleanup(path):
        if path == db_path:
            real_remove(path)
            raise OSError("replacement cleanup warning")
        return real_remove(path)

    monkeypatch.setattr(snapshot_writer, "_remove_path", fail_replacement_cleanup)
    with pytest.raises(snapshot_writer.RestoreRollbackError) as caught:
        pending.rollback(RuntimeError("post-publication failure"))

    assert str(caught.value.publish_error) == "post-publication failure"
    assert [str(error) for error in caught.value.rollback_errors] == [
        "replacement cleanup warning"
    ]
    assert db_path.read_bytes() == b"original database"
    assert (vault / "original.txt").read_text() == "original"


def test_post_publication_rollback_removes_replacement_sidecars_before_original_db(
    tmp_path,
):
    archive = _snapshot_for_restore(tmp_path, "sidecar-rollback-source")
    original = _seed(_settings(tmp_path, "sidecar-rollback-target"))
    db_path = original.settings.db_path
    vault = original.settings.vault_path
    original_body = original.docs.get("note.md")["content"]
    original.db.close()
    pending = snapshot_writer.restore_snapshot(archive, db_path, vault, force=True)

    Path(f"{db_path}-wal").write_bytes(b"replacement wal must not survive")
    Path(f"{db_path}-shm").write_bytes(b"replacement shm must not survive")
    with pytest.raises(RuntimeError, match="post-check failed"):
        pending.rollback(RuntimeError("post-check failed"))

    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    restored = build_context(original.settings, full=False)
    assert restored.docs.get("note.md")["content"] == original_body
    restored.db.close()


def test_restore_cli_closes_postcheck_database_before_rollback(
    tmp_path, monkeypatch
):
    archive = _snapshot_for_restore(tmp_path, "close-before-rollback-source")
    settings = _settings(tmp_path, "close-before-rollback-target")
    settings.db_path.parent.mkdir()
    settings.db_path.write_bytes(b"original")
    settings.vault_path.mkdir()
    events: list[str] = []
    real_restore = _cli_impl.restore_snapshot

    class ReportProxy:
        def __init__(self, report):
            self._report = report
            self.embedding_model = report.embedding_model

        def rollback(self, cause):
            events.append("rollback")
            return self._report.rollback(cause)

    class FailingDocs:
        def recover_pending(self):
            raise RuntimeError("recover failed")

    class DatabaseProxy:
        def close(self):
            events.append("close")

    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    monkeypatch.setattr(
        _cli_impl,
        "restore_snapshot",
        lambda *args, **kwargs: ReportProxy(real_restore(*args, **kwargs)),
    )
    monkeypatch.setattr(
        _cli_impl,
        "build_context",
        lambda **kwargs: SimpleNamespace(db=DatabaseProxy(), docs=FailingDocs()),
    )

    assert _cli_impl._restore(SimpleNamespace(in_=str(archive), force=True)) == 1
    assert events == ["close", "rollback"]


def test_restore_refuses_while_application_lock_is_held(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "locked-restore-source")
    db_path = tmp_path / "locked-target" / "wiki.db"
    vault = tmp_path / "locked-vault"

    with ProjectLock(db_path):
        with pytest.raises(ProjectLockError, match="already active"):
            snapshot_writer.restore_snapshot(archive, db_path, vault, force=False)


def test_pending_restore_holds_application_lock_until_finalize(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "pending-lock-source")
    db_path = tmp_path / "pending-lock-target" / "wiki.db"
    vault = tmp_path / "pending-lock-vault"
    pending = snapshot_writer.restore_snapshot(archive, db_path, vault, force=False)

    with pytest.raises(ProjectLockError, match="already active"):
        ProjectLock(db_path).acquire()

    pending.finalize()
    with ProjectLock(db_path):
        pass


def test_pending_restore_is_durable_and_sigkill_recovery_restores_originals(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "crash-source")
    original = _seed(_settings(tmp_path, "crash-target"))
    db_path = original.settings.db_path
    vault = original.settings.vault_path
    original.db.close()
    original_db = db_path.read_bytes()
    original_vault = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    }
    script = """
import os
import sqlite3
import sys
from pathlib import Path
from llm_wiki.snapshot import restore_snapshot

restore_snapshot(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), force=True)
connection = sqlite3.connect(sys.argv[2])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("CREATE TABLE postcheck_crash(value TEXT)")
connection.execute("INSERT INTO postcheck_crash VALUES ('replacement WAL')")
connection.commit()
os._exit(91)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(archive), str(db_path), str(vault)],
        check=False,
    )

    assert crashed.returncode == 91
    assert Path(f"{db_path}-wal").exists()
    assert Path(f"{db_path}-shm").exists()
    journal = snapshot_writer.restore_journal_path(db_path)
    assert journal.exists()
    assert snapshot_writer.recover_pending_restore(db_path, vault) == "rolled_back"
    assert not journal.exists()
    assert db_path.read_bytes() == original_db
    assert {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file()
    } == original_vault
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_finalize_marks_durable_commit_and_removes_journal(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "durable-finalize-source")
    db_path = tmp_path / "durable-finalize" / "wiki.db"
    vault = tmp_path / "durable-finalize-vault"
    pending = snapshot_writer.restore_snapshot(archive, db_path, vault, force=False)
    journal = snapshot_writer.restore_journal_path(db_path)

    assert journal.exists()
    pending.finalize()

    assert not journal.exists()


def test_serve_startup_recovers_pending_restore_before_building_context(
    tmp_path, monkeypatch
):
    archive = _snapshot_for_restore(tmp_path, "serve-recovery-source")
    original = _seed(_settings(tmp_path, "serve-recovery-target"))
    original_body = original.docs.get("note.md")["content"]
    original.db.close()
    pending = snapshot_writer.restore_snapshot(
        archive,
        original.settings.db_path,
        original.settings.vault_path,
        force=True,
    )
    assert pending._journal is not None
    pending._journal.process_lock.release()
    observed = []

    def serve_locked(args, settings):
        ctx = build_context(settings, full=False)
        observed.append(ctx.docs.get("note.md")["content"])
        ctx.db.close()
        return 0

    monkeypatch.setattr(_cli_impl, "get_settings", lambda: original.settings)
    monkeypatch.setattr(_cli_impl, "_serve_locked", serve_locked)

    assert _cli_impl._serve(
        SimpleNamespace(host=None, gui_port=None, mcp_port=None)
    ) == 0
    assert observed == [original_body]


def test_restore_startup_recovers_previous_pending_journal(tmp_path):
    first_archive = _snapshot_for_restore(tmp_path, "first-interrupted-source")
    second_archive = _snapshot_for_restore(tmp_path, "second-after-recovery-source")
    original = _seed(_settings(tmp_path, "restore-recovery-target"))
    original.db.close()
    pending = snapshot_writer.restore_snapshot(
        first_archive,
        original.settings.db_path,
        original.settings.vault_path,
        force=True,
    )
    assert pending._journal is not None
    pending._journal.process_lock.release()

    replacement = snapshot_writer.restore_snapshot(
        second_archive,
        original.settings.db_path,
        original.settings.vault_path,
        force=True,
    )
    replacement.finalize()

    assert not snapshot_writer.restore_journal_path(original.settings.db_path).exists()
    assert not list(
        original.settings.db_path.parent.glob(".*.restore-backup-*")
    )
    assert not list(original.settings.vault_path.parent.glob(".*.restore-backup-*"))


def test_restore_refuses_lock_held_by_another_process(tmp_path):
    archive = _snapshot_for_restore(tmp_path, "process-lock-source")
    db_path = tmp_path / "process-lock-target" / "wiki.db"
    vault = tmp_path / "process-lock-vault"
    script = """
import sys
import time
from pathlib import Path
from llm_wiki.process_lock import ProjectLock

with ProjectLock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(30)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(ProjectLockError, match="already active"):
            snapshot_writer.restore_snapshot(archive, db_path, vault, force=False)
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_restore_cli_reports_preserved_backup_when_rollback_fails(
    tmp_path, monkeypatch, capsys
):
    archive = _snapshot_for_restore(tmp_path, "rollback-source")
    settings = _settings(tmp_path, "rollback-target")
    settings.db_path.parent.mkdir()
    settings.db_path.write_bytes(b"original db")
    settings.vault_path.mkdir()
    (settings.vault_path / "original.txt").write_text("original")
    real_replace = snapshot_writer.os.replace
    real_remove = snapshot_writer._remove_path
    preserved: list[Path] = []
    rollback_errors: list[snapshot_writer.RestoreRollbackError] = []

    def fail_publish_and_db_rollback(source, destination):
        source_path, destination_path = Path(source), Path(destination)
        if (
            destination_path == settings.vault_path
            and ".restore-stage-" in source_path.name
        ):
            raise OSError("simulated vault publish failure")
        if (
            destination_path == settings.db_path
            and ".restore-backup-" in source_path.name
        ):
            preserved.append(source_path)
            raise OSError("simulated database rollback failure")
        return real_replace(source, destination)

    monkeypatch.setattr(snapshot_writer.os, "replace", fail_publish_and_db_rollback)

    def fail_staging_cleanup(path):
        if ".restore-stage-" in path.name and path.exists():
            raise OSError("simulated staging cleanup failure")
        return real_remove(path)

    monkeypatch.setattr(snapshot_writer, "_remove_path", fail_staging_cleanup)
    monkeypatch.setattr(_cli_impl, "get_settings", lambda: settings)
    real_restore = _cli_impl.restore_snapshot

    def record_rollback_error(*args, **kwargs):
        try:
            return real_restore(*args, **kwargs)
        except snapshot_writer.RestoreRollbackError as exc:
            rollback_errors.append(exc)
            raise

    monkeypatch.setattr(_cli_impl, "restore_snapshot", record_rollback_error)

    assert _cli_impl._restore(SimpleNamespace(in_=str(archive), force=True)) == 1

    output = capsys.readouterr().out
    assert "restore failed:" in output
    assert "backups preserved at" in output
    assert preserved and preserved[0].exists()
    assert str(preserved[0]) in output
    assert (settings.vault_path / "original.txt").read_text() == "original"
    assert rollback_errors[0].backup_paths == (preserved[0],)
    assert str(rollback_errors[0].publish_error) == "simulated vault publish failure"
    assert str(rollback_errors[0].rollback_errors[0]) == "simulated database rollback failure"
    assert str(rollback_errors[0].staging_cleanup_errors[0]) == (
        "simulated staging cleanup failure"
    )


@pytest.mark.parametrize("corruption", ["database", "managed"])
def test_restore_rejects_staged_database_projection_mismatch_without_live_changes(
    tmp_path, corruption
):
    original = _snapshot_for_restore(tmp_path, f"projection-{corruption}-source")
    damaged = tmp_path / f"projection-{corruption}.tar"
    scratch_db = tmp_path / f"projection-{corruption}.db"

    def corrupt_projection(members):
        tampered_database = b""
        result = []
        for member, data in members:
            if corruption == "database" and member.name == "wiki.db":
                scratch_db.write_bytes(data)
                with sqlite3.connect(scratch_db) as conn:
                    conn.execute(
                        "UPDATE revisions SET body=? WHERE version=1",
                        ("# Note\n\ntampered database",),
                    )
                data = scratch_db.read_bytes()
                tampered_database = data
            elif corruption == "database" and member.name == "manifest.json":
                manifest = json.loads(data)
                manifest["database"] = {
                    "size": len(tampered_database),
                    "sha256": hashlib.sha256(tampered_database).hexdigest(),
                }
                data = json.dumps(manifest).encode()
            elif corruption == "managed" and member.name == "vault/note.md":
                data = b"# Note\n\ntampered projection"
            elif corruption == "managed" and member.name == "manifest.json":
                manifest = json.loads(data)
                entry = next(
                    item for item in manifest["files"] if item["path"] == "vault/note.md"
                )
                body = b"# Note\n\ntampered projection"
                entry["size"] = len(body)
                entry["sha256"] = hashlib.sha256(body).hexdigest()
                data = json.dumps(manifest).encode()
            result.append((member, data))
        return result

    _rewrite_snapshot(original, damaged, corrupt_projection)
    db_path = tmp_path / f"live-{corruption}.db"
    vault = tmp_path / f"live-{corruption}-vault"
    db_path.write_bytes(b"original db")
    vault.mkdir()
    (vault / "original.txt").write_text("original")

    with pytest.raises(ValueError, match="hash mismatch|document mismatch"):
        snapshot_writer.restore_snapshot(damaged, db_path, vault, force=True)

    assert db_path.read_bytes() == b"original db"
    assert (vault / "original.txt").read_text() == "original"
