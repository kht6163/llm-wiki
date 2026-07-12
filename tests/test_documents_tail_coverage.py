from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.services.documents import ProjectionPendingError
from llm_wiki.services.errors import ConflictError, NotFoundError, ValidationError
from llm_wiki.util import path_norm


def _write(root: Path, rel: str, content: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _queue_absent_cleanup(ctx, doc_id: int, rel: str, *, pending: bool = True) -> None:
    with ctx.db.writer() as conn:
        if pending:
            conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
        conn.execute(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
            "VALUES(?,?,?,0,NULL,NULL,NULL,NULL,NULL,"
            "(SELECT version FROM documents WHERE id=?),'now')",
            (doc_id, rel, path_norm(rel), doc_id),
        )


def _run_recovery(docs, ctx, results: list[int], errors: list[BaseException]) -> None:
    try:
        results.append(docs.recover_pending())
    except BaseException as exc:
        errors.append(exc)
    finally:
        ctx.db.close()


def _run_call(action, ctx, results: list[dict], errors: list[BaseException]) -> None:
    try:
        results.append(action())
    except BaseException as exc:
        errors.append(exc)
    finally:
        ctx.db.close()


def test_delete_folder_succeeds_when_projected_directory_already_disappeared(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create_folder(editor, "gone")
    os.rmdir(docs.vault / "gone")

    result = docs.delete_folder(editor, "gone")

    assert result == {"ok": True, "path": "gone", "deleted": True}
    assert docs.list_folders() == []


def test_delete_folder_prunes_multiple_empty_projected_levels(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create_folder(editor, "empty/a/b")

    result = docs.delete_folder(editor, "empty")

    assert result["deleted"] is True
    assert not (docs.vault / "empty").exists()


def test_llms_index_rejects_a_document_without_its_current_revision(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "corrupt-corpus.md", "body", embed=False)
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM revisions WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?) "
            "AND version=?",
            (path_norm("corrupt-corpus.md"), created["version"]),
        )

    with pytest.raises(RuntimeError, match="Corpus metadata and body cursors lost alignment"):
        docs.llms_index(site_title="Wiki")


def test_llms_index_omits_description_when_body_contains_only_a_heading(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "heading-only.md", "# Heading", embed=False)

    index = docs.llms_index(site_title="Wiki")

    assert "- [Heading](/doc/heading-only.md/raw)" in index
    assert "heading-only.md) —" not in index


def test_llms_index_skips_marker_only_lines_before_description(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "marker.md", "# Heading\n\n>\n\nactual prose", embed=False)

    index = docs.llms_index(site_title="Wiki")

    assert "marker.md/raw): actual prose" in index


def test_append_under_final_section_preserves_a_line_boundary(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "append.md", "# Log\n\n## Notes\nlast line", embed=False)

    result = docs.append_to_document(editor, "append.md", "next line", ensure_heading="Notes")

    assert result["content"] == "# Log\n\n## Notes\nlast line\nnext line\n"


def test_concurrent_append_with_same_key_replays_the_committed_result(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "idempotent.md", "# Log\n", embed=False)
    real_update = docs.update
    ready = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def synchronized_update(*args, **kwargs):
        ready.wait(timeout=5)
        return real_update(*args, **kwargs)

    def append() -> None:
        try:
            results.append(
                docs.append_to_document(editor, "idempotent.md", "entry", idempotency_key="same")
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            ctx.db.close()

    monkeypatch.setattr(docs, "update", synchronized_update)
    threads = [threading.Thread(target=append) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert {result["version"] for result in results} == {2}
    assert sum(bool(result.get("deduplicated")) for result in results) == 1
    assert docs.get("idempotent.md")["content"].count("entry") == 1


def test_append_conflict_without_a_matching_key_is_not_suppressed(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "stale-append.md", "body", embed=False)
    docs.update(editor, "stale-append.md", created["version"], "new body", embed=False)

    with pytest.raises(ConflictError):
        docs.append_to_document(
            editor,
            "stale-append.md",
            "entry",
            base_version=created["version"],
            idempotency_key="new-key",
        )


def test_two_public_recoveries_settle_the_same_pending_generation_once(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "pending.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "pending.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
    staged = threading.Barrier(2)
    real_stage = fp.stage_text

    def synchronize_stage(vault, target, body):
        value = real_stage(vault, target, body)
        if threading.current_thread().name.startswith("settle-"):
            staged.wait(timeout=5)
        return value

    monkeypatch.setattr(fp, "stage_text", synchronize_stage)
    results: list[int] = []
    errors: list[BaseException] = []
    threads = [
        threading.Thread(
            target=_run_recovery,
            args=(docs, ctx, results, errors),
            name=f"settle-{index}",
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results) == [0, 1]
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT file_state FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
            == "clean"
        )


def test_public_purge_removes_generation_while_recovery_is_staged(ctx, principals, monkeypatch):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "staged-purge.md", "canonical", embed=False)
    docs.delete(editor, "staged-purge.md")
    doc_id = _doc_id(ctx, "staged-purge.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
    staged = threading.Event()
    release = threading.Event()
    real_stage = fp.stage_text

    def pause_recovery_stage(vault, target, body):
        value = real_stage(vault, target, body)
        if threading.current_thread().name == "staged-recovery":
            staged.set()
            assert release.wait(timeout=10)
        return value

    monkeypatch.setattr(fp, "stage_text", pause_recovery_stage)
    results: list[int] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_run_recovery,
        args=(docs, ctx, results, errors),
        name="staged-recovery",
    )
    thread.start()
    assert staged.wait(timeout=5)
    docs.purge(admin, "staged-purge.md")
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert results == [0]
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone() is None


@pytest.mark.parametrize("outcome", ["settled", "missing", "purge_pending"])
def test_cleanup_phase_revalidates_public_concurrent_outcome(ctx, principals, monkeypatch, outcome):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "cleanup-race.md", "canonical", embed=False)
    if outcome != "settled":
        docs.delete(editor, "cleanup-race.md")
    doc_id = _doc_id(ctx, "cleanup-race.md")
    _queue_absent_cleanup(ctx, doc_id, "retired.md")
    real_writer = ctx.db.writer
    initial_committed = threading.Event()
    release = threading.Event()
    paused = False

    @contextmanager
    def pause_after_initial_projection():
        nonlocal paused
        with real_writer() as conn:
            yield conn
        if threading.current_thread().name == "cleanup-recovery" and not paused:
            paused = True
            initial_committed.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(ctx.db, "writer", pause_after_initial_projection)
    results: list[int] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_run_recovery,
        args=(docs, ctx, results, errors),
        name="cleanup-recovery",
    )
    thread.start()
    assert initial_committed.wait(timeout=5)
    if outcome == "settled":
        assert docs.recover_pending() == 1
    elif outcome == "missing":
        docs.purge(admin, "cleanup-race.md")
    else:
        real_unlink = fp.unlink_regular

        def fail_purge_unlink(path, *, expected=None, vault=None):
            if threading.current_thread().name == "MainThread" and expected is not None:
                raise OSError("injected purge failure")
            return real_unlink(path, expected=expected, vault=vault)

        monkeypatch.setattr(fp, "unlink_regular", fail_purge_unlink)
        with pytest.raises(ProjectionPendingError):
            docs.purge(admin, "cleanup-race.md")
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert results == [1 if outcome == "purge_pending" else 0]
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT file_state FROM documents WHERE id=?", (doc_id,)).fetchone()
        intent = conn.execute(
            "SELECT 1 FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()
    if outcome == "settled":
        assert row["file_state"] == "clean" and intent is None
    elif outcome == "missing":
        assert row is None and intent is None
    else:
        assert row is None and intent is None


@pytest.mark.parametrize("outcome", ["settled", "missing"])
def test_final_projection_revalidates_public_concurrent_outcome(
    ctx, principals, monkeypatch, outcome
):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "final-race.md", "canonical", embed=False)
    if outcome == "missing":
        docs.delete(editor, "final-race.md")
    doc_id = _doc_id(ctx, "final-race.md")
    _queue_absent_cleanup(ctx, doc_id, "retired.md")
    final_staged = threading.Event()
    release = threading.Event()
    real_stage = fp.stage_text

    def pause_final_stage(vault, target, body):
        value = real_stage(vault, target, body)
        if threading.current_thread().name == "final-recovery":
            with ctx.db.reader() as conn:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?",
                    (doc_id,),
                ).fetchone()[0]
            if remaining == 0:
                final_staged.set()
                assert release.wait(timeout=10)
        return value

    monkeypatch.setattr(fp, "stage_text", pause_final_stage)
    results: list[int] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_run_recovery,
        args=(docs, ctx, results, errors),
        name="final-recovery",
    )
    thread.start()
    assert final_staged.wait(timeout=5)
    if outcome == "settled":
        assert docs.recover_pending() == 1
    else:
        docs.purge(admin, "final-race.md")
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert results == [0]
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT file_state FROM documents WHERE id=?", (doc_id,)).fetchone()
    if outcome == "settled":
        assert row["file_state"] == "clean"
    else:
        assert row is None


