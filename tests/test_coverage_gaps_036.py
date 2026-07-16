"""Close residual coverage gaps introduced around 0.36.0 work."""

from __future__ import annotations

import pytest

import llm_wiki.mcp_server as mcp_mod
from llm_wiki.services import auth
from llm_wiki.services.documents import DocumentService
from llm_wiki.web.routes.auth_pages import _safe_return_path


def test_safe_return_path_rejects_open_redirects():
    assert _safe_return_path(None) == "/"
    assert _safe_return_path("") == "/"
    assert _safe_return_path("relative") == "/"
    assert _safe_return_path("//evil.example") == "/"
    assert _safe_return_path("/ok") == "/ok"
    assert _safe_return_path("/ok?x=1") == "/ok?x=1"
    assert _safe_return_path("/path\\with\\slash") == "/"
    assert _safe_return_path("/http://sneak") == "/"
    assert _safe_return_path("  /notes  ") == "/notes"


def test_authenticate_null_password_hash_fails_after_dummy_work(ctx):
    with ctx.db.writer() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,is_active,"
            "credential_version,created_at,updated_at) "
            "VALUES('nopass',NULL,'viewer',1,1,datetime('now'),datetime('now'))"
        )
    assert auth.authenticate(ctx.db, "nopass", "anything-long-enough") is None


def test_shape_merge_preview_metadata_handles_nulls_and_non_dicts():
    full = {
        "ok": True,
        "base": "b",
        "mine": None,
        "current": "c",
        "merged": None,
        "conflicts": [
            {
                "start_line": 1,
                "base": "x",
                "mine": None,
                "current": "y",
                "resolved": None,
                "merged_start": 0,
            },
            "not-a-dict",
        ],
        "suggested_action": "resolve_conflicts",
    }
    out = mcp_mod._shape_merge_preview(full, "metadata")
    assert out["content_omitted"] is True
    assert "base" not in out
    assert out["base_chars"] == 1
    assert out["mine_chars"] == 0
    assert out["merged_chars"] == 0
    assert out["conflicts"][0]["mine_chars"] == 0
    assert out["conflicts"][1] == "not-a-dict"
    assert mcp_mod._shape_merge_preview({"ok": False}, "metadata") == {"ok": False}
    assert mcp_mod._shape_merge_preview("x", "metadata") == "x"
    assert mcp_mod._shape_merge_preview(full, "full") is full
    slim = mcp_mod._shape_merge_preview({"ok": True, "base": "z"}, "metadata")
    assert slim["base_chars"] == 1
    no_list = mcp_mod._shape_merge_preview({"ok": True, "conflicts": {"a": 1}}, "metadata")
    assert no_list["conflicts"] == {"a": 1}


def test_document_delegate_helpers_are_callable(ctx, principals):
    docs: DocumentService = ctx.docs
    docs.create(principals["editor"], "gap.md", "# Gap\n\nbody")
    assert "hello" in docs._doc_description("# H\n\nhello world")
    body, _trunc = docs._corpus_body_prefix("hello", 100)
    assert isinstance(body, str)
    with ctx.db.reader() as conn:
        assert docs._purge_intent_snapshot(conn, 999999) is None


def test_merge_tags_reraises_non_projection_errors(ctx, principals, monkeypatch):
    docs = ctx.docs
    docs.create(principals["editor"], "t1.md", "---\ntags: [old]\n---\n# T\n")

    def boom(*_a, **_k):
        raise RuntimeError("not projection")

    monkeypatch.setattr(docs, "patch_tags", boom)
    with pytest.raises(RuntimeError, match="not projection"):
        docs.merge_tags(principals["editor"], ["old"], "new")
