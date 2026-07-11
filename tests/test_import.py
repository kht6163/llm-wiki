"""Bulk Obsidian/markdown directory importer (`llm-wiki import`).

Covers the service (`DocumentService.import_from_directory`) and the CLI wrapper:
routing through create()/update() for real revisions, dry-run prediction, conflict
strategies, tombstone revival, idempotent re-runs, embed/attachment rewriting,
skip rules (excluded dirs, symlinks, empty/oversized/self-import), and exit codes.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_wiki import _cli_impl
from llm_wiki.services import audit
from llm_wiki.services.auth import Principal
from llm_wiki.services.errors import ForbiddenError, ValidationError


def _imp(principals) -> Principal:
    """An editor principal carrying the importer's surface tag."""
    e = principals["editor"]
    return Principal(e.user_id, e.username, "editor", via="cli")


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---- service: creation + provenance ----------------------------------------
def test_import_creates_docs_via_create(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nalpha")
    _write(src, "sub/b.md", "# B\n\nbeta")
    rep = docs.import_from_directory(p, src, into="notes")
    assert rep["created"] == 2 and rep["scanned"] == 2 and not rep["errors"]
    a = docs.get("notes/a.md")
    assert a["version"] == 1 and "alpha" in a["content"]
    assert docs.get("notes/sub/b.md")["title"] == "B"
    rev = docs.revisions("notes/a.md")["revisions"][0]
    assert rev["op"] == "create" and rev["via"] == "cli" and rev["author"] == "alice"
    assert (ctx.settings.vault_path / "notes" / "a.md").exists()  # projected to disk


def test_import_forbidden_for_viewer(ctx, principals, tmp_path):
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nx")
    with pytest.raises(ForbiddenError):
        ctx.docs.import_from_directory(principals["viewer"], src, into="")


# ---- dry-run predicts the real run -----------------------------------------
def test_import_dry_run_predicts_real_run(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    docs.create(p, "notes/exists.md", "# Exists\n\nold")
    src = tmp_path / "src"
    _write(src, "fresh.md", "# Fresh\n\nnew")
    _write(src, "exists.md", "# Exists\n\ndifferent")

    dry = docs.import_from_directory(p, src, into="notes", dry_run=True)
    assert not docs.exists("notes/fresh.md")          # dry-run wrote nothing
    real = docs.import_from_directory(p, src, into="notes")
    assert docs.exists("notes/fresh.md")

    for k in ("created", "revived", "overwritten", "skipped", "renamed", "scanned", "embedded"):
        assert dry[k] == real[k], k
    norm = lambda r: sorted((x["target"], x["action"]) for x in r["plan"])  # noqa: E731
    assert norm(dry) == norm(real)
    assert dry["created"] == 1 and dry["skipped"] == 1


# ---- conflict strategies ----------------------------------------------------
def test_import_on_conflict_skip_leaves_existing(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    docs.create(p, "k.md", "# K\n\noriginal")
    src = tmp_path / "src"
    _write(src, "k.md", "# K\n\nCHANGED")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["skipped"] == 1 and rep["created"] == 0
    d = docs.get("k.md")
    assert d["version"] == 1 and "original" in d["content"]
    assert audit.recent(ctx.db, action="doc_import_skip")  # skip is audited


def test_import_on_conflict_overwrite_uses_update(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    docs.create(p, "k.md", "# K\n\noriginal")
    src = tmp_path / "src"
    _write(src, "k.md", "# K\n\nUPDATED")
    rep = docs.import_from_directory(p, src, into="", on_conflict="overwrite")
    assert rep["overwritten"] == 1
    d = docs.get("k.md")
    assert d["version"] == 2 and "UPDATED" in d["content"]
    assert docs.revisions("k.md")["revisions"][0]["op"] == "edit"


def test_import_on_conflict_rename_appends_dash_n(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    docs.create(p, "note.md", "# n\n\none")
    docs.create(p, "note-2.md", "# n2\n\ntwo")
    src = tmp_path / "src"
    _write(src, "note.md", "# n\n\nthree")
    rep = docs.import_from_directory(p, src, into="", on_conflict="rename")
    assert rep["renamed"] == 1
    assert docs.exists("note-3.md") and "three" in docs.get("note-3.md")["content"]
    assert "one" in docs.get("note.md")["content"]  # original untouched


def test_import_revives_tombstone(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    docs.create(p, "t.md", "# t\n\nold")
    docs.delete(p, "t.md")
    assert not docs.exists("t.md")
    src = tmp_path / "src"
    _write(src, "t.md", "# t\n\nrevived body")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["revived"] == 1
    assert "revived body" in docs.get("t.md")["content"]
    assert docs.revisions("t.md")["revisions"][0]["op"] == "create"


def test_import_idempotent_rerun(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nx")
    _write(src, "b.md", "# B\n\ny")
    r1 = docs.import_from_directory(p, src, into="docs")
    assert r1["created"] == 2
    r2 = docs.import_from_directory(p, src, into="docs")
    assert r2["created"] == 0 and r2["skipped"] == 2
    assert docs.get("docs/a.md")["version"] == 1  # no spurious version bump


# ---- embed / attachment normalization --------------------------------------
def test_import_embed_syntax_rewritten_not_graph_linked(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "n.md", "# N\n\n![[diagram.png]] and ![[Other Note]]")
    docs.import_from_directory(p, src, into="")
    body = docs.get("n.md")["content"]
    assert "![diagram.png](diagram.png)" in body  # asset embed -> image link
    assert "![[diagram.png]]" not in body
    assert "[[Other Note]]" in body               # note embed -> wikilink (still a link)
    names = [(link.get("dst_name") or "") for link in docs.links("n.md")["links"]]
    assert all("diagram" not in n for n in names)  # image never entered the graph


def test_import_attachments_copies_and_rewrites(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "post.md", "# Post\n\n![pic](img/a.png)\n\n![[b.png]]")
    (src / "img").mkdir(parents=True, exist_ok=True)
    (src / "img" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nAAAA")
    (src / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nBBBB")
    rep = docs.import_from_directory(p, src, into="", import_attachments=True)
    assert rep["attachments"]["copied"] == 2
    body = docs.get("post.md")["content"]
    assert "/attachments/a-" in body and "](img/a.png)" not in body
    assert "/attachments/b-" in body
    assert len(list((ctx.settings.vault_path / "_attachments").glob("a-*.png"))) == 1
    assert audit.recent(ctx.db, action="attachment_upload")


def test_import_attachments_count_is_zero_on_rerun(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "post.md", "# Post\n\n![pic](a.png)")
    (src / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nAAAA")
    r1 = docs.import_from_directory(p, src, into="", import_attachments=True)
    assert r1["attachments"]["copied"] == 1
    # Re-run: doc unchanged (SKIP) and the asset already on disk -> nothing new copied.
    r2 = docs.import_from_directory(p, src, into="", import_attachments=True)
    assert r2["skipped"] == 1 and r2["attachments"]["copied"] == 0


def test_import_preserves_embeds_inside_code_fences(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "doc.md",
           "# Doc\n\n```\n예: ![[diagram.png]]\n```\n\n바깥: ![[image.png]]\n")
    docs.import_from_directory(p, src, into="")
    body = docs.get("doc.md")["content"]
    assert "![[diagram.png]]" in body                  # inside code fence: untouched
    assert "![image.png](image.png)" in body           # outside: normalized
    assert "![[image.png]]" not in body


def test_import_attachment_missing_is_warned_not_failed(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "post.md", "# P\n\n![x](gone.png)")
    rep = docs.import_from_directory(p, src, into="", import_attachments=True)
    assert rep["created"] == 1
    assert any("missing asset" in w for w in rep["warnings"])
    assert "gone.png" in docs.get("post.md")["content"]  # left as-is


# ---- skip rules -------------------------------------------------------------
def test_import_skips_excluded_dirs(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "keep.md", "# Keep\n\nyes")
    _write(src, ".obsidian/cfg.md", "# x")
    _write(src, ".git/g.md", "# g")
    _write(src, "_attachments/att.md", "# a")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["scanned"] == 1 and rep["created"] == 1
    assert docs.exists("keep.md") and not docs.exists("cfg.md")


def test_import_does_not_follow_symlinks(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.md").write_text("# R\n\nreal", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Out\n\nsecret", encoding="utf-8")
    (src / "link.md").symlink_to(outside)
    (src / "loop").symlink_to(src, target_is_directory=True)  # must not hang
    rep = docs.import_from_directory(p, src, into="")
    assert docs.exists("real.md") and not docs.exists("link.md")
    assert any("symlink" in w for w in rep["warnings"])


def test_import_rejects_self_import(ctx, principals):
    with pytest.raises(ValidationError):
        ctx.docs.import_from_directory(_imp(principals), ctx.settings.vault_path, into="")


def test_import_encoding_replace_continues(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    src.mkdir()
    (src / "legacy.md").write_bytes(b"# Legacy\n\n\xff\xfe broken bytes")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["created"] == 1
    assert any("encoding replaced" in w for w in rep["warnings"])


def test_import_skips_empty_file(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "blank.md", "   \n\n  ")
    _write(src, "real.md", "# R\n\nx")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["created"] == 1 and not docs.exists("blank.md")
    assert any("empty" in w for w in rep["warnings"])


def test_import_case_collision_within_batch(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "Note.md", "# Upper\n\nfirst")
    _write(src, "note.md", "# Lower\n\nsecond")
    dry = docs.import_from_directory(p, src, into="", dry_run=True)
    real = docs.import_from_directory(p, src, into="")
    assert dry["created"] == real["created"] and dry["skipped"] == real["skipped"]
    assert real["created"] == 1 and real["skipped"] == 1
    assert any("collision" in w for w in real["warnings"])


def test_import_recurse_and_no_recurse(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "top.md", "# T\n\nt")
    _write(src, "a/b/c.md", "# C\n\nc")
    docs.import_from_directory(p, src, into="kept")
    assert docs.exists("kept/top.md") and docs.exists("kept/a/b/c.md")

    src2 = tmp_path / "src2"
    _write(src2, "x.md", "# X\n\nx")
    _write(src2, "deep/y.md", "# Y\n\ny")
    rep = docs.import_from_directory(p, src2, into="flat", recurse=False)
    assert rep["scanned"] == 1
    assert docs.exists("flat/x.md") and not docs.exists("flat/deep/y.md")


def test_import_broken_links_reported(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "n.md", "# N\n\nsee [[Missing Target]]")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["created"] == 1
    assert any("missing target" in b["target"].lower() for b in rep["broken_links"])


def test_import_no_embed_leaves_vector_dirty(ctx, principals, tmp_path):
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nsome body text to chunk and embed")
    rep = docs.import_from_directory(p, src, into="", embed=False)
    assert rep["created"] == 1 and rep["embedded"] == 0
    with ctx.db.reader() as conn:
        vd = conn.execute("SELECT vector_dirty FROM documents WHERE path_norm=?", ("a.md",)).fetchone()[0]
    assert vd == 1
    assert docs.embed_pending() >= 1
    with ctx.db.reader() as conn:
        vd2 = conn.execute("SELECT vector_dirty FROM documents WHERE path_norm=?", ("a.md",)).fetchone()[0]
    assert vd2 == 0


def test_import_too_large_skipped(ctx, principals, tmp_path, monkeypatch):
    from llm_wiki.services import documents as dm
    monkeypatch.setattr(dm, "IMPORT_MAX_BYTES", 5)
    docs, p = ctx.docs, _imp(principals)
    src = tmp_path / "src"
    _write(src, "big.md", "# Big\n\nway more than five bytes")
    rep = docs.import_from_directory(p, src, into="")
    assert rep["created"] == 0 and not docs.exists("big.md")
    assert any("too large" in w for w in rep["warnings"])


# ---- CLI wrapper ------------------------------------------------------------
def _imp_args(**over) -> SimpleNamespace:
    base = dict(from_=None, into="", on_conflict="skip", include=None, no_recurse=False,
                import_attachments=False, no_embed=False, dry_run=False, force=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_cli_import_happy_path_exit_0(ctx, principals, tmp_path, monkeypatch):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:test-operator")
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nx")
    rc = _cli_impl._import(_imp_args(from_=str(src), into="imp"))
    assert rc == 0 and ctx.docs.exists("imp/a.md")
    revision = ctx.docs.revisions("imp/a.md")["revisions"][0]
    assert revision["via"] == "cli"
    assert revision["author"] is None
    with ctx.db.reader() as conn:
        doc = conn.execute(
            "SELECT created_by, updated_by FROM documents WHERE path_norm=?",
            ("imp/a.md",),
        ).fetchone()
    assert doc["created_by"] is None and doc["updated_by"] is None
    events = audit.recent(
        ctx.db,
        actor="cli:test-operator",
        via="cli",
        action="doc_create",
    )
    assert len(events) == 1 and events[0]["target"] == "imp/a.md"


def test_cli_import_does_not_require_wiki_user(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    monkeypatch.setattr(_cli_impl, "_os_actor", lambda: "cli:test-operator")
    src = tmp_path / "src"
    _write(src, "a.md", "# A\n\nx")

    rc = _cli_impl._import(_imp_args(from_=str(src), into="imp"))

    assert rc == 0 and ctx.docs.exists("imp/a.md")


def test_cli_import_overwrite_requires_force(ctx, principals, tmp_path, monkeypatch):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    ctx.docs.create(principals["admin"], "k.md", "# K\n\norig")
    src = tmp_path / "src"
    _write(src, "k.md", "# K\n\nnew")
    rc = _cli_impl._import(_imp_args(from_=str(src), into="", on_conflict="overwrite"))
    assert rc == 1 and "orig" in ctx.docs.get("k.md")["content"]


def test_cli_import_missing_dir_exit_2(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._import(_imp_args(from_=str(tmp_path / "nope"), into=""))
    assert rc == 2


def test_cli_import_self_import_exit_2(ctx, monkeypatch):
    monkeypatch.setattr(_cli_impl, "build_context", lambda **kw: ctx)
    rc = _cli_impl._import(_imp_args(from_=str(ctx.settings.vault_path), into=""))
    assert rc == 2
