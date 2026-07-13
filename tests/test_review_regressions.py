from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import file_projection as fp
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.services import auth, users
from llm_wiki.services.auth import Principal
from llm_wiki.services.documents import ProjectionPendingError
from llm_wiki.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from llm_wiki.util import PathError


def test_idempotency_key_rejects_different_logical_request(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "idem.md", "# Log\n", embed=False)
    docs.append_to_document(editor, "idem.md", "first", idempotency_key="same")

    with pytest.raises(ConflictError, match="different request"):
        docs.append_to_document(editor, "idem.md", "second", idempotency_key="same")

    assert docs.get("idem.md")["content"].count("first") == 1
    assert "second" not in docs.get("idem.md")["content"]


def test_writer_rechecks_current_role_inside_transaction(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    doc = docs.create(editor, "demoted.md", "v1", embed=False)
    users.set_role(ctx.db, editor.user_id, "viewer")

    with pytest.raises(ForbiddenError, match="read-only"):
        docs.update(editor, "demoted.md", doc["version"], "v2", embed=False)

    assert docs.get("demoted.md")["content"] == "v1"


def test_writer_rechecks_individually_revoked_mcp_key(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    doc = docs.create(editor, "revoked-key.md", "v1", embed=False)
    raw = auth.create_api_key(ctx.db, editor, "writer")
    mcp_principal = auth.principal_from_api_key(ctx.db, raw)
    assert mcp_principal is not None and mcp_principal.api_key_id is not None
    auth.revoke_api_key(ctx.db, editor, mcp_principal.api_key_id)

    with pytest.raises(ForbiddenError, match="revoked"):
        docs.update(mcp_principal, doc["path"], doc["version"], "v2", embed=False)


def test_attachment_and_template_roots_reject_symlinks(ctx, principals, tmp_path):
    docs, editor = ctx.docs, principals["editor"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"private")
    (outside / "template.md").write_text("outside", encoding="utf-8")
    (docs.vault / "_attachments").symlink_to(outside, target_is_directory=True)
    (docs.vault / "_templates").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathError):
        docs.save_attachment(editor, "new.png", b"image")
    with pytest.raises(PathError):
        docs.attachment_bytes("secret.png")
    assert docs.list_templates() == []
    with pytest.raises(ValidationError, match="invalid template path|template not found"):
        docs.create(editor, "from-template.md", "", template="template", embed=False)
    assert not (outside / "new.png").exists()


def test_projection_pending_is_structured_as_committed_result():
    result = fp.ProjectionResult(7, "note.md", False, False, "target_changed", attempts=3)
    error = ProjectionPendingError(result, version=4)

    assert error.http_status == 202
    assert error.to_dict() == {
        "ok": False,
        "error": {
            "code": "projection_pending",
            "message": "Document file projection remains pending (target_changed)",
            "suggested_action": "check_status_do_not_repeat_write",
            "committed": True,
            "path": "note.md",
            "version": 4,
            "projection_reason": "target_changed",
            "projection_attempts": 3,
        },
    }


def test_writer_fence_covers_cli_and_current_identity_boundaries(ctx, principals):
    fence = ctx.docs._fence_principal
    with ctx.db.reader() as conn:
        with pytest.raises(ForbiddenError, match="CLI role is not admin"):
            fence(
                conn,
                Principal(None, "local", "editor", via="cli"),  # type: ignore[arg-type]
                require_admin=True,
            )
        with pytest.raises(ForbiddenError, match="CLI role is read-only"):
            fence(
                conn,
                Principal(None, "local", "viewer", via="cli"),  # type: ignore[arg-type]
                require_write=True,
            )
        with pytest.raises(ForbiddenError, match="Credentials changed"):
            fence(conn, Principal(999_999, "gone", "editor"), require_write=True)
        with pytest.raises(ForbiddenError, match="role is not admin"):
            fence(conn, principals["editor"], require_admin=True)
        with pytest.raises(ForbiddenError, match="role is read-only"):
            fence(conn, principals["viewer"], require_write=True)
        with pytest.raises(ForbiddenError, match="identity is no longer valid"):
            fence(
                conn,
                Principal(
                    principals["editor"].user_id,
                    "alice",
                    "editor",
                    via="mcp",
                ),
                require_write=True,
            )

    token = auth.create_api_key(ctx.db, principals["editor"], "reader", scope="read")
    read_key = auth.principal_from_api_key(ctx.db, token)
    assert read_key is not None
    with ctx.db.reader() as conn, pytest.raises(ForbiddenError, match="read-only"):
        fence(conn, read_key, require_write=True)


def test_confined_attachment_accessors_cover_valid_and_unsafe_paths(
    ctx, principals, tmp_path
):
    docs = ctx.docs
    saved = docs.save_attachment(principals["editor"], "safe.png", b"safe-image")
    subpath = saved["path"].removeprefix("_attachments/")
    assert docs.attachment_file(subpath).is_file()
    with pytest.raises(NotFoundError):
        docs.attachment_bytes("missing.png")

    outside = tmp_path / "attachment-outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"secret")
    attachment_root = docs.vault / "_attachments"
    for child in attachment_root.iterdir():
        child.unlink()
    attachment_root.rmdir()
    attachment_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathError):
        docs.attachment_file("secret.png")
    with pytest.raises(PathError):
        docs.attachment_bytes("secret.png")


