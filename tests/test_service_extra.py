"""Service-layer invariants for the newly added surface and a few security/
durability paths that previously had no coverage."""
import pytest

from llm_wiki.services import users as users_svc
from llm_wiki.services.errors import ConflictError, ValidationError
from llm_wiki.util import PathError, normalize_rel_path, safe_join


# -- path safety -----------------------------------------------------------
@pytest.mark.parametrize("bad", ["../secrets.md", "a/../../b.md", "..\\win.md"])
def test_normalize_rejects_traversal(bad):
    with pytest.raises(PathError):
        normalize_rel_path(bad)


def test_absolute_path_is_contained_not_escaped(tmp_path):
    # A leading slash is stripped (treated vault-relative), so it cannot reach /etc.
    rel = normalize_rel_path("/etc/passwd")
    assert rel == "etc/passwd.md"
    assert safe_join(tmp_path, rel).is_relative_to(tmp_path.resolve())


def test_safe_join_blocks_escape(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert safe_join(vault, "ok/note.md").is_relative_to(vault.resolve())
    with pytest.raises(PathError):
        safe_join(vault, "../escape.md")


# -- count / tags ----------------------------------------------------------
def test_count_and_tags(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "---\ntags: [x, y]\n---\nbody", tags=["x", "y"])
    docs.create(p, "b.md", "body", tags=["y"])
    assert docs.count() == 2
    assert docs.count(tag="y") == 2
    assert docs.count(tag="x") == 1
    tags = {t["tag"]: t["count"] for t in docs.tags()}
    assert tags["y"] == 2 and tags["x"] == 1


def test_deleted_docs_excluded_from_count_and_tags(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "gone.md", "body", tags=["z"])
    docs.delete(p, "gone.md")
    assert docs.count() == 0
    assert all(t["tag"] != "z" for t in docs.tags())


# -- revision restore (the mechanism the web rollback route uses) ----------
def test_restore_revision_writes_new_version(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "r.md", "first")
    docs.update(p, "r.md", 1, "second")
    rev = docs.revision("r.md", 1)
    cur = docs.get("r.md")
    out = docs.update(p, "r.md", cur["version"], rev["content"], title=rev["title"])
    assert out["version"] == 3
    assert docs.get("r.md")["content"] == "first"


def test_restore_conflict_when_changed_meanwhile(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "c.md", "v1")
    docs.update(p, "c.md", 1, "v2")
    # Simulate a stale restore: base_version no longer current.
    with pytest.raises(ConflictError):
        docs.update(p, "c.md", 1, "restore of v1")


# -- last-admin guard ------------------------------------------------------
def test_cannot_demote_or_delete_last_admin(ctx, principals):
    admin_id = principals["admin"].user_id
    with pytest.raises(ValidationError):
        users_svc.set_role(ctx.db, admin_id, "editor")
    with pytest.raises(ValidationError):
        users_svc.set_active(ctx.db, admin_id, False)
    with pytest.raises(ValidationError):
        users_svc.delete_user(ctx.db, admin_id)


def test_password_policy_min_length(ctx):
    from llm_wiki.services.auth import create_user
    with pytest.raises(ValidationError):
        create_user(ctx.db, "shorty", "1234567", "viewer")  # 7 chars
    uid = create_user(ctx.db, "okuser", "12345678", "viewer")  # 8 chars
    assert uid > 0


# -- targeted edits: section / patch / move --------------------------------
def test_append_and_replace_section(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "s.md", "# Title\n\n## Notes\nfirst\n\n## Refs\nlink\n")
    out = docs.append_section(p, "s.md", "Notes", "second")
    assert "first" in out["content"] and "second" in out["content"]
    # append targets the right section (before "## Refs")
    assert out["content"].index("second") < out["content"].index("## Refs")
    out = docs.replace_section(p, "s.md", "Notes", "rewritten")
    assert "rewritten" in out["content"] and "first" not in out["content"]
    assert "link" in out["content"]  # other section untouched
    sec = docs.get_section("s.md", "Refs")
    assert "link" in sec["content"] and sec["content"].startswith("## Refs")


def test_section_not_found(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "s2.md", "# T\n\n## A\nx\n")
    from llm_wiki.services.errors import NotFoundError
    with pytest.raises(NotFoundError):
        docs.append_section(p, "s2.md", "Nonexistent", "y")


def test_patch_unique_ambiguous_and_missing(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "p.md", "alpha beta alpha")
    with pytest.raises(ValidationError):  # appears twice, count=1
        docs.patch(p, "p.md", "alpha", "X")
    out = docs.patch(p, "p.md", "beta", "BETA")
    assert "BETA" in out["content"]
    from llm_wiki.services.errors import NotFoundError
    with pytest.raises(NotFoundError):
        docs.patch(p, "p.md", "missing", "Y")


def test_move_document_and_links(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "old.md", "# Old\n\nbody")
    out = docs.move(p, "old.md", "sub/new.md")
    assert out["path"] == "sub/new.md"
    assert not docs.exists("old.md") and docs.exists("sub/new.md")
    # a rename revision was recorded
    ops = {r["op"] for r in docs.revisions("sub/new.md")["revisions"]}
    assert "rename" in ops
    # moving onto an existing path is rejected
    docs.create(p, "taken.md", "x")
    with pytest.raises(ConflictError):
        docs.move(p, "sub/new.md", "taken.md")


def test_recent_changes_window(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "r1.md", "one")
    docs.create(p, "r2.md", "two")
    recent = docs.recent_changes(limit=10)
    assert {d["path"] for d in recent} >= {"r1.md", "r2.md"}
    # an impossible window returns nothing
    assert docs.recent_changes(limit=10, until="1990-01-01T00:00:00Z") == []


def test_audit_log_records_writes(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "a.md", "body")
    docs.delete(p, "a.md")
    from llm_wiki.services import audit
    actions = {row["action"] for row in audit.recent(ctx.db)}
    assert {"doc_create", "doc_delete"} <= actions
