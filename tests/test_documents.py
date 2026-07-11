import pytest

from llm_wiki.services.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)


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


def test_explicit_title_and_tags_survive_body_only_update(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(
        p, "note.md", "plain body", title="Explicit", tags=["kept"], embed=False
    )

    updated = docs.update(
        p, "note.md", created["version"], "plain body changed", embed=False
    )

    assert updated["title"] == "Explicit"
    assert updated["tags"] == ["kept"]


def test_content_title_replaces_preserved_title_on_update(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(p, "note.md", "plain body", title="Explicit", embed=False)

    updated = docs.update(
        p, "note.md", created["version"], "# Derived\n\nplain body changed", embed=False
    )

    assert updated["title"] == "Derived"


@pytest.mark.parametrize(
    ("content", "expected_tags"),
    [
        ("---\ntags: [frontmatter]\n---\nplain body changed", ["frontmatter"]),
        ("plain body changed #inline", ["inline"]),
    ],
)
def test_content_tags_replace_preserved_tags_on_update(
    ctx, principals, content, expected_tags
):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(p, "note.md", "plain body", tags=["kept"], embed=False)

    updated = docs.update(p, "note.md", created["version"], content, embed=False)

    assert updated["tags"] == expected_tags


@pytest.mark.parametrize(
    "content",
    [
        "---\ntags: []\n---\nplain body changed",
        "---\ntags:\n---\nplain body changed",
    ],
)
def test_empty_content_tags_preserve_explicit_tags(ctx, principals, content):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(p, "note.md", "plain body", tags=["kept"], embed=False)

    updated = docs.update(p, "note.md", created["version"], content, embed=False)

    assert updated["tags"] == ["kept"]


def test_explicit_update_metadata_wins_over_content_metadata(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    created = docs.create(
        p, "note.md", "plain body", title="Old", tags=["old"], embed=False
    )

    updated = docs.update(
        p,
        "note.md",
        created["version"],
        "---\ntitle: Frontmatter\ntags: [frontmatter]\n---\n"
        "# Heading\n\nplain body changed #inline",
        title="Explicit",
        tags=["argument"],
        embed=False,
    )

    assert updated["title"] == "Explicit"
    assert updated["tags"] == ["argument", "frontmatter", "inline"]


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


# ---- frontmatter property editing -----------------------------------------
def test_set_property_adds_and_updates_through_cas(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "# P\n\nbody")
    d = docs.set_property(p, "p.md", "status", "draft")
    assert d["version"] == 2 and "status: draft" in d["content"]
    d2 = docs.set_property(p, "p.md", "status", "done")
    assert "status: done" in d2["content"] and "draft" not in d2["content"]
    # body preserved across property edits
    assert "body" in d2["content"]


def test_set_property_list_value_becomes_inline_list(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "# P\n\nbody")
    d = docs.set_property(p, "p.md", "aliases", ["별명1", "별명2"])
    assert "aliases: [별명1, 별명2]" in d["content"]


def test_set_property_empty_value_removes_key(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "---\nstatus: draft\n---\nbody")
    d = docs.set_property(p, "p.md", "status", "")
    assert "status" not in d["content"]


def test_remove_property_is_idempotent_noop(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    doc = docs.create(p, "p.md", "# P\n\nbody")
    d = docs.remove_property(p, "p.md", "status")  # absent -> no version bump
    assert d["version"] == doc["version"]


def test_property_key_title_and_tags_are_reserved(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "# P\n\nbody")
    for reserved in ("title", "tags"):
        with pytest.raises(ValidationError):
            docs.set_property(p, "p.md", reserved, "x")


def test_property_key_must_be_simple(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "# P\n\nbody")
    with pytest.raises(ValidationError):
        docs.set_property(p, "p.md", "bad key!", "x")


def test_viewer_cannot_edit_properties(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "p.md", "# P\n\nbody")
    with pytest.raises(ForbiddenError):
        docs.set_property(principals["viewer"], "p.md", "status", "draft")


def test_replace_properties_drops_omitted_keeps_reserved(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "---\ntitle: T\ntags: [x]\nstatus: draft\nauthor: kim\n---\nbody",
                tags=["x"])
    # keep status (changed), drop author, add due; title/tags untouched
    d = docs.replace_properties(p, "p.md", [("status", ["done"]), ("due", ["2026-07-01"])])
    assert "status: done" in d["content"]
    assert "author" not in d["content"]
    assert "due: 2026-07-01" in d["content"]
    assert "title: T" in d["content"] and "x" in d["tags"]


def test_property_edit_conflicts_on_stale_base_version(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "p.md", "# P\n\nbody")           # v1
    docs.update(p, "p.md", base_version=1, content="# P\n\nchanged")  # v2
    with pytest.raises(ConflictError):
        docs.set_property(p, "p.md", "status", "draft", base_version=1)


def test_backlink_context_snippet(ctx, principals):
    # with_context adds a 'context' snippet sliced from the source body at the link
    # offset (links.char_start indexes the full stored body), so a caller sees WHY a
    # doc links here without re-reading it; default omits it.
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "target.md", "# Target\n\nbody")
    docs.create(p, "src.md",
                "# Src\n\nIntro sentence. See [[target]] for the full story. Trailing text.")
    plain = docs.backlinks("target.md")["backlinks"]
    assert plain and "context" not in plain[0]
    withctx = docs.backlinks("target.md", with_context=True)["backlinks"]
    b = next(x for x in withctx if x["src_path"] == "src.md")
    assert "target" in b["context"] and "full story" in b["context"]
