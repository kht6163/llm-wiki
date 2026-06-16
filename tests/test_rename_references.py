"""rename_references: rewriting stale link TEXT after a document moves, so a move
doesn't leave dangling references behind."""
import pytest

from llm_wiki.services.errors import ForbiddenError


def _broken_targets(docs):
    return {b["target"] for b in docs.broken_links()["links"]}


def test_path_form_links_rewritten_on_move(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "notes/old.md", "# Old\n\nbody")
    docs.create(p, "ref.md", "see [[notes/old]] and [link](notes/old.md) here")
    # A plain move re-resolves the graph but leaves the body text stale -> broken.
    docs.move(p, "notes/old.md", "archive/new.md")
    assert _broken_targets(docs)

    out = docs.rename_references(p, "notes/old.md", "archive/new.md")
    assert out["docs_rewritten"] == 1 and out["links_rewritten"] == 2
    body = docs.get("ref.md")["content"]
    assert "[[archive/new]]" in body and "[link](archive/new.md)" in body
    assert "old" not in body                 # no stale reference remains
    assert not _broken_targets(docs)         # references resolve again


def test_move_with_fix_references(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "# A\n\nx")
    docs.create(p, "b.md", "points to [[a]] and [md](a.md)")
    res = docs.move(p, "a.md", "sub/a.md", fix_references=True)
    assert res["references"]["docs_rewritten"] == 1
    body = docs.get("b.md")["content"]
    # Bare [[a]] still resolves by stem -> left intact; the path-form link repointed.
    assert "[md](sub/a.md)" in body
    assert not _broken_targets(docs)


def test_bare_name_rewritten_when_stem_changes(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "Note.md", "# Note\n\nx")
    docs.create(p, "c.md", "look at [[Note]]")
    docs.move(p, "Note.md", "Renamed.md", fix_references=True)
    body = docs.get("c.md")["content"]
    assert "[[Renamed]]" in body and "[[Note]]" not in body
    assert not _broken_targets(docs)


def test_alias_and_anchor_preserved(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "g.md", "# G\n\nx")
    docs.create(p, "h.md", "[[g#Intro|the guide]] and [txt](g.md#sec)")
    docs.move(p, "g.md", "guide.md", fix_references=True)
    body = docs.get("h.md")["content"]
    assert "[[guide#Intro|the guide]]" in body
    assert "[txt](guide.md#sec)" in body


def test_bare_name_resolving_elsewhere_is_left_alone(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    # Two docs share the bare name "dup" in different folders.
    docs.create(p, "x/dup.md", "# Dup X\n\none")
    docs.create(p, "y/dup.md", "# Dup Y\n\ntwo")
    docs.create(p, "z.md", "precise [[y/dup]] and bare [[dup]]")
    docs.move(p, "y/dup.md", "y/moved.md", fix_references=True)
    body = docs.get("z.md")["content"]
    assert "[[y/moved]]" in body     # path-form repointed
    assert "[[dup]]" in body         # bare name still resolves to x/dup -> untouched
    assert not _broken_targets(docs)


def test_viewer_cannot_rename_references(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "v.md", "x")
    with pytest.raises(ForbiddenError):
        docs.rename_references(principals["viewer"], "v.md", "w.md")
