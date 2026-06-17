"""Sidebar nav cache: render() reads a generation-invalidated snapshot of the file tree
+ top tags, so the full vault scan is paid once per structural write, not once per page.
These guard that every structural write invalidates it and pure reads never do."""


def _doc_paths(node):
    out = [d["path"] for d in node["docs"]]
    for f in node["folders"]:
        out += _doc_paths(f)
    return out


def test_nav_cache_caches_between_reads(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "# A\n\nbody")
    t1 = docs.nav_tree()
    t2 = docs.nav_tree()
    assert t1 is t2  # no write between the two reads -> same cached object, no rebuild
    assert "a.md" in _doc_paths(t1)


def test_nav_cache_invalidates_on_create(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "body")
    t1 = docs.nav_tree()
    docs.create(p, "b.md", "body")
    t2 = docs.nav_tree()
    assert t1 is not t2  # the create bumped the generation -> rebuilt
    assert "b.md" in _doc_paths(t2)


def test_nav_cache_invalidates_on_every_structural_write(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "x.md", "# X\n\nbody")

    def bumps(fn):
        before = docs._nav_gen
        fn()
        assert docs._nav_gen > before

    bumps(lambda: docs.update(p, "x.md", docs.get("x.md")["version"], "# X2\n\nbody", title="X2"))
    bumps(lambda: docs.move(p, "x.md", "y.md"))
    bumps(lambda: docs.create_folder(p, "F"))
    bumps(lambda: docs.delete_folder(p, "F"))
    bumps(lambda: docs.delete(p, "y.md"))


def test_nav_cache_reads_do_not_invalidate(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "r.md", "body with [[a]]")
    docs.nav_tree()
    gen = docs._nav_gen
    docs.get("r.md")
    docs.backlinks("r.md")
    docs.tree()       # the uncached public scan must not bump either
    docs.nav_tree()   # a cache read must not bump
    assert docs._nav_gen == gen


def test_nav_tags_reflects_tag_changes(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "tg.md", "body")
    docs.patch_tags(p, "tg.md", add=["alpha"])           # funnels through update() -> bumps
    assert any(t["tag"] == "alpha" for t in docs.nav_tags())
