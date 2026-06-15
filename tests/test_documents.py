import pytest

from llm_wiki.services.errors import ConflictError, ForbiddenError, NotFoundError


def test_create_read_update_version(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    doc = docs.create(p, "notes/hello.md", "# Hello\n\nworld", tags=["greeting"])
    assert doc["version"] == 1
    assert doc["path"] == "notes/hello.md"
    assert "greeting" in doc["tags"]

    got = docs.get("notes/hello")  # .md auto-appended, case-insensitive resolution
    assert got["version"] == 1
    assert "world" in got["content"]

    updated = docs.update(p, "notes/hello.md", base_version=1, content="# Hello\n\nupdated")
    assert updated["version"] == 2
    assert "updated" in docs.get("notes/hello.md")["content"]

    # the .md file is materialized on disk
    assert (ctx.settings.vault_path / "notes" / "hello.md").read_text(encoding="utf-8").endswith("updated")


def test_optimistic_conflict(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "a.md", "v1 content")
    docs.update(p, "a.md", base_version=1, content="v2 content")  # now version 2

    with pytest.raises(ConflictError) as ei:
        docs.update(p, "a.md", base_version=1, content="stale write")  # stale base_version
    err = ei.value
    assert err.code == "conflict"
    assert err.extra["current_version"] == 2
    assert "v2 content" in err.extra["current_content"]
    # the rejected write did not change anything
    assert docs.get("a.md")["version"] == 2
    assert "v2 content" in docs.get("a.md")["content"]


def test_create_duplicate_conflicts(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "dup.md", "one")
    with pytest.raises(ConflictError):
        docs.create(p, "dup.md", "two")


def test_viewer_cannot_write(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "x.md", "hi")
    with pytest.raises(ForbiddenError):
        docs.create(principals["viewer"], "y.md", "nope")
    with pytest.raises(ForbiddenError):
        docs.update(principals["viewer"], "x.md", base_version=1, content="nope")


def test_delete_soft(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "tmp.md", "bye")
    docs.delete(p, "tmp.md")
    with pytest.raises(NotFoundError):
        docs.get("tmp.md")
    # recreating on the tombstone works
    doc = docs.create(p, "tmp.md", "reborn")
    assert doc["version"] >= 2


def test_links_backlinks_and_resolution(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    # B links to A before A exists -> dangling, then backfilled on A creation
    docs.create(p, "B.md", "see [[A]] for details")
    links_b = docs.links("B.md")["links"]
    assert links_b and links_b[0]["is_resolved"] == 0

    docs.create(p, "A.md", "# A\n\ncontent")
    back = docs.backlinks("A.md")["backlinks"]
    assert any(b["src_path"] == "B.md" for b in back)

    g = docs.graph(root="A.md", depth=1)
    ids = {n["id"] for n in g["nodes"]}
    assert "A.md" in ids and "B.md" in ids


def test_revisions(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "r.md", "one")
    docs.update(p, "r.md", base_version=1, content="two")
    hist = docs.revisions("r.md")
    assert hist["current_version"] == 2
    assert len(hist["revisions"]) == 2
    rev1 = docs.revision("r.md", 1)
    assert rev1["content"] == "one"
