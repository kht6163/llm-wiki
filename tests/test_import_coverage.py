from __future__ import annotations

from pathlib import Path

import pytest

from llm_wiki.services.errors import ConflictError, ValidationError


def _write(root: Path, rel: str, data: str | bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        target.write_bytes(data)
    else:
        target.write_text(data, encoding="utf-8")
    return target


def test_import_validates_arguments(ctx, principals, tmp_path):
    docs, editor = ctx.docs, principals["editor"]
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(ValidationError):
        docs.import_from_directory(editor, src, on_conflict="merge")
    with pytest.raises(ValidationError):
        docs.import_from_directory(editor, tmp_path / "missing")
    with pytest.raises(ValidationError):
        docs.import_from_directory(editor, src, into="../escape")


def test_import_asset_warnings_cache_and_extension_normalization(ctx, principals, tmp_path, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    src = tmp_path / "src"
    outside = _write(tmp_path, "outside.png", b"png")
    _write(src, "assets/same.png", b"same")
    _write(src, "assets/no.txt", b"text")
    _write(src, "assets/empty.png", b"")
    _write(
        src,
        "note.markdown",
        "# N\n\n![a](assets/same.png) ![b](assets/same.png) "
        "![e](../outside.png) ![u](assets/no.txt) ![z](assets/empty.png) "
        "![remote](https://example.test/x.png) ![root](/x.png) ![anchor](#x)",
    )
    report = docs.import_from_directory(editor, src, import_attachments=True, embed=False)

    assert outside.exists()
    assert report["created"] == 1
    assert report["attachments"] == {"copied": 1, "skipped": 3}
    assert any("escapes the source" in item for item in report["warnings"])
    assert any("unsupported asset" in item for item in report["warnings"])
    assert any("empty or too large" in item for item in report["warnings"])
    body = docs.get("note.md")["content"]
    assert body.count("/attachments/same-") == 2
    assert "https://example.test/x.png" in body


def test_import_case_collision_tombstone_rename_invalid_encoding_and_flat_walk(
    ctx, principals, tmp_path
):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "gone.md", "old", embed=False)
    docs.delete(editor, "gone.md")
    src = tmp_path / "src"
    _write(src, "gone.mdown", "revived elsewhere")
    _write(src, "A.md", "upper")
    _write(src, "a.md", "lower")
    _write(src, "bad.mkd", b"bad\xfftext")
    _write(src, "nested/ignored.md", "nested")

    report = docs.import_from_directory(
        editor,
        src,
        on_conflict="rename",
        recurse=False,
        embed=False,
    )

    assert report["renamed"] == 2
    assert docs.exists("gone-2.md")
    assert docs.exists("A.md") and docs.exists("a-2.md")
    assert not docs.exists("nested/ignored.md")
    assert any("case collision" in item for item in report["warnings"])
    assert any("encoding replaced" in item for item in report["warnings"])


def test_import_records_per_file_conflict_and_continues(ctx, principals, tmp_path, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    src = tmp_path / "src"
    _write(src, "a.md", "a")
    _write(src, "b.md", "b")
    real_create = docs.create

    def create(principal, path, content, **kwargs):
        if path == "a.md":
            raise ConflictError("raced")
        return real_create(principal, path, content, **kwargs)

    monkeypatch.setattr(docs, "create", create)
    report = docs.import_from_directory(editor, src, embed=False)

    assert report["errors"] == [{"path": "a.md", "error": "raced"}]
    assert docs.exists("b.md")


def test_import_file_io_failures_and_odd_embeds_are_isolated(ctx, principals, tmp_path, monkeypatch):
    docs, editor = ctx.docs, principals["editor"]
    src = tmp_path / "src"
    unreadable_asset = _write(src, "bad.png", b"png")
    vanished = _write(src, "vanished.md", "body")
    unreadable = _write(src, "unreadable.md", "body")
    _write(src, "odd.md", "# Odd\n\n![[#heading]] ![x](bad.png)")
    _write(src, "skip.txt", "not markdown")
    _write(src, "bad\nname.md", "invalid target")
    real_stat = Path.stat
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    vanished_stats = 0

    def stat(path, *args, **kwargs):
        nonlocal vanished_stats
        if path == vanished:
            vanished_stats += 1
        if path == vanished and vanished_stats > 2:
            raise OSError("vanished")
        return real_stat(path, *args, **kwargs)

    def read_text(path, *args, **kwargs):
        if path == unreadable:
            raise OSError("denied")
        return real_read_text(path, *args, **kwargs)

    def read_bytes(path, *args, **kwargs):
        if path == unreadable_asset:
            raise OSError("denied")
        return real_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    report = docs.import_from_directory(
        editor, src, import_attachments=True, recurse=False, embed=False
    )

    assert any("vanished before read" in item for item in report["warnings"])
    assert any("control characters" in item for item in report["warnings"])
    assert {item["path"] for item in report["errors"]} == {"unreadable.md"}
    assert report["attachments"]["skipped"] == 1
    assert "![[#heading]]" in docs.get("odd.md")["content"]


def test_import_dry_run_without_embedding_does_not_predict_embed(ctx, principals, tmp_path):
    src = tmp_path / "src"
    _write(src, "a.md", "body")
    report = ctx.docs.import_from_directory(
        principals["editor"], src, dry_run=True, embed=False
    )
    assert report["created"] == 1 and report["embedded"] == 0
    assert not ctx.docs.exists("a.md")
