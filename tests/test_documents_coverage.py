from __future__ import annotations

import os

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.services.documents import ProjectionPendingError
from llm_wiki.services.errors import ConflictError, NotFoundError, ValidationError
from llm_wiki.util import now_iso, path_norm


class _BrokenEvents:
    def publish(self, _event):
        raise RuntimeError("subscriber stopped")


def test_write_survives_event_failure_and_filters_blank_tags(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.events = _BrokenEvents()

    created = docs.create(editor, "events.md", "body", tags=["", "  ", "#kept"], embed=False)

    assert created["tags"] == ["kept"]
    assert docs.get("events.md")["content"] == "body"
    assert (ctx.settings.vault_path / "events.md").read_text() == "body"


def test_read_navigation_and_corpus_boundaries(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "deep/child/note.md", "# Title\n\nalpha\n\nbeta", tags=["x", "y"], embed=False)
    docs.create(editor, "deep/other.md", "---\ndescription: concise summary\n---\n# Other\n\nbody", tags=["x"], embed=False)

    assert docs.complete("   ") == []
    assert docs.complete("child", limit=999)[0]["path"] == "deep/child/note.md"
    assert docs.preview("deep/child/note.md", max_chars=3)["excerpt"] == "Tit"
    assert docs.list_docs(tags=["x", "x", "y"], sort="unknown")[0]["tags"] == ["x", "y"]
    assert docs.count(folder="deep", tags=["x"]) == 2
    assert docs.list_folders() == ["deep", "deep/child"]
    assert docs.folder_counts() == [("deep", 1), ("deep/child", 1)]
    assert docs.tree()["folders"][0]["path"] == "deep"
    assert docs.nav_tree() is docs.nav_tree()
    assert docs.nav_tags() == [{"tag": "x", "count": 2}, {"tag": "y", "count": 1}]
    assert "concise summary" in docs.llms_index(site_title="Wiki [x]", base_url="https://wiki/")
    full = docs.llms_full(site_title="Wiki", max_chars=1)
    assert full["truncated"] is True
    assert full["included"] == 0 and full["total"] == 2

    with pytest.raises(NotFoundError):
        docs.read_chunk("missing.md", 0)
    with pytest.raises(NotFoundError):
        docs.read_chunk("deep/child/note.md", 999)


def test_folder_error_and_best_effort_projection_boundaries(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    with pytest.raises(ValidationError):
        docs.create_folder(editor, "/")
    with pytest.raises(ValidationError):
        docs.delete_folder(editor, "/")
    with pytest.raises(NotFoundError):
        docs.delete_folder(editor, "missing")

    docs.create(editor, "populated/note.md", "body", embed=False)
    with pytest.raises(ConflictError):
        docs.create_folder(editor, "populated")
    docs.create_folder(editor, "empty/sub")
    real_rmdir = os.rmdir
    calls = []

    def flaky_rmdir(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("busy")
        return real_rmdir(path)

    monkeypatch.setattr(os, "rmdir", flaky_rmdir)
    assert docs.delete_folder(editor, "empty")["deleted"] is True
    assert calls

    with pytest.raises(NotFoundError):
        docs.attachment_file("missing.png")


def test_revision_link_and_move_read_errors(ctx):
    docs = ctx.docs
    for call in (
        lambda: docs.revision("missing.md", 1),
        lambda: docs.compare_revisions("missing.md", 1, 2),
        lambda: docs.backlinks("missing.md"),
        lambda: docs.links("missing.md"),
        lambda: docs.move_preview("missing.md", "new.md"),
    ):
        with pytest.raises(NotFoundError):
            call()


def test_revision_diff_and_targeted_operation_boundaries(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    created = docs.create(editor, "target.md", "# H\n\none\n- [ ] task\none", embed=False)
    docs.update(editor, "target.md", created["version"], "# H\n\ntwo\n- [ ] task\none", embed=False)
    diff = docs.compare_revisions("target.md", 1, 2)
    assert diff["summary"] == {"lines_added": 1, "lines_deleted": 1}
    with pytest.raises(NotFoundError):
        docs.revision("target.md", 99)
    with pytest.raises(NotFoundError):
        docs.compare_revisions("target.md", 99, 2)
    with pytest.raises(NotFoundError):
        docs.compare_revisions("target.md", 1, 99)

    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "x", "y", mode="bad")
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "one", "x", occurrence=0)
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "x" * 1001, "y", mode="regex")
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "(", "y", mode="regex")
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "one", "x", occurrence=3)
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "\n", "x", count=1)
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "one", "x", mode="regex", occurrence=3)
    with pytest.raises(ValidationError):
        docs.patch(editor, "target.md", "\n", "x", mode="regex", count=1)

    with pytest.raises(ValidationError):
        docs.toggle_task(editor, "target.md", index=9)
    with pytest.raises(ValidationError):
        docs.toggle_task(editor, "target.md", line=99)
    assert docs.toggle_task(editor, "target.md", index=0)["version"] == 3


