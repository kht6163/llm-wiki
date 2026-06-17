"""Agent-write primitives that funnel through the CAS update path:
append_to_document, patch (literal/regex + occurrence), and restore_revision.
"""
import pytest

from llm_wiki.services.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError


@pytest.fixture
def docs(ctx):
    return ctx.docs


# ---- append_to_document --------------------------------------------------
def test_append_to_end_of_document(docs, principals):
    p = principals["editor"]
    docs.create(p, "log.md", "# Log\n\nfirst\n")
    docs.append_to_document(p, "log.md", "second")
    body = docs.get("log.md")["content"]
    assert body.rstrip().endswith("second")
    assert "first" in body and body.count("second") == 1


def test_append_under_existing_heading(docs, principals):
    p = principals["editor"]
    docs.create(p, "n.md", "# T\n\n## Notes\nalpha\n\n## Other\nz\n")
    docs.append_to_document(p, "n.md", "beta", ensure_heading="Notes")
    body = docs.get("n.md")["content"]
    # 'beta' lands inside the Notes section (before '## Other'), not at the very end.
    assert body.index("beta") < body.index("## Other")
    assert body.index("alpha") < body.index("beta")


def test_append_creates_missing_heading(docs, principals):
    p = principals["editor"]
    docs.create(p, "j.md", "# Journal\n\nintro\n")
    docs.append_to_document(p, "j.md", "entry one", ensure_heading="2026-06-16")
    body = docs.get("j.md")["content"]
    assert "## 2026-06-16" in body
    assert body.index("## 2026-06-16") < body.index("entry one")


def test_append_rejects_empty_and_viewer(docs, principals):
    p = principals["editor"]
    docs.create(p, "e.md", "x")
    with pytest.raises(ValidationError):
        docs.append_to_document(p, "e.md", "   ")
    with pytest.raises(ForbiddenError):
        docs.append_to_document(principals["viewer"], "e.md", "nope")


# ---- append idempotency (retry-safe) ------------------------------------
def test_append_without_key_appends_each_time(docs, principals):
    p = principals["editor"]
    docs.create(p, "log.md", "# Log\n")
    docs.append_to_document(p, "log.md", "entry")
    docs.append_to_document(p, "log.md", "entry")
    assert docs.get("log.md")["content"].count("entry") == 2  # no key => not deduped


def test_append_with_idempotency_key_dedups_retry(docs, principals):
    p = principals["editor"]
    docs.create(p, "log.md", "# Log\n")
    first = docs.append_to_document(p, "log.md", "entry", idempotency_key="k1")
    v1 = first["version"]
    # A retry with the SAME key returns the prior result and does not append again.
    again = docs.append_to_document(p, "log.md", "entry", idempotency_key="k1")
    assert again["deduplicated"] is True
    assert again["version"] == v1
    assert docs.get("log.md")["content"].count("entry") == 1
    assert docs.get("log.md")["version"] == v1


def test_append_distinct_keys_both_apply(docs, principals):
    p = principals["editor"]
    docs.create(p, "log.md", "# Log\n")
    docs.append_to_document(p, "log.md", "entry", idempotency_key="k1")
    docs.append_to_document(p, "log.md", "entry", idempotency_key="k2")
    assert docs.get("log.md")["content"].count("entry") == 2  # different keys => both land


def test_append_key_is_scoped_per_user(docs, principals):
    # The same key string used by two users must not collide.
    a, b = principals["editor"], principals["admin"]
    docs.create(a, "log.md", "# Log\n")
    docs.append_to_document(a, "log.md", "alice", idempotency_key="shared")
    docs.append_to_document(b, "log.md", "admin", idempotency_key="shared")
    body = docs.get("log.md")["content"]
    assert "alice" in body and "admin" in body


# ---- patch: occurrence + regex ------------------------------------------
def test_patch_literal_targets_nth_occurrence(docs, principals):
    p = principals["editor"]
    docs.create(p, "rep.md", "x\nx\nx\n")          # 'x' three times
    docs.patch(p, "rep.md", "x", "Y", occurrence=2)
    assert docs.get("rep.md")["content"] == "x\nY\nx\n"


def test_patch_literal_too_many_without_occurrence(docs, principals):
    p = principals["editor"]
    docs.create(p, "many.md", "a a a")
    with pytest.raises(ValidationError):
        docs.patch(p, "many.md", "a", "b")          # 3 matches, count=1 -> ambiguous


def test_patch_regex_replaces_with_backref(docs, principals):
    p = principals["editor"]
    docs.create(p, "rx.md", "value: 41\n")
    docs.patch(p, "rx.md", r"value: (\d+)", r"value: \1!", mode="regex")
    assert "value: 41!" in docs.get("rx.md")["content"]


def test_patch_regex_occurrence_and_multiline(docs, principals):
    p = principals["editor"]
    docs.create(p, "rl.md", "- [ ] a\n- [ ] b\n- [ ] c\n")
    docs.patch(p, "rl.md", r"^- \[ \]", "- [x]", mode="regex", occurrence=3)
    assert docs.get("rl.md")["content"] == "- [ ] a\n- [ ] b\n- [x] c\n"


def test_patch_regex_no_match_and_bad_pattern(docs, principals):
    p = principals["editor"]
    docs.create(p, "g.md", "hello")
    with pytest.raises(NotFoundError):
        docs.patch(p, "g.md", r"\d+", "X", mode="regex")
    with pytest.raises(ValidationError):
        docs.patch(p, "g.md", r"(unclosed", "X", mode="regex")


def test_patch_occurrence_out_of_range(docs, principals):
    p = principals["editor"]
    docs.create(p, "o.md", "k k")
    with pytest.raises(ValidationError):
        docs.patch(p, "o.md", "k", "Z", occurrence=5)


# ---- restore_revision ----------------------------------------------------
def test_restore_revision_replays_old_body(docs, principals):
    p = principals["editor"]
    docs.create(p, "r.md", "v1 body")
    docs.update(p, "r.md", 1, "v2 body")
    out = docs.restore_revision(p, "r.md", 1)
    assert out["content"] == "v1 body"
    assert out["version"] == 3  # restore is a new edit on top of v2


def test_restore_unknown_revision_and_viewer(docs, principals):
    p = principals["editor"]
    docs.create(p, "u.md", "only")
    with pytest.raises(NotFoundError):
        docs.restore_revision(p, "u.md", 99)
    with pytest.raises(ForbiddenError):
        docs.restore_revision(principals["viewer"], "u.md", 1)


def test_restore_with_stale_base_version_conflicts(docs, principals):
    p = principals["editor"]
    docs.create(p, "c.md", "one")
    docs.update(p, "c.md", 1, "two")   # now at v2
    with pytest.raises(ConflictError):
        docs.restore_revision(p, "c.md", 1, base_version=1)  # stale guard
