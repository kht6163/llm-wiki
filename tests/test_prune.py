"""prune: revision/audit retention that bounds DB growth (CLI: llm-wiki prune)."""
from llm_wiki.services import audit


def _rev_count(ctx, path):
    with ctx.db.reader() as conn:
        doc = conn.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
        return conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE doc_id=?", (doc["id"],)).fetchone()[0]


def test_prune_revisions_keeps_latest_n(ctx, principals):
    p = principals["editor"]
    ctx.docs.create(p, "r.md", "# R\n\nv1")
    for i in range(2, 6):  # versions 2..5
        cur = ctx.docs.get("r.md")["version"]
        ctx.docs.update(p, "r.md", cur, f"# R\n\nv{i}")
    assert _rev_count(ctx, "r.md") == 5

    rep = ctx.docs.prune_revisions(keep=2, apply=False)   # dry run counts, deletes nothing
    assert rep["deletable_revisions"] == 3 and rep["applied"] is False
    assert _rev_count(ctx, "r.md") == 5

    rep = ctx.docs.prune_revisions(keep=2, apply=True)
    assert rep["applied"] is True
    assert _rev_count(ctx, "r.md") == 2
    assert ctx.docs.get("r.md")["content"] == "# R\n\nv5"  # latest body intact


def test_prune_revisions_keep_floor_is_one(ctx, principals):
    ctx.docs.create(principals["editor"], "k.md", "# K\n\nonly")
    rep = ctx.docs.prune_revisions(keep=0, apply=True)     # forced to >=1
    assert rep["keep"] == 1
    assert _rev_count(ctx, "k.md") == 1
    assert ctx.docs.get("k.md")["content"] == "# K\n\nonly"


def test_audit_prune_deletes_old_rows_only(ctx):
    with ctx.db.writer() as conn:
        conn.execute("INSERT INTO audit_log(ts, actor, via, action, target, outcome, detail) "
                     "VALUES('2020-01-01T00:00:00Z','x','cli','doc_create',NULL,'ok',NULL)")
        conn.execute("INSERT INTO audit_log(ts, actor, via, action, target, outcome, detail) "
                     "VALUES('2999-01-01T00:00:00Z','x','cli','doc_create',NULL,'ok',NULL)")
    rep = audit.prune(ctx.db, older_than_days=30, apply=False)
    assert rep["deletable_events"] == 1 and rep["applied"] is False

    rep = audit.prune(ctx.db, older_than_days=30, apply=True)
    assert rep["applied"] is True
    with ctx.db.reader() as conn:
        rows = conn.execute("SELECT ts FROM audit_log").fetchall()
    assert [r["ts"] for r in rows] == ["2999-01-01T00:00:00Z"]  # only future row survives
