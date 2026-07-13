"""Adversarial two-phase purge and stale-projector race contracts."""

from __future__ import annotations

import threading

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.db import Database
from llm_wiki.services.auth import Principal, create_api_key, create_user, principal_from_api_key
from llm_wiki.services.documents import DocumentService
from llm_wiki.util import path_norm


class _AfterPhaseOne(RuntimeError):
    """Simulate process loss after the durable purge request commits."""


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def _leave_purge_intent(docs, principal, rel: str, monkeypatch) -> int:
    doc_id = _doc_id_from_service(docs, rel)
    original_finish = docs._finish_purge

    def interrupt(_doc_id: int):
        raise _AfterPhaseOne

    monkeypatch.setattr(docs, "_finish_purge", interrupt)
    try:
        with pytest.raises(_AfterPhaseOne):
            docs.purge(principal, rel)
    finally:
        monkeypatch.setattr(docs, "_finish_purge", original_finish)
    return doc_id


def _doc_id_from_service(docs, rel: str) -> int:
    with docs.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def test_purge_preserves_live_file_created_after_clean_tombstone(
    ctx, principals
):
    docs = ctx.docs
    docs.create(principals["editor"], "external-after-delete.md", "managed", embed=False)
    docs.delete(principals["editor"], "external-after-delete.md")
    live = docs.vault / "external-after-delete.md"
    trash = docs.vault / ".trash" / "external-after-delete.md"
    with ctx.db.reader() as conn:
        state = conn.execute(
            "SELECT file_state FROM documents WHERE path_norm=?",
            (path_norm("external-after-delete.md"),),
        ).fetchone()[0]
    assert state == "clean"

    live.write_text("new external generation", encoding="utf-8")
    docs.purge(principals["admin"], "external-after-delete.md")

    assert live.read_text(encoding="utf-8") == "new external generation"
    assert not trash.exists()
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?",
            (path_norm("external-after-delete.md"),),
        ).fetchone() is None