def test_tags_properties_favorites_and_attachment_edges(ctx, principals, monkeypatch):
    docs, editor, viewer = ctx.docs, principals["editor"], principals["viewer"]
    doc = docs.create(editor, "meta.md", "---\nstatus: old\n---\nbody", tags=["a"], embed=False)
    assert docs.patch_tags(editor, "meta.md", add=["a"])["version"] == doc["version"]
    with pytest.raises(ValidationError):
        docs.merge_tags(editor, ["same"], "same")
    with pytest.raises(ValidationError):
        docs.merge_tags(editor, ["a"], "")
    with pytest.raises(ValidationError):
        docs.set_property(editor, "meta.md", "title", "x")
    with pytest.raises(ValidationError):
        docs.set_property(editor, "meta.md", "bad key", "x")
    assert docs.set_property(editor, "meta.md", "absent", "")["version"] == 1
    assert docs.remove_property(editor, "meta.md", "absent")["version"] == 1
    with pytest.raises(ValidationError):
        docs.replace_properties(editor, "meta.md", [("a", ["1"]), ("A", ["2"])])
    updated = docs.replace_properties(editor, "meta.md", [("empty", []), ("list", ["one", "two"])])
    assert "list:" in updated["content"] and "status:" not in updated["content"]
    assert docs.replace_properties(editor, "meta.md", [("list", ["one", "two"])])["version"] == updated["version"]

    with pytest.raises(NotFoundError):
        docs.toggle_favorite(viewer, "missing.md")
    with pytest.raises(NotFoundError):
        docs.set_favorite(viewer, "missing.md", True)
    assert docs.toggle_favorite(viewer, "meta.md")["favorite"] is True
    assert docs.toggle_favorite(viewer, "meta.md")["favorite"] is False
    assert docs.set_favorite(viewer, "meta.md", True)["favorite"] is True
    assert docs.is_favorite(viewer.user_id, "meta.md") is True
    assert docs.list_favorites(viewer.user_id)[0]["path"] == "meta.md"
    assert docs.set_favorite(viewer, "meta.md", False)["favorite"] is False

    with pytest.raises(ValidationError):
        docs.save_attachment(editor, "x.png", b"")
    monkeypatch.setattr("llm_wiki.services.documents.ATTACH_MAX_BYTES", 1)
    with pytest.raises(ValidationError):
        docs.save_attachment(editor, "x.png", b"12")


def test_crud_delete_restore_purge_public_boundaries(ctx, principals):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    assert docs.resolve_link("none", "../invalid") is None
    with pytest.raises(ValidationError):
        docs.update(editor, "none.md", None, "body")
    with pytest.raises(NotFoundError):
        docs.update(editor, "none.md", 1, "body")
    with pytest.raises(NotFoundError):
        docs.delete(editor, "none.md")
    with pytest.raises(NotFoundError):
        docs.restore(editor, "none.md")
    with pytest.raises(NotFoundError):
        docs.purge(admin, "none.md")

    made = docs.create(editor, "live.md", "body", embed=False)
    with pytest.raises(ValidationError):
        docs.restore(editor, "live.md")
    with pytest.raises(ValidationError):
        docs.purge(admin, "live.md")
    with pytest.raises(ConflictError):
        docs.delete(editor, "live.md", base_version=made["version"] + 1)
    assert docs.get("live.md")["version"] == made["version"]

    docs.delete(editor, "live.md", base_version=made["version"])
    deleted = docs.list_deleted(limit=9999, offset=-10)
    assert deleted[0]["path"] == "live.md" and deleted[0]["deleted_by"] == "alice"
    assert docs.restore(editor, "live.md")["restored"] is True
    assert (ctx.settings.vault_path / "live.md").read_text() == "body"