def test_final_projection_install_failure_remains_recoverable(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "final-install.md", "canonical", embed=False)
    doc_id = _doc_id(ctx, "final-install.md")
    _queue_absent_cleanup(ctx, doc_id, "retired.md")
    real_install = fp.install_staged

    def fail_final_install(staged, target):
        with ctx.db.reader() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]
        if remaining == 0:
            raise OSError("injected final install failure")
        return real_install(staged, target)

    monkeypatch.setattr(fp, "install_staged", fail_final_install)

    assert docs.recover_pending() == 0
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT file_state FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
            == "pending"
        )


def test_merge_tags_reports_a_concurrent_noop_without_a_version_bump(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "tags.md", "body", tags=["old"], embed=False)
    real_reader = ctx.db.reader
    removed = False

    @contextmanager
    def remove_source_after_candidate_read():
        nonlocal removed
        with real_reader() as conn:
            yield conn
        if not removed:
            removed = True
            docs.patch_tags(editor, "tags.md", add=["new"], remove=["old"])

    monkeypatch.setattr(ctx.db, "reader", remove_source_after_candidate_read)

    result = docs.merge_tags(editor, ["old"], "new")

    assert removed
    assert result["docs_affected"] == 1
    assert result["docs_changed"] == 0
    assert docs.get("tags.md")["tags"] == ["new"]