def test_purge_api_retry_keeps_original_intent_actor_and_via(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    docs.create(principals["editor"], "immutable-request.md", "body", embed=False)
    docs.delete(principals["editor"], "immutable-request.md")
    token = create_api_key(ctx.db, principals["admin"], "purge-retry")
    original_admin = principal_from_api_key(ctx.db, token)
    assert original_admin is not None
    retry_id = create_user(ctx.db, "retry-admin", "secret12", "admin")
    retry_admin = Principal(retry_id, "retry-admin", "admin", via="web")
    doc_id = _leave_purge_intent(
        docs, original_admin, "immutable-request.md", monkeypatch
    )

    with ctx.db.reader() as conn:
        before = conn.execute(
            "SELECT actor,via FROM document_purge_intents WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
    assert before is not None
    assert (before["actor"], before["via"]) == ("admin", "mcp")

    docs.purge(retry_admin, "immutable-request.md")

    with ctx.db.reader() as conn:
        audits = conn.execute(
            "SELECT actor,via FROM audit_log "
            "WHERE action='doc_purge' AND target='immutable-request.md' ORDER BY id"
        ).fetchall()
    assert [(row["actor"], row["via"]) for row in audits] == [("admin", "mcp")]


def test_two_database_finishers_audit_once_and_loser_is_settled_noop(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    docs.create(principals["editor"], "two-finishers.md", "body", embed=False)
    docs.delete(principals["editor"], "two-finishers.md")
    doc_id = _leave_purge_intent(
        docs, principals["admin"], "two-finishers.md", monkeypatch
    )
    other_db = Database(ctx.db.path)
    other_docs = DocumentService(other_db, ctx.embedder, docs.vault)
    barrier = threading.Barrier(2)
    results = []
    errors: list[BaseException] = []

    def finish(service: DocumentService) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(service._finish_purge(doc_id))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            service.db.close()

    threads = [
        threading.Thread(target=finish, args=(docs,)),
        threading.Thread(target=finish, args=(other_docs,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert sum(result.transitioned for result in results) == 1
    loser = next(result for result in results if not result.transitioned)
    assert loser.settled and loser.reason == "missing"
    with ctx.db.reader() as conn:
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action='doc_purge' AND target='two-finishers.md'"
        ).fetchone()[0]
    assert audit_count == 1


def test_purge_visits_all_193_cleanup_rows_in_batches_of_at_most_64(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    docs.create(principals["editor"], "many-cleanups.md", "body", embed=False)
    docs.delete(principals["editor"], "many-cleanups.md")
    doc_id = _doc_id(ctx, "many-cleanups.md")
    cleanup_paths = [f"historical/{index:03d}.md" for index in range(193)]
    historical = docs.vault / "historical"
    historical.mkdir()
    first = docs.vault / cleanup_paths[0]
    last = docs.vault / cleanup_paths[-1]
    first.write_text("external first", encoding="utf-8")
    last.write_text("external last", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.executemany(
            "INSERT INTO file_projection_cleanup("
            "doc_id,path,path_norm,expected_exists,expected_dev,expected_ino,"
            "expected_size,expected_mtime_ns,expected_ctime_ns,queued_version,"
            "created_at) VALUES(?,?,?,0,NULL,NULL,NULL,NULL,NULL,2,'now')",
            [(doc_id, rel, path_norm(rel)) for rel in cleanup_paths],
        )

    original_batch = docs._process_purge_cleanup_batch
    processed_pages: list[int] = []

    def observe_batch(conn, intent, *, after_norm: str, batch_size: int = 64):
        before = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup "
                "WHERE doc_id=? AND path_norm>?",
                (intent.doc_id, after_norm),
            ).fetchone()[0]
        )
        result = original_batch(
            conn, intent, after_norm=after_norm, batch_size=batch_size
        )
        after = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_projection_cleanup "
                "WHERE doc_id=? AND path_norm>?",
                (intent.doc_id, after_norm),
            ).fetchone()[0]
        )
        processed_pages.append(before - after)
        return result

    monkeypatch.setattr(docs, "_process_purge_cleanup_batch", observe_batch)
    docs.purge(principals["admin"], "many-cleanups.md")

    nonempty_pages = [size for size in processed_pages if size]
    assert nonempty_pages == [64, 64, 64, 1]
    assert max(nonempty_pages) <= 64
    assert first.read_text(encoding="utf-8") == "external first"
    assert last.read_text(encoding="utf-8") == "external last"


def test_phase_one_purge_intent_fences_projector_staged_before_request(
    ctx, principals, monkeypatch
):
    docs = ctx.docs
    docs.create(principals["editor"], "phase-one-fence.md", "canonical", embed=False)
    docs.delete(principals["editor"], "phase-one-fence.md")
    doc_id = _doc_id(ctx, "phase-one-fence.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    staged = threading.Event()
    release = threading.Event()
    real_stage = fp.stage_text
    projector_result = []
    projector_errors: list[BaseException] = []

    def pause_stale_stage(vault, target, body):
        staged_text = real_stage(vault, target, body)
        if threading.current_thread().name == "stale-projector":
            staged.set()
            if not release.wait(timeout=10):
                raise TimeoutError("stale projector was not released")
        return staged_text

    def run_stale_projector() -> None:
        try:
            projector_result.append(docs._project_current(doc_id))
        except BaseException as exc:  # pragma: no cover - asserted below
            projector_errors.append(exc)
        finally:
            ctx.db.close()

    monkeypatch.setattr(fp, "stage_text", pause_stale_stage)
    trash = docs.vault / ".trash" / "phase-one-fence.md"
    thread = threading.Thread(target=run_stale_projector, name="stale-projector")
    thread.start()
    try:
        assert staged.wait(timeout=5)
        _leave_purge_intent(
            docs, principals["admin"], "phase-one-fence.md", monkeypatch
        )
        trash.write_text("generation after phase one", encoding="utf-8")
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert projector_errors == []
    assert len(projector_result) == 1
    assert projector_result[0].reason == "purge_pending"
    assert not projector_result[0].settled
    assert trash.read_text(encoding="utf-8") == "generation after phase one"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM document_purge_intents WHERE doc_id=?", (doc_id,)
        ).fetchone() is not None