@pytest.mark.parametrize("operation", ["move", "delete", "restore"])
def test_corrupt_latest_revision_is_rejected_without_state_change(ctx, principals, operation):
    docs, editor = ctx.docs, principals["editor"]
    made = docs.create(editor, "corrupt.md", "canonical", embed=False)
    if operation == "restore":
        docs.delete(editor, "corrupt.md")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE revisions SET body='tampered' WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?) "
            "AND version=(SELECT version FROM documents WHERE path_norm=?)",
            (path_norm("corrupt.md"), path_norm("corrupt.md")),
        )

    with pytest.raises(RuntimeError, match="missing or corrupt"):
        if operation == "move":
            docs.move(editor, "corrupt.md", "moved.md")
        elif operation == "delete":
            docs.delete(editor, "corrupt.md")
        else:
            docs.restore(editor, "corrupt.md")

    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT path, version, is_deleted FROM documents WHERE path_norm=?",
            (path_norm("corrupt.md"),),
        ).fetchone()
    expected_version = made["version"] + (1 if operation == "restore" else 0)
    assert (row["path"], row["version"], row["is_deleted"]) == (
        "corrupt.md",
        expected_version,
        int(operation == "restore"),
    )


def test_recent_changes_filters_and_revision_prune(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    made = docs.create(editor, "history.md", "one", embed=False)
    docs.update(editor, "history.md", made["version"], "two", embed=False)
    assert docs.recent_changes(since="0000", until="9999", limit=999)[0]["path"] == "history.md"
    dry = docs.prune_revisions(keep=0, apply=False)
    assert dry == {"keep": 1, "deletable_revisions": 1, "applied": False}
    applied = docs.prune_revisions(keep=1, apply=True)
    assert applied["deletable_revisions"] == 1 and applied["applied"] is True
    assert [r["version"] for r in docs.revisions("history.md")["revisions"]] == [2]


def test_projection_path_failure_leaves_durable_write_for_recovery(
    ctx, principals, monkeypatch
):
    docs, editor = ctx.docs, principals["editor"]
    real_managed = fp.managed_path
    calls = 0

    def fail_during_projection(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise fp.FileProjectionError("blocked target")
        return real_managed(*args, **kwargs)

    monkeypatch.setattr(fp, "managed_path", fail_during_projection)
    with pytest.raises(ProjectionPendingError) as caught:
        docs.create(editor, "pending.md", "durable", embed=False)
    assert caught.value.result.reason == "io_error"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.id,d.file_state,d.version,r.body FROM documents d JOIN revisions r "
            "ON r.doc_id=d.id AND r.version=d.version WHERE d.path_norm=?",
            (path_norm("pending.md"),),
        ).fetchone()
    assert (row["file_state"], row["version"], row["body"]) == ("pending", 1, "durable")
    assert not (ctx.settings.vault_path / "pending.md").exists()

    monkeypatch.setattr(fp, "managed_path", real_managed)
    assert docs.recover_pending() == 1
    assert (ctx.settings.vault_path / "pending.md").read_text() == "durable"


def test_recovery_reports_corrupt_generation_without_overwriting_file(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "broken.md", "canonical", embed=False)
    projected = ctx.settings.vault_path / "broken.md"
    projected.write_text("external", encoding="utf-8")
    with ctx.db.writer() as conn:
        conn.execute(
            "UPDATE documents SET file_state='pending' WHERE path_norm=?",
            (path_norm("broken.md"),),
        )
        conn.execute(
            "UPDATE revisions SET body='corrupt' WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?) "
            "AND version=(SELECT version FROM documents WHERE path_norm=?)",
            (path_norm("broken.md"), path_norm("broken.md")),
        )

    assert docs.recover_pending() == 0
    assert projected.read_text() == "external"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE path_norm=?", (path_norm("broken.md"),)
        ).fetchone()[0] == "pending"