def test_rename_references_skips_candidate_deleted_after_candidate_read(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "source.md", "[[old/path.md]]", embed=False)
    real_reader = ctx.db.reader
    deleted = False

    @contextmanager
    def delete_after_candidate_read():
        nonlocal deleted
        with real_reader() as conn:
            yield conn
        if not deleted:
            deleted = True
            docs.delete(editor, "source.md")

    monkeypatch.setattr(ctx.db, "reader", delete_after_candidate_read)

    result = docs.rename_references(editor, "old/path.md", "new/path.md")

    assert deleted
    assert result["docs_rewritten"] == 0
    assert docs.list_deleted()[0]["path"] == "source.md"


def test_rename_references_ignores_invalid_and_unrelated_links(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(
        editor,
        "source.md",
        "[[old/path.md]] [[../invalid]] [[other.md]]",
        embed=False,
    )

    result = docs.rename_references(editor, "old/path.md", "new/path.md")

    assert result["links_rewritten"] == 1
    assert docs.get("source.md")["content"] == ("[[new/path.md]] [[../invalid]] [[other.md]]")


def test_rename_references_skips_candidate_changed_after_candidate_read(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "source.md", "[[old/path.md]]", embed=False)
    real_reader = ctx.db.reader
    changed = False

    @contextmanager
    def change_after_candidate_read():
        nonlocal changed
        with real_reader() as conn:
            yield conn
        if not changed:
            changed = True
            docs.update(editor, "source.md", created["version"], "unrelated", embed=False)

    monkeypatch.setattr(ctx.db, "reader", change_after_candidate_read)

    result = docs.rename_references(editor, "old/path.md", "new/path.md")

    assert changed
    assert result["docs_rewritten"] == 0
    assert docs.get("source.md")["content"] == "unrelated"


def test_delete_rejects_an_inconsistent_live_document_with_a_purge_intent(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "live-intent.md", "body", embed=False)
    with ctx.db.writer() as conn:
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?",
            (path_norm("live-intent.md"),),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO document_purge_intents("
            "doc_id,path,path_norm,version,actor,via,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                doc_id,
                "live-intent.md",
                path_norm("live-intent.md"),
                created["version"],
                editor.username,
                editor.via,
                "now",
            ),
        )

    with pytest.raises(ConflictError, match="Permanent deletion"):
        docs.delete(editor, "live-intent.md")

    assert docs.get("live-intent.md")["content"] == "body"


