"""Link-graph BFS edge cases (batch B): depth bounds, cycles, unresolved ghosts,
truncation, and a missing root."""


def test_bfs_depth_bounds(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "A.md", "[[B]]")
    docs.create(p, "B.md", "[[C]]")
    docs.create(p, "C.md", "[[D]]")
    docs.create(p, "D.md", "leaf")

    d1 = {n["id"] for n in docs.graph(root="A.md", depth=1)["nodes"]}
    assert "B.md" in d1 and "C.md" not in d1
    d2 = {n["id"] for n in docs.graph(root="A.md", depth=2)["nodes"]}
    assert "C.md" in d2 and "D.md" not in d2
    d3 = {n["id"] for n in docs.graph(root="A.md", depth=3)["nodes"]}
    assert "D.md" in d3


def test_cycle_terminates(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "x.md", "[[y]]")
    docs.create(p, "y.md", "[[x]]")
    g = docs.graph(root="x.md", depth=3)
    assert {n["id"] for n in g["nodes"]} == {"x.md", "y.md"}


def test_unresolved_ghost_toggle(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "A.md", "[[B]] and [[ghosttarget]]")
    docs.create(p, "B.md", "x")
    g_on = docs.graph(root="A.md", depth=1, include_unresolved=True)
    assert any(n["id"].startswith("unresolved:") for n in g_on["nodes"])
    g_off = docs.graph(root="A.md", depth=1, include_unresolved=False)
    assert not any(n["id"].startswith("unresolved:") for n in g_off["nodes"])


def test_truncation_flag(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    for i in range(5):
        docs.create(p, f"n{i}.md", "x")
    g = docs.graph(root=None, depth=1, limit=2)
    assert g["truncated"] is True
    assert len(g["nodes"]) <= 2


def test_missing_root_is_empty(ctx, principals):
    docs = ctx.docs
    g = docs.graph(root="nope.md")
    assert g["nodes"] == [] and g["edges"] == [] and g["truncated"] is False