def test_purge_io_failure_preserves_intent_and_retry_finishes(ctx, principals, monkeypatch):
    docs, editor, admin = ctx.docs, principals["editor"], principals["admin"]
    docs.create(editor, "purge.md", "body", embed=False)
    docs.delete(editor, "purge.md")
    real_unlink = fp.unlink_regular

    def changed(path, **kwargs):
        if ".trash" in str(path):
            return False
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(fp, "unlink_regular", changed)
    with pytest.raises(ProjectionPendingError) as caught:
        docs.purge(admin, "purge.md")
    assert caught.value.result.reason == "purge_io_error"
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT d.is_deleted,d.file_state,COUNT(p.doc_id) AS intents FROM documents d "
            "LEFT JOIN document_purge_intents p ON p.doc_id=d.id WHERE d.path_norm=? GROUP BY d.id",
            (path_norm("purge.md"),),
        ).fetchone()
    assert (row["is_deleted"], row["file_state"], row["intents"]) == (1, "pending", 1)

    monkeypatch.setattr(fp, "unlink_regular", real_unlink)
    assert docs.recover_pending() == 1
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT 1 FROM documents WHERE path_norm=?", (path_norm("purge.md"),)
        ).fetchone() is None
    assert not (ctx.settings.vault_path / ".trash" / "purge.md").exists()


def _queue_cleanup(ctx, doc_path, cleanup_path, *, expected_exists=0):
    with ctx.db.writer() as conn:
        row = conn.execute(
            "SELECT id,version FROM documents WHERE path_norm=?", (path_norm(doc_path),)
        ).fetchone()
        conn.execute("UPDATE documents SET file_state='pending' WHERE id=?", (row["id"],))
        conn.execute(
            "INSERT INTO file_projection_cleanup(doc_id,path,path_norm,expected_exists,"
            "queued_version,created_at) VALUES(?,?,?,?,?,?)",
            (
                row["id"],
                cleanup_path,
                path_norm(cleanup_path),
                expected_exists,
                row["version"],
                now_iso(),
            ),
        )
        return row["id"]


def test_recovery_retires_stale_current_owned_and_absent_cleanup_rows(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "current.md", "current", embed=False)
    docs.create(editor, "owned.md", "owned", embed=False)
    doc_id = _queue_cleanup(ctx, "current.md", "current.md")
    with ctx.db.writer() as conn:
        for cleanup_path in ("owned.md", "already-gone.md"):
            conn.execute(
                "INSERT INTO file_projection_cleanup(doc_id,path,path_norm,expected_exists,"
                "queued_version,created_at) VALUES(?,?,?,?,1,?)",
                (doc_id, cleanup_path, path_norm(cleanup_path), 0, now_iso()),
            )

    assert docs.recover_pending() == 1
    assert (docs.vault / "current.md").read_text() == "current"
    assert (docs.vault / "owned.md").read_text() == "owned"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "clean"


def test_recovery_preserves_changed_cleanup_generation(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "new.md", "canonical", embed=False)
    old = docs.vault / "old.md"
    old.write_text("external owner", encoding="utf-8")
    doc_id = _queue_cleanup(ctx, "new.md", "old.md", expected_exists=0)

    assert docs.recover_pending() == 0
    assert old.read_text() == "external owner"
    assert (docs.vault / "new.md").read_text() == "canonical"
    with ctx.db.reader() as conn:
        assert conn.execute(
            "SELECT file_state FROM documents WHERE id=?", (doc_id,)
        ).fetchone()[0] == "pending"
        assert conn.execute(
            "SELECT COUNT(*) FROM file_projection_cleanup WHERE doc_id=?", (doc_id,)
        ).fetchone()[0] == 1


def test_move_and_reference_rewrite_failure_boundaries(ctx, principals, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "source.md", "[[old/path.md]]", embed=False)
    assert docs.move(editor, "source.md", "SOURCE.md")["path"] == "source.md"
    with pytest.raises(NotFoundError):
        docs.move(editor, "missing.md", "new.md")

    real_update = docs.update

    def conflict(*args, **kwargs):
        raise ConflictError("raced")

    monkeypatch.setattr(docs, "update", conflict)
    result = docs.rename_references(editor, "old/path.md", "new/path.md")
    assert result["skipped_conflicts"] == 1
    assert docs.get("source.md")["content"] == "[[old/path.md]]"
    monkeypatch.setattr(docs, "update", real_update)