def test_purge_rejects_a_pending_tombstone_when_projection_cannot_start(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "blocked-purge.md", "body", embed=False)
    docs.delete(editor, "blocked-purge.md")
    doc_id = _doc_id(ctx, "blocked-purge.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))

    def fail_stage(_vault, _target, _body):
        raise OSError("injected stage failure")

    monkeypatch.setattr(fp, "stage_text", fail_stage)

    with pytest.raises(ProjectionPendingError) as raised:
        docs.purge(admin, "blocked-purge.md")

    assert raised.value.result.reason == "io_error"
    with ctx.db.reader() as conn:
        row = conn.execute("SELECT file_state FROM documents WHERE id=?", (doc_id,)).fetchone()
        intent = conn.execute(
            "SELECT 1 FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone()
    assert row["file_state"] == "pending" and intent is None


def test_purge_rejects_a_live_file_reappearing_after_pending_projection(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "live-reappeared.md", "body", embed=False)
    docs.delete(editor, "live-reappeared.md")
    doc_id = _doc_id(ctx, "live-reappeared.md")
    with ctx.db.writer() as conn:
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
    real_confirm = fp.confirm_confined_absence

    def report_live(vault, target):
        if Path(target) == docs.vault / "live-reappeared.md":
            return False
        return real_confirm(vault, target)

    monkeypatch.setattr(fp, "confirm_confined_absence", report_live)

    with pytest.raises(ProjectionPendingError) as raised:
        docs.purge(admin, "live-reappeared.md")

    assert raised.value.result.reason == "purge_live_present"
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM document_purge_intents WHERE doc_id=?", (doc_id,)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("mutation", ["purged", "intent", "restored", "changed"])
def test_purge_revalidates_public_mutation_before_phase_one_writer(
    ctx, principals, monkeypatch, mutation
):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "purge-race.md", "body", embed=False)
    docs.delete(editor, "purge-race.md")
    real_writer = ctx.db.writer
    reached = threading.Event()
    release = threading.Event()
    paused = False

    @contextmanager
    def pause_worker_before_writer():
        nonlocal paused
        if threading.current_thread().name == "purge-worker" and not paused:
            paused = True
            reached.set()
            assert release.wait(timeout=10)
        with real_writer() as conn:
            yield conn

    monkeypatch.setattr(ctx.db, "writer", pause_worker_before_writer)
    results: list[dict] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_run_call,
        args=(lambda: docs.purge(admin, "purge-race.md"), ctx, results, errors),
        name="purge-worker",
    )
    thread.start()
    assert reached.wait(timeout=5)

    if mutation == "purged":
        docs.purge(admin, "purge-race.md")
    elif mutation == "restored":
        docs.restore(editor, "purge-race.md")
    elif mutation == "changed":
        docs.restore(editor, "purge-race.md")
        docs.delete(editor, "purge-race.md")
    else:
        real_unlink = fp.unlink_regular

        def fail_main_purge(path, *, expected=None, vault=None):
            if threading.current_thread().name == "MainThread" and expected is not None:
                raise OSError("injected durable purge failure")
            return real_unlink(path, expected=expected, vault=vault)

        monkeypatch.setattr(fp, "unlink_regular", fail_main_purge)
        with pytest.raises(ProjectionPendingError):
            docs.purge(admin, "purge-race.md")

    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    if mutation == "purged":
        assert results == [] and len(errors) == 1
        assert isinstance(errors[0], NotFoundError)
    elif mutation == "restored":
        assert results == [] and len(errors) == 1
        assert isinstance(errors[0], ConflictError)
        assert docs.get("purge-race.md")["content"] == "body"
    else:
        assert errors == []
        assert results == [{"ok": True, "path": "purge-race.md", "purged": True}]


