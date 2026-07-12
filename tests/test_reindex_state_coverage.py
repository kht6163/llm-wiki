"""Public reindex state-machine coverage with synchronized real mutations."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.util import path_norm, sha256_hex

_EVENT_TIMEOUT = 5.0


def _doc(ctx, rel: str):
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id,path,version,content_hash,file_state,is_deleted "
            "FROM documents WHERE path_norm=?",
            (path_norm(rel),),
        ).fetchone()
    assert row is not None
    return row


def _start_worker(
    action: Callable[[], None],
) -> tuple[threading.Event, threading.Event, list[BaseException], threading.Thread]:
    start = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        if not start.wait(_EVENT_TIMEOUT):
            errors.append(AssertionError("reindex synchronization point was not reached"))
            return
        try:
            action()
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return start, finished, errors, thread


def _finish_worker(thread: threading.Thread, errors: list[BaseException]) -> None:
    thread.join(_EVENT_TIMEOUT)
    assert not thread.is_alive(), "synchronized mutation did not terminate"
    if errors:
        raise errors[0]


def _assert_database_connections_released(ctx) -> None:
    """A leaked worker connection would retain the writer lock and fail this probe."""
    with ctx.db.writer() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def _latest_revision(ctx, doc_id: int):
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT version,body,op FROM revisions WHERE doc_id=? ORDER BY version DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
    assert row is not None
    return (int(row["version"]), str(row["body"]), str(row["op"]))


def test_empty_vault_reindex_finishes_without_entering_a_path_attempt(ctx):
    report = ctx.docs.reindex_all()

    assert report == {
        "created": 0,
        "updated": 0,
        "renamed": 0,
        "unchanged": 0,
        "renames": [],
        "retried": 0,
        "recovered_pending": 0,
        "missing_files": [],
        "skipped_deleted": [],
        "skipped_conflicts": [],
        "embedded": 0,
    }
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_postcommit_same_hash_peer_creation_preserves_rename_and_reports_identity_change(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\npeer set changes after commit"
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = int(_doc(ctx, "a-source.md")["id"])
    os.replace(docs.vault / "a-source.md", docs.vault / "z-target.md")

    start, finished, errors, thread = _start_worker(
        lambda: docs.create(editor, "m-new-peer.md", body, embed=False)
    )
    real_is_current = fp.stable_markdown_is_current
    real_writer = ctx.db.writer
    writer_active = threading.Event()
    synchronized = False

    @contextmanager
    def tracked_writer():
        with real_writer() as conn:
            writer_active.set()
            try:
                yield conn
            finally:
                writer_active.clear()

    def create_peer_after_rename_commit(stable: fp.StableMarkdown) -> bool:
        nonlocal synchronized
        current = real_is_current(stable)
        if stable.relative_path != "z-target.md" or synchronized or writer_active.is_set():
            return current
        with ctx.db.reader() as conn:
            renamed = conn.execute(
                "SELECT path,content_hash FROM documents WHERE id=?", (source_id,)
            ).fetchone()
        if (
            renamed is not None
            and renamed["path"] == "z-target.md"
            and renamed["content_hash"] == sha256_hex(stable.text)
        ):
            synchronized = True
            start.set()
            assert finished.wait(_EVENT_TIMEOUT), "peer creation did not finish"
        return current

    monkeypatch.setattr(ctx.db, "writer", tracked_writer)
    monkeypatch.setattr(fp, "stable_markdown_is_current", create_peer_after_rename_commit)

    try:
        report = docs.reindex_all()
    finally:
        start.set()
        _finish_worker(thread, errors)
    _assert_database_connections_released(ctx)
    assert synchronized
    assert report["renamed"] == 1
    assert report["renames"] == ["a-source.md -> z-target.md"]
    assert report["skipped_conflicts"] == [
        {
            "path": "z-target.md",
            "reason": "rename_source_changed",
            "attempts": 1,
        }
    ]
    renamed = _doc(ctx, "z-target.md")
    peer = _doc(ctx, "m-new-peer.md")
    assert (
        int(renamed["id"]),
        renamed["version"],
        renamed["file_state"],
        renamed["is_deleted"],
    ) == (source_id, 2, "clean", 0)
    assert (peer["version"], peer["file_state"], peer["is_deleted"]) == (1, "clean", 0)
    assert _latest_revision(ctx, source_id) == (2, body, "rename")
    assert _latest_revision(ctx, int(peer["id"])) == (1, body, "create")
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM documents WHERE path_norm=?", (path_norm("a-source.md"),)
            ).fetchone()
            is None
        )
    assert not (docs.vault / "a-source.md").exists()
    assert docs.get("m-new-peer.md")["content"] == body
    assert (docs.vault / "m-new-peer.md").read_text(encoding="utf-8") == body


def test_pending_same_hash_source_identity_change_retries_before_target_adoption(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# Shared generation\n\npending source changes identity"
    source = docs.vault / "z-source.md"
    target = docs.vault / "a-target.md"
    docs.create(editor, "z-source.md", body, embed=False)
    source_id = int(_doc(ctx, "z-source.md")["id"])
    target.write_text(body, encoding="utf-8")
    real_read = fp.read_stable_markdown
    real_absence = fp.confirm_confined_absence

    def make_source_pending_and_absent() -> None:
        source.unlink()
        with ctx.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (source_id,))

    pending_start, pending_done, pending_errors, pending_thread = _start_worker(
        make_source_pending_and_absent
    )
    recreate_start, recreate_done, recreate_errors, recreate_thread = _start_worker(
        lambda: source.write_text(body, encoding="utf-8")
    )
    pending_injected = False
    source_recreated = False

    def inject_pending_after_target_read(vault: Path, path: Path):
        nonlocal pending_injected
        stable = real_read(vault, path)
        if Path(path) == target and not pending_injected:
            pending_injected = True
            pending_start.set()
            assert pending_done.wait(_EVENT_TIMEOUT), "pending source mutation did not finish"
        return stable

    def recreate_after_first_absence(vault: Path, path: Path) -> bool:
        nonlocal source_recreated
        absent = real_absence(vault, path)
        if Path(path) == source and absent and not source_recreated:
            source_recreated = True
            recreate_start.set()
            assert recreate_done.wait(_EVENT_TIMEOUT), "source recreation did not finish"
        return absent

    monkeypatch.setattr(fp, "read_stable_markdown", inject_pending_after_target_read)
    monkeypatch.setattr(fp, "confirm_confined_absence", recreate_after_first_absence)

    try:
        report = docs.reindex_all()
    finally:
        pending_start.set()
        recreate_start.set()
        _finish_worker(pending_thread, pending_errors)
        _finish_worker(recreate_thread, recreate_errors)
    _assert_database_connections_released(ctx)
    assert pending_injected and source_recreated
    assert report["retried"] == 1
    assert report["created"] == 1
    assert report["unchanged"] == 1
    assert report["recovered_pending"] == 1
    assert report["skipped_conflicts"] == []
    adopted = _doc(ctx, "a-target.md")
    settled_source = _doc(ctx, "z-source.md")
    assert int(adopted["id"]) != source_id
    assert (settled_source["file_state"], settled_source["version"]) == ("clean", 1)
    assert docs.get("a-target.md")["content"] == body
    assert source.read_text(encoding="utf-8") == body


def test_reconcile_adopts_replacement_but_reports_failed_cleanup_owner_recovery(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "z-owner.md", "canonical owner", embed=False)
    owner_id = int(_doc(ctx, "z-owner.md")["id"])
    replacement = docs.vault / "a-replacement.md"
    replacement.write_text("external replacement", encoding="utf-8")
    real_signature = fp.confined_file_signature

    def install_pending_cleanup() -> None:
        owner_path = docs.vault / "z-owner.md"
        owner_path.unlink()
        owner_path.mkdir()
        with ctx.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (owner_id,))
            conn.execute(
                "INSERT INTO file_projection_cleanup("
                "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                "VALUES(?,?,?,0,NULL,NULL,NULL,NULL,NULL,1,'now')",
                (owner_id, "a-replacement.md", path_norm("a-replacement.md")),
            )

    start, finished, errors, thread = _start_worker(install_pending_cleanup)
    synchronized = False

    def mutate_owner_during_scan(vault, path, *, missing_ok=False):
        nonlocal synchronized
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == replacement and not missing_ok and not synchronized:
            synchronized = True
            start.set()
            assert finished.wait(_EVENT_TIMEOUT), "cleanup mutation did not finish"
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", mutate_owner_during_scan)

    try:
        report = docs.reindex_all()
    finally:
        start.set()
        _finish_worker(thread, errors)
    _assert_database_connections_released(ctx)
    assert synchronized
    assert report["created"] == 1
    assert report["skipped_conflicts"] == [
        {
            "path": "z-owner.md",
            "reason": "pending_projection",
            "attempts": 1,
        }
    ]
    adopted = _doc(ctx, "a-replacement.md")
    owner = _doc(ctx, "z-owner.md")
    assert (adopted["version"], adopted["file_state"], adopted["is_deleted"]) == (
        1,
        "clean",
        0,
    )
    assert (owner["version"], owner["file_state"], owner["is_deleted"]) == (
        1,
        "pending",
        0,
    )
    assert _latest_revision(ctx, int(adopted["id"])) == (
        1,
        "external replacement",
        "external-reconcile",
    )
    assert _latest_revision(ctx, owner_id) == (1, "canonical owner", "create")
    assert docs.get("z-owner.md")["content"] == "canonical owner"
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (owner_id,)
            ).fetchone()[0]
            == 0
        )
    assert (docs.vault / "a-replacement.md").read_text(encoding="utf-8") == ("external replacement")
    assert (docs.vault / "z-owner.md").is_dir()


def test_three_postcommit_generation_changes_keep_best_update_and_report_latest_file(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    rel = "postcommit-race.md"
    target = docs.vault / rel
    docs.create(editor, rel, "managed generation", embed=False)
    target.write_text("external generation 1", encoding="utf-8")
    real_writer = ctx.db.writer
    writer_active = threading.Event()
    real_is_current = fp.stable_markdown_is_current
    triggers = {version: threading.Event() for version in (2, 3, 4)}
    finished = {version: threading.Event() for version in (2, 3, 4)}
    errors: list[BaseException] = []

    @contextmanager
    def tracked_writer():
        with real_writer() as conn:
            writer_active.set()
            try:
                yield conn
            finally:
                writer_active.clear()

    def replace_after_each_commit() -> None:
        try:
            for version in (2, 3, 4):
                if not triggers[version].wait(_EVENT_TIMEOUT):
                    raise AssertionError(f"reindex did not commit version {version}")
                replacement = target.with_name(f".{target.name}.{version}.swap")
                replacement.write_text(f"external generation {version}", encoding="utf-8")
                os.replace(replacement, target)
                finished[version].set()
        except BaseException as exc:  # pragma: no cover - re-raised in test thread
            errors.append(exc)
            for event in finished.values():
                event.set()

    thread = threading.Thread(target=replace_after_each_commit, daemon=True)
    thread.start()

    def mutate_only_after_committed_generation(stable: fp.StableMarkdown) -> bool:
        current = real_is_current(stable)
        if stable.relative_path != rel or writer_active.is_set():
            return current
        row = _doc(ctx, rel)
        version = int(row["version"])
        if (
            version in triggers
            and row["content_hash"] == sha256_hex(stable.text)
            and not triggers[version].is_set()
        ):
            triggers[version].set()
            assert finished[version].wait(_EVENT_TIMEOUT), (
                f"external replacement after version {version} did not finish"
            )
            return real_is_current(stable)
        return current

    monkeypatch.setattr(ctx.db, "writer", tracked_writer)
    monkeypatch.setattr(fp, "stable_markdown_is_current", mutate_only_after_committed_generation)

    try:
        report = docs.reindex_all()
    finally:
        for event in triggers.values():
            event.set()
        _finish_worker(thread, errors)
    _assert_database_connections_released(ctx)
    assert report["updated"] == 1
    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [{"path": rel, "reason": "file_changed", "attempts": 3}]
    current = _doc(ctx, rel)
    assert (current["version"], current["file_state"]) == (4, "clean")
    assert docs.get(rel)["content"] == "external generation 3"
    assert target.read_text(encoding="utf-8") == "external generation 4"


def test_postcommit_rename_source_becoming_symlink_reports_unreadable_source(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "# External rename\n\nsource becomes unsafe after commit"
    old = docs.vault / "old.md"
    target = docs.vault / "new.md"
    outside = docs.vault.parent / "outside.md"
    outside.write_text(body, encoding="utf-8")
    docs.create(editor, "old.md", body, embed=False)
    source_id = int(_doc(ctx, "old.md")["id"])
    os.replace(old, target)
    real_writer = ctx.db.writer
    writer_active = threading.Event()
    real_is_current = fp.stable_markdown_is_current

    @contextmanager
    def tracked_writer():
        with real_writer() as conn:
            writer_active.set()
            try:
                yield conn
            finally:
                writer_active.clear()

    start, finished, errors, thread = _start_worker(lambda: old.symlink_to(outside))
    synchronized = False

    def install_symlink_after_rename(stable: fp.StableMarkdown) -> bool:
        nonlocal synchronized
        current = real_is_current(stable)
        if stable.relative_path != "new.md" or synchronized or writer_active.is_set():
            return current
        if _doc(ctx, "new.md")["id"] == source_id:
            synchronized = True
            start.set()
            assert finished.wait(_EVENT_TIMEOUT), "source symlink creation did not finish"
        return current

    monkeypatch.setattr(ctx.db, "writer", tracked_writer)
    monkeypatch.setattr(fp, "stable_markdown_is_current", install_symlink_after_rename)

    try:
        report = docs.reindex_all()
    finally:
        start.set()
        _finish_worker(thread, errors)
    _assert_database_connections_released(ctx)
    assert synchronized
    assert report["renamed"] == 1
    assert report["renames"] == ["old.md -> new.md"]
    assert report["skipped_conflicts"] == [
        {"path": "old.md", "reason": "file_unreadable", "attempts": 3}
    ]
    current = _doc(ctx, "new.md")
    assert (
        int(current["id"]),
        int(current["version"]),
        current["file_state"],
        current["is_deleted"],
    ) == (source_id, 2, "clean", 0)
    assert _latest_revision(ctx, source_id) == (2, body, "rename")
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM documents WHERE path_norm=?", (path_norm("old.md"),)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (source_id,)
            ).fetchone()[0]
            == 0
        )
    assert target.read_text(encoding="utf-8") == body
    assert old.is_symlink()


def test_final_missing_sweep_bounds_repeated_managed_updates_as_target_changed(
    ctx, principals, monkeypatch: pytest.MonkeyPatch
):
    docs, editor = ctx.docs, principals["editor"]
    rel = "final-race.md"
    target = docs.vault / rel
    docs.create(editor, rel, "managed generation 1", embed=False)
    target.unlink()
    real_signature = fp.confined_file_signature
    triggers = {version: threading.Event() for version in (1, 2, 3)}
    finished = {version: threading.Event() for version in (1, 2, 3)}
    mutation_active = threading.Event()
    errors: list[BaseException] = []

    def mutate_each_snapshot() -> None:
        try:
            for version in (1, 2, 3):
                if not triggers[version].wait(_EVENT_TIMEOUT):
                    raise AssertionError(f"final sweep did not inspect version {version}")
                mutation_active.set()
                docs.update(
                    editor,
                    rel,
                    version,
                    f"managed generation {version + 1}",
                    embed=False,
                )
                mutation_active.clear()
                finished[version].set()
        except BaseException as exc:  # pragma: no cover - re-raised in the test thread
            errors.append(exc)
            mutation_active.clear()
            for event in finished.values():
                event.set()

    thread = threading.Thread(target=mutate_each_snapshot, daemon=True)
    thread.start()

    def update_after_final_signature(vault, path, *, missing_ok=False):
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) != target or not missing_ok or mutation_active.is_set():
            return signature
        version = int(_doc(ctx, rel)["version"])
        if version in triggers and not triggers[version].is_set():
            triggers[version].set()
            assert finished[version].wait(_EVENT_TIMEOUT), (
                f"managed update for version {version} did not finish"
            )
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", update_after_final_signature)

    try:
        report = docs.reindex_all()
    finally:
        for event in triggers.values():
            event.set()
        _finish_worker(thread, errors)
    _assert_database_connections_released(ctx)
    assert report["missing_files"] == []
    assert report["skipped_conflicts"] == [{"path": rel, "reason": "target_changed", "attempts": 3}]
    current = _doc(ctx, rel)
    assert (current["version"], current["file_state"], current["is_deleted"]) == (
        4,
        "clean",
        0,
    )
    assert docs.get(rel)["content"] == "managed generation 4"
    assert target.read_text(encoding="utf-8") == "managed generation 4"
    with ctx.db.reader() as conn:
        revisions = conn.execute(
            "SELECT version,op FROM revisions WHERE doc_id=? ORDER BY version",
            (current["id"],),
        ).fetchall()
    assert [(row["version"], row["op"]) for row in revisions] == [
        (1, "create"),
        (2, "edit"),
        (3, "edit"),
        (4, "edit"),
    ]
