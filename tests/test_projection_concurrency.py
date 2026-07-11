from __future__ import annotations

import threading

from llm_wiki import file_projection as fp
from llm_wiki.db import Database
from llm_wiki.util import path_norm, sha256_hex


def _doc_id(ctx, rel: str) -> int:
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm(rel),)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def test_v3_projector_publishes_before_blocked_v2_projector_resumes(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "reverse.md", "v1", embed=False)

    v2_staged = threading.Event()
    v3_published = threading.Event()
    real_stage = fp.stage_text
    worker_errors: list[BaseException] = []
    worker_results: list[dict] = []

    def block_v2_after_stage(vault, target, body):
        staged = real_stage(vault, target, body)
        if body == "v2":
            v2_staged.set()
            if not v3_published.wait(timeout=10):
                raise AssertionError("v3 projector did not publish while v2 was staged")
        return staged

    monkeypatch.setattr(fp, "stage_text", block_v2_after_stage)

    def update_v2() -> None:
        try:
            worker_results.append(
                docs.update(editor, "reverse.md", 1, "v2", embed=False)
            )
        except BaseException as exc:  # surfaced in the main pytest thread below
            worker_errors.append(exc)

    worker = threading.Thread(target=update_v2, name="blocked-v2-projector")
    worker.start()
    assert v2_staged.wait(timeout=10), "v2 projector never reached its staged fence"

    try:
        v3_result = docs.update(editor, "reverse.md", 2, "v3", embed=False)
    finally:
        # Always release the worker so a failed assertion cannot strand the test run.
        v3_published.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert worker_errors == []
    assert [result["version"] for result in worker_results] == [3]
    assert v3_result["version"] == 3
    assert (docs.vault / "reverse.md").read_text(encoding="utf-8") == "v3"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT version,file_state FROM documents WHERE path_norm=?",
            (path_norm("reverse.md"),),
        ).fetchone()
    assert row is not None
    assert (row["version"], row["file_state"]) == (3, "clean")
    assert list((docs.vault / ".tmp").iterdir()) == []


def test_projector_stops_after_three_real_snapshot_changes(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "hot.md", "v1", embed=False)
    doc_id = _doc_id(ctx, "hot.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,)
        )

    external_db = Database(ctx.db.path)
    real_stage = fp.stage_text
    staged_bodies: list[str] = []

    def stage_then_advance_generation(vault, target, body):
        staged = real_stage(vault, target, body)
        staged_bodies.append(body)
        expected_version = len(staged_bodies)
        next_version = expected_version + 1
        next_body = f"v{next_version}"
        next_hash = sha256_hex(next_body)
        with external_db.writer() as conn:
            changed = conn.execute(
                "UPDATE documents SET version=?,content_hash=?,file_state='pending' "
                "WHERE id=? AND version=?",
                (next_version, next_hash, doc_id, expected_version),
            )
            assert changed.rowcount == 1
            conn.execute(
                "INSERT INTO revisions(doc_id,version,body,title,content_hash,"
                "author_id,op,via,created_at) VALUES(?,?,?,?,?,NULL,'edit','web',?)",
                (doc_id, next_version, next_body, "hot", next_hash, f"v{next_version}"),
            )
        return staged

    monkeypatch.setattr(fp, "stage_text", stage_then_advance_generation)
    try:
        result = docs._project_current(doc_id, max_attempts=3)
    finally:
        external_db.close()

    assert staged_bodies == ["v1", "v2", "v3"]
    assert result.reason == "target_changed"
    assert result.attempts == 3
    assert not result.settled
    assert not result.transitioned
    assert (docs.vault / "hot.md").read_text(encoding="utf-8") == "v1"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT version,file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert row is not None
    assert (row["version"], row["file_state"]) == (4, "pending")
    assert list((docs.vault / ".tmp").iterdir()) == []