def test_purge_stops_after_three_public_generation_changes(ctx, principals, monkeypatch):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    docs.create(editor, "moving-purge.md", "body", embed=False)
    docs.delete(editor, "moving-purge.md")
    real_writer = ctx.db.writer
    reached = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]
    writer_index = 0

    @contextmanager
    def pause_each_worker_writer():
        nonlocal writer_index
        if threading.current_thread().name == "purge-retry-worker":
            index = writer_index
            writer_index += 1
            reached[index].set()
            assert release[index].wait(timeout=10)
        with real_writer() as conn:
            yield conn

    monkeypatch.setattr(ctx.db, "writer", pause_each_worker_writer)
    results: list[dict] = []
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_run_call,
        args=(lambda: docs.purge(admin, "moving-purge.md"), ctx, results, errors),
        name="purge-retry-worker",
    )
    thread.start()
    for index in range(3):
        assert reached[index].wait(timeout=5)
        docs.restore(editor, "moving-purge.md")
        docs.delete(editor, "moving-purge.md")
        release[index].set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert results == [] and len(errors) == 1
    assert isinstance(errors[0], ConflictError)
    assert "kept changing" in str(errors[0])
    assert docs.list_deleted()[0]["path"] == "moving-purge.md"


@pytest.mark.parametrize(
    ("failure", "attempts"),
    [(OSError("writer stat failed"), 3), (fp.FileProjectionError("blocked"), 1)],
)
def test_reindex_isolates_writer_file_validation_failures(ctx, monkeypatch, failure, attempts):
    target = ctx.docs.vault / "external.md"
    target.write_text("external", encoding="utf-8")

    def fail_current(_stable):
        raise failure

    monkeypatch.setattr(fp, "stable_markdown_is_current", fail_current)

    report = ctx.docs.reindex_all()

    assert report["skipped_conflicts"] == [
        {"path": "external.md", "reason": "file_unreadable", "attempts": attempts}
    ]
    assert report["retried"] == attempts - 1
    with ctx.db.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_reindex_exact_cleanup_owner_removes_external_target_before_adoption(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "z-owner.md", "canonical owner", embed=False)
    owner_id = _doc_id(ctx, "z-owner.md")
    target = docs.vault / "a-target.md"
    target.write_text("external target", encoding="utf-8")
    real_signature = fp.confined_file_signature
    injected = False

    def inject_exact_owner(vault, path, *, missing_ok=False):
        nonlocal injected
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == target and not missing_ok and not injected:
            injected = True
            with ctx.db.writer() as conn:
                conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (owner_id,))
                conn.execute(
                    "INSERT INTO file_projection_cleanup("
                    "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                    "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                    "VALUES(?,?,?,1,?,?,?,?,?,1,'now')",
                    (
                        owner_id,
                        "a-target.md",
                        path_norm("a-target.md"),
                        signature.dev,
                        signature.ino,
                        signature.size,
                        signature.mtime_ns,
                        signature.ctime_ns,
                    ),
                )
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", inject_exact_owner)

    report = docs.reindex_all()

    assert injected
    assert report["created"] == 0
    assert report["skipped_conflicts"] == []
    assert not target.exists()
    with ctx.db.reader() as conn:
        owner = conn.execute("SELECT file_state FROM documents WHERE id=?", (owner_id,)).fetchone()
        adopted = conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?", (path_norm("a-target.md"),)
        ).fetchone()
    assert owner["file_state"] == "clean" and adopted is None