def test_embedding_status_worker_and_disabled_related_missing(tmp_path, ctx):
    ctx.docs.embed_worker = SimpleNamespace(status=lambda: {"running": True})
    assert ctx.docs.embedding_status()["embed_worker"] == {"running": True}

    settings = Settings(
        vault_path=tmp_path / "disabled-vault",
        db_path=tmp_path / "disabled-data" / "wiki.db",
        embedding_enabled=False,
        session_secret="disabled-related-test",
        gui_port=18090,
        mcp_port=18091,
    )
    disabled = build_context(settings, full=True)
    try:
        with pytest.raises(Exception) as caught:
            disabled.docs.related("missing.md")
        assert getattr(caught.value, "code", None) == "not_found"
    finally:
        disabled.db.close()


def test_template_defensive_branches(ctx, monkeypatch):
    docs = ctx.docs
    root = docs.vault / "_templates"
    root.mkdir()
    (root / "plain.txt").write_text("ignored", encoding="utf-8")
    (root / ".hidden.md").write_text("ignored", encoding="utf-8")
    (root / "invalid.md").write_bytes(b"\xff")
    assert docs.list_templates() == []

    monkeypatch.setattr(fp, "list_confined_names", lambda *_args: ("a/b.md", "a\\b.md"))
    assert docs.list_templates() == []
    monkeypatch.setattr(fp, "list_confined_names", lambda *_args: ("unsafe.md",))
    monkeypatch.setattr(
        fp,
        "read_confined_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(fp.UnsafeProjectionPath("unsafe")),
    )
    assert docs.list_templates() == []


def test_template_resolution_and_second_read_failures(ctx, monkeypatch):
    docs = ctx.docs
    root = docs.vault / "_templates"
    root.mkdir()
    (root / "good.md").write_text("# Good", encoding="utf-8")
    assert docs._resolve_template_path("./good").name == "good.md"
    for name in (None, "~private", "a/../b", "bad\x01name"):
        with pytest.raises(ValidationError):
            docs._resolve_template_path(name)  # type: ignore[arg-type]

    monkeypatch.setattr(docs, "_resolve_template_path", lambda _name: Path("good.md"))
    monkeypatch.setattr(
        fp,
        "read_confined_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("gone")),
    )
    with pytest.raises(ValidationError, match="not readable"):
        docs._load_template_body("good")


@pytest.mark.parametrize("key", ["", "x" * 201, "bad\nkey"])
def test_append_rejects_invalid_idempotency_keys(ctx, principals, key):
    with pytest.raises(ValidationError, match="idempotency_key"):
        ctx.docs.append_to_document(
            principals["editor"], "missing.md", "text", idempotency_key=key
        )


def test_tag_merge_reports_committed_pending_and_skipped(ctx, principals, monkeypatch):
    docs = ctx.docs
    docs.create(principals["editor"], "a.md", "---\ntags: [old]\n---\n# A", embed=False)
    docs.create(principals["editor"], "b.md", "---\ntags: [old]\n---\n# B", embed=False)

    def patch(_principal, path, **_kwargs):
        if path == "a.md":
            raise ProjectionPendingError(
                fp.ProjectionResult(1, path, False, False, "busy", attempts=1)
            )
        raise ConflictError("changed")

    monkeypatch.setattr(docs, "patch_tags", patch)
    result = docs.merge_tags(principals["editor"], ["old"], "new")
    assert result["docs_changed"] == 1
    assert result["docs_skipped"] == 1
    assert result["projection_pending"] == [{"path": "a.md", "reason": "busy"}]
