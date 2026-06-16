"""#10 single-command full snapshot/restore (DB + vault + manifest as one .tar)."""
import io
import json
import tarfile
from types import SimpleNamespace

from llm_wiki import _cli_impl
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