@pytest.mark.parametrize(
    ("failure", "attempts"),
    [(OSError("post-owner stat failed"), 3), (fp.FileProjectionError("blocked"), 1)],
)
def test_reindex_reports_signature_failure_after_cleanup_owner_recovery(
    ctx, principals, monkeypatch, failure, attempts
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "z-owner.md", "canonical owner", embed=False)
    owner_id = _doc_id(ctx, "z-owner.md")
    owner_path = docs.vault / "z-owner.md"
    owner_path.unlink()
    owner_path.mkdir()
    target = docs.vault / "a-target.md"
    target.write_text("external target", encoding="utf-8")
    real_signature = fp.confined_file_signature
    injected = False

    def inject_owner_then_fail_postcheck(vault, path, *, missing_ok=False):
        nonlocal injected
        if Path(path) == target and missing_ok and injected:
            raise failure
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == target and not missing_ok and not injected:
            injected = True
            with ctx.db.writer() as conn:
                conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (owner_id,))
                conn.execute(
                    "INSERT INTO file_projection_cleanup("
                    "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                    "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                    "VALUES(?,?,?,1,?,?,?,?,?,1,'now')",
                    (
                        owner_id,
                        "a-target.md",
                        path_norm("a-target.md"),
                        signature.dev,
                        signature.ino,
                        signature.size,
                        signature.mtime_ns,
                        signature.ctime_ns,
                    ),
                )
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", inject_owner_then_fail_postcheck)
    monkeypatch.setattr(
        fp,
        "stable_markdown_is_current",
        lambda stable: (
            real_signature(stable.vault, stable.path, missing_ok=True) == stable.signature
        ),
    )

    report = docs.reindex_all()

    conflict = next(item for item in report["skipped_conflicts"] if item["path"] == "a-target.md")
    assert conflict == {
        "path": "a-target.md",
        "reason": "file_unreadable",
        "attempts": attempts,
    }
    assert report["retried"] == attempts - 1
    assert target.read_text(encoding="utf-8") == "external target"
    with ctx.db.reader() as conn:
        assert (
            conn.execute("SELECT file_state FROM documents WHERE id=?", (owner_id,)).fetchone()[0]
            == "pending"
        )


def test_reindex_existing_target_retires_another_documents_stale_cleanup_authority(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "a-target.md", "managed", embed=False)
    docs.create(editor, "z-owner.md", "owner", embed=False)
    owner_id = _doc_id(ctx, "z-owner.md")
    target = docs.vault / "a-target.md"
    target.write_text("external update", encoding="utf-8")
    real_signature = fp.confined_file_signature
    injected = False

    def inject_stale_owner(vault, path, *, missing_ok=False):
        nonlocal injected
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == target and not missing_ok and not injected:
            injected = True
            with ctx.db.writer() as conn:
                conn.execute(
                    "INSERT INTO file_projection_cleanup("
                    "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                    "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                    "VALUES(?,?,?,0,NULL,NULL,NULL,NULL,NULL,1,'now')",
                    (owner_id, "a-target.md", path_norm("a-target.md")),
                )
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", inject_stale_owner)

    report = docs.reindex_all()

    assert report["updated"] == 1
    assert docs.get("a-target.md")["content"] == "external update"
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (owner_id,)
            ).fetchone()[0]
            == 0
        )


