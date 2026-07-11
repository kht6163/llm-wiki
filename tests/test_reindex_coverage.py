from __future__ import annotations

from llm_wiki import file_projection as fp
from llm_wiki.util import path_norm


def test_reindex_recovery_exception_isolated_and_pending_state_remains(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "pending.md", "canonical", embed=False)
    projected = docs.vault / "pending.md"
    projected.unlink()
    with ctx.db.writer() as conn:
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm=?", (path_norm("pending.md"),)
        ).fetchone()[0]
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (doc_id,))
    real_project = docs._project_current

    def fail_one(target_id, **kwargs):
        if target_id == doc_id:
            raise RuntimeError("projector unavailable")
        return real_project(target_id, **kwargs)

    monkeypatch.setattr(docs, "_project_current", fail_one)
    report = docs.reindex_all()

    assert {item["path"]: item["reason"] for item in report["skipped_conflicts"]}[
        "pending.md"
    ] == "pending_projection"
    assert not projected.exists()
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"


def test_reindex_retries_stable_read_failure_without_database_adoption(ctx, monkeypatch):
    path = ctx.docs.vault / "unstable.md"
    path.write_text("external generation", encoding="utf-8")
    real_read = fp.read_stable_markdown

    def unstable(vault, target):
        if target == path:
            raise fp.StableFileError("file_unreadable", "changing")
        return real_read(vault, target)

    monkeypatch.setattr(fp, "read_stable_markdown", unstable)
    report = ctx.docs.reindex_all()

    assert report["retried"] == 2
    assert report["skipped_conflicts"] == [
        {"path": "unstable.md", "reason": "file_unreadable", "attempts": 3}
    ]
    assert path.read_text() == "external generation"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?", (path_norm("unstable.md"),)
        ).fetchone() is None


def test_reindex_transient_scan_signature_failure_is_retried_and_adopted(ctx, monkeypatch):
    path = ctx.docs.vault / "scan-race.md"
    path.write_text("# External\n\nbody", encoding="utf-8")
    real_signature = fp.confined_file_signature
    failed = False

    def transient(vault, target, *args, **kwargs):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("temporary stat failure")
        return real_signature(vault, target, *args, **kwargs)

    monkeypatch.setattr(fp, "confined_file_signature", transient)
    report = ctx.docs.reindex_all()

    assert failed and report["created"] == 1
    assert report["skipped_conflicts"] == []
    assert ctx.docs.get("scan-race.md")["content"] == "# External\n\nbody"