def test_reindex_retries_when_rename_source_reappears_during_identity_check(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    body = "same generation"
    docs.create(editor, "Case.md", body, embed=False)
    old = docs.vault / "Case.md"
    target = docs.vault / "case.md"
    os.replace(old, target)
    real_absence = fp.confirm_confined_absence
    recreated = False

    def recreate_source(vault, path):
        nonlocal recreated
        if Path(path) == old and not recreated:
            recreated = True
            old.write_text(body, encoding="utf-8")
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", recreate_source)

    report = docs.reindex_all()

    assert recreated
    assert report["renamed"] == 0
    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [
        {"path": "case.md", "reason": "rename_source_reappeared", "attempts": 3}
    ]
    assert docs.get("Case.md")["content"] == body
    assert old.read_text(encoding="utf-8") == body
    assert target.read_text(encoding="utf-8") == body


def test_reindex_bounds_repeated_cleanup_owner_generation_replacement(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "z-owner.md", "owner", embed=False)
    owner_id = _doc_id(ctx, "z-owner.md")
    target = docs.vault / "a-target.md"
    target.write_text("external", encoding="utf-8")
    real_signature = fp.confined_file_signature
    injected = False

    def queue_exact(signature):
        with ctx.db.writer() as conn:
            conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (owner_id,))
            conn.execute(
                "INSERT INTO file_projection_cleanup("
                "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
                "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,created_at) "
                "VALUES(?,?,?,1,?,?,?,?,?,1,'now')",
                (
                    owner_id,
                    "a-target.md",
                    path_norm("a-target.md"),
                    signature.dev,
                    signature.ino,
                    signature.size,
                    signature.mtime_ns,
                    signature.ctime_ns,
                ),
            )

    def replace_after_owner_cleanup(vault, path, *, missing_ok=False):
        nonlocal injected
        if Path(path) == target and missing_ok and injected and not target.exists():
            target.write_text("external", encoding="utf-8")
            replacement = real_signature(vault, path, missing_ok=missing_ok)
            assert replacement is not None
            queue_exact(replacement)
            return replacement
        signature = real_signature(vault, path, missing_ok=missing_ok)
        if Path(path) == target and not missing_ok and not injected:
            injected = True
            assert signature is not None
            queue_exact(signature)
        return signature

    monkeypatch.setattr(fp, "confined_file_signature", replace_after_owner_cleanup)

    report = docs.reindex_all()

    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [
        {"path": "a-target.md", "reason": "target_changed", "attempts": 3}
    ]
    assert not target.exists()
    with ctx.db.reader() as conn:
        owner = conn.execute("SELECT file_state FROM documents WHERE id=?", (owner_id,)).fetchone()
        cleanup_count = conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (owner_id,)
        ).fetchone()[0]
    assert owner["file_state"] == "clean" and cleanup_count == 0


def test_reindex_final_recovery_reports_two_pending_documents_added_after_scan(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    trigger = docs.vault / "trigger.md"
    trigger.write_text("trigger", encoding="utf-8")
    real_writer = ctx.db.writer
    real_current = fp.stable_markdown_is_current
    writer_active = threading.Event()
    injected = False

    @contextmanager
    def track_writer():
        with real_writer() as conn:
            writer_active.set()
            try:
                yield conn
            finally:
                writer_active.clear()

    def add_pending_after_commit(stable):
        nonlocal injected
        current = real_current(stable)
        if stable.relative_path == "trigger.md" and not writer_active.is_set() and not injected:
            injected = True
            for rel in ("late-a.md", "late-b.md"):
                docs.create(editor, rel, "late", embed=False)
                path = docs.vault / rel
                path.unlink()
                path.mkdir()
                with ctx.db.writer() as conn:
                    conn.execute(
                        "UPDATE documents SET file_state='pending' WHERE path_norm=?",
                        (path_norm(rel),),
                    )
        return current

    monkeypatch.setattr(ctx.db, "writer", track_writer)
    monkeypatch.setattr(fp, "stable_markdown_is_current", add_pending_after_commit)

    report = docs.reindex_all()

    assert injected
    pending = [
        item for item in report["skipped_conflicts"] if item["reason"] == "pending_projection"
    ]
    assert pending == [
        {"path": "late-a.md", "reason": "pending_projection", "attempts": 1},
        {"path": "late-b.md", "reason": "pending_projection", "attempts": 1},
    ]


def test_reindex_detects_same_hash_peer_purged_during_postcommit_verification(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    editor, admin = principals["editor"], principals["admin"]
    body = "same generation"
    docs.create(editor, "a-source.md", body, embed=False)
    source_id = _doc_id(ctx, "a-source.md")
    docs.create(editor, "m-peer.md", body, embed=False)
    os.replace(docs.vault / "a-source.md", docs.vault / "z-target.md")
    real_absence = fp.confirm_confined_absence
    purged = False

    def purge_peer_after_rename(vault, path):
        nonlocal purged
        if Path(path) == docs.vault / "m-peer.md" and not purged:
            with ctx.db.reader() as conn:
                source_path = conn.execute(
                    "SELECT path FROM documents WHERE id=?", (source_id,)
                ).fetchone()[0]
            if source_path == "z-target.md":
                purged = True
                docs.delete(editor, "m-peer.md")
                docs.purge(admin, "m-peer.md")
        return real_absence(vault, path)

    monkeypatch.setattr(fp, "confirm_confined_absence", purge_peer_after_rename)

    report = docs.reindex_all()

    assert purged
    assert report["renamed"] == 1
    assert report["skipped_conflicts"] == [
        {"path": "z-target.md", "reason": "rename_source_changed", "attempts": 1}
    ]
    assert docs.get("z-target.md")["content"] == body


def test_import_asset_resolution_os_errors_are_isolated(ctx, principals, tmp_path, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    source = tmp_path / "source"
    _write(source, "note.md", "![broken](boom.png)")
    real_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path.name == "boom.png":
            raise OSError("injected resolution failure")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    report = docs.import_from_directory(editor, source, import_attachments=True, embed=False)

    assert report["created"] == 1
    assert report["attachments"] == {"copied": 0, "skipped": 1}
    assert report["warnings"] == [
        "missing asset boom.png referenced by note.md (left as broken link)"
    ]
    assert docs.get("note.md")["content"] == "![broken](boom.png)"


def test_import_rename_skips_variants_claimed_earlier_in_the_batch(ctx, principals, tmp_path):
    docs, editor = ctx.docs, principals["editor"]
    source = tmp_path / "source"
    _write(source, "A.md", "upper")
    _write(source, "a-2.md", "claimed suffix")
    _write(source, "a.md", "lower")

    report = docs.import_from_directory(editor, source, on_conflict="rename", embed=False)

    assert report["renamed"] == 1
    assert [item["target"] for item in report["plan"]] == [
        "A.md",
        "a-2.md",
        "a-3.md",
    ]
    assert docs.get("a-3.md")["content"] == "lower"


def test_import_large_file_records_skip_audit_outside_dry_run(
    ctx, principals, tmp_path, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    source = tmp_path / "source"
    _write(source, "large.md", "too large")
    monkeypatch.setattr("llm_wiki.services.documents.IMPORT_MAX_BYTES", 1)

    report = docs.import_from_directory(editor, source, embed=False)

    assert report["scanned"] == 1
    assert report["created"] == 0
    assert report["warnings"] == ["skipped large.md (file too large)"]
    with ctx.db.reader() as conn:
        audit_row = conn.execute(
            "SELECT action,target,outcome,detail FROM audit_log WHERE action='doc_import_skip'"
        ).fetchone()
    assert tuple(audit_row) == (
        "doc_import_skip",
        "large.md",
        "skipped",
        "file too large",
    )


def test_import_large_file_dry_run_skips_without_an_audit_write(
    ctx, principals, tmp_path, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    source = tmp_path / "source"
    _write(source, "large.md", "too large")
    monkeypatch.setattr("llm_wiki.services.documents.IMPORT_MAX_BYTES", 1)

    report = docs.import_from_directory(editor, source, dry_run=True, embed=False)

    assert report["warnings"] == ["skipped large.md (file too large)"]
    with ctx.db.reader() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='doc_import_skip'"
            ).fetchone()[0]
            == 0
        )


def test_import_isolates_service_and_os_errors_from_individual_files(
    ctx, principals, tmp_path, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    source = tmp_path / "source"
    _write(source, "a-service.md", "service")
    _write(source, "b-os.md", "os")
    _write(source, "c-ok.md", "ok")
    real_create = docs.create

    def create(principal, path, content, **kwargs):
        if path == "a-service.md":
            raise ValidationError("invalid imported document")
        if path == "b-os.md":
            raise OSError("projection unavailable")
        return real_create(principal, path, content, **kwargs)

    monkeypatch.setattr(docs, "create", create)

    report = docs.import_from_directory(editor, source, embed=False)

    assert report["errors"] == [
        {"path": "a-service.md", "error": "invalid imported document"},
        {"path": "b-os.md", "error": "projection unavailable"},
    ]
    assert report["created"] == 1
    assert docs.get("c-ok.md")["content"] == "ok"
