from pathlib import Path

import pytest

from llm_wiki.services.errors import ForbiddenError, NotFoundError


def _state(ctx, path: str) -> tuple[int, int, int, str]:
    with ctx.db.reader() as conn:
        doc_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm=lower(?)", (path,)
        ).fetchone()["id"]
        revisions = conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    return ctx.docs.get(path)["version"], revisions, audit, Path(ctx.docs.vault, path).read_text()


def test_merge_preview_uses_exact_revision_and_does_not_mutate(ctx, principals, monkeypatch):
    docs = ctx.docs
    editor = principals["editor"]
    base = "one\ntwo\nthree\n"
    mine = "ONE\ntwo\nthree\n"
    current = "one\ntwo\nTHREE\n"
    docs.create(editor, "merge.md", base)
    docs.update(editor, "merge.md", 1, current)
    before = _state(ctx, "merge.md")

    calls = []
    from llm_wiki import merge as merge_module
    from llm_wiki.services import documents as documents_module

    real_merge = merge_module.three_way_merge

    def tracked(base_text, mine_text, current_text):
        calls.append((base_text, mine_text, current_text))
        return real_merge(base_text, mine_text, current_text)

    monkeypatch.setattr(documents_module, "three_way_merge", tracked)
    preview = docs.merge_preview(editor, "merge.md", 1, mine)
    current_updated_at = docs.get("merge.md")["updated_at"]

    assert calls == [(base, mine, current)]
    assert preview == {
        "base_version": 1,
        "current_version": 2,
        "updated_by": "alice",
        "updated_at": current_updated_at,
        "current_via": "?",
        "base": base,
        "mine": mine,
        "current": current,
        "merged": "ONE\ntwo\nTHREE\n",
        "conflicts": [],
        "manual_only": False,
    }
    assert _state(ctx, "merge.md") == before


@pytest.mark.parametrize(
    ("base", "mine", "current", "expected"),
    [
        (
            "one\ntwo\nthree\n",
            "one\nMINE\nthree\n",
            "one\nCURRENT\nthree\n",
            {
                "start_line": 2,
                "base": "two\n",
                "mine": "MINE\n",
                "current": "CURRENT\n",
                "resolved": None,
                "merged_start": 4,
            },
        ),
        (
            "one\ntwo\nthree\n",
            "one\nthree\n",
            "one\nTWO\nthree\n",
            {
                "start_line": 2,
                "base": "two\n",
                "mine": "",
                "current": "TWO\n",
                "resolved": None,
                "merged_start": 4,
            },
        ),
    ],
)
def test_merge_preview_serializes_ordered_conflicts(
    ctx, principals, base, mine, current, expected
):
    docs = ctx.docs
    editor = principals["editor"]
    docs.create(editor, "overlap.md", base)
    docs.update(editor, "overlap.md", 1, current)

    preview = docs.merge_preview(editor, "overlap.md", 1, mine)

    assert preview["merged"] == base
    assert preview["conflicts"] == [expected]
    assert preview["manual_only"] is False


@pytest.mark.parametrize(
    ("base", "mine", "current", "merged", "expected_start", "expected_base"),
    [
        (
            "repeat\nanchor\nrepeat\ntail\n",
            "repeat\nanchor\nMINE\ntail\n",
            "repeat\nanchor\nCURRENT\ntail\n",
            "repeat\nanchor\nrepeat\ntail\n",
            14,
            "repeat\n",
        ),
        (
            "head\nanchor\n",
            "HEAD\nanchor\nmine\n",
            "head\nanchor\ncurrent\n",
            "HEAD\nanchor\n",
            12,
            "",
        ),
    ],
)
def test_merge_preview_preserves_exact_engine_placeholder_offsets(
    ctx, principals, base, mine, current, merged, expected_start, expected_base
):
    docs = ctx.docs
    editor = principals["editor"]
    docs.create(editor, "offsets.md", base)
    docs.update(editor, "offsets.md", 1, current)

    preview = docs.merge_preview(editor, "offsets.md", 1, mine)

    assert preview["merged"] == merged
    assert preview["conflicts"][0]["merged_start"] == expected_start
    assert preview["conflicts"][0]["base"] == expected_base


def test_merge_preview_missing_base_is_explicit_manual_fallback(ctx, principals, monkeypatch):
    docs = ctx.docs
    editor = principals["editor"]
    docs.create(editor, "pruned.md", "base")
    docs.update(editor, "pruned.md", 1, "current")
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM revisions WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?) "
            "AND version=1",
            ("pruned.md",),
        )

    def forbidden_call(*_args):
        raise AssertionError("merge engine must not run without the exact base")

    from llm_wiki.services import documents as documents_module

    monkeypatch.setattr(documents_module, "three_way_merge", forbidden_call)
    preview = docs.merge_preview(editor, "pruned.md", 1, "mine")
    current_updated_at = docs.get("pruned.md")["updated_at"]

    assert preview == {
        "base_version": 1,
        "current_version": 2,
        "updated_by": "alice",
        "updated_at": current_updated_at,
        "current_via": "?",
        "base": None,
        "mine": "mine",
        "current": "current",
        "merged": None,
        "conflicts": [],
        "manual_only": True,
    }


def test_merge_preview_enforces_write_authorization_and_document_existence(ctx, principals):
    docs = ctx.docs
    docs.create(principals["editor"], "private.md", "body")

    with pytest.raises(ForbiddenError):
        docs.merge_preview(principals["viewer"], "private.md", 1, "mine")
    with pytest.raises(NotFoundError):
        docs.merge_preview(principals["editor"], "missing.md", 1, "mine")


def test_merge_preview_rejects_a_missing_current_revision(ctx, principals):
    docs = ctx.docs
    editor = principals["editor"]
    docs.create(editor, "corrupt.md", "current")
    with ctx.db.writer() as conn:
        conn.execute(
            "DELETE FROM revisions WHERE doc_id=(SELECT id FROM documents WHERE path_norm=?)",
            ("corrupt.md",),
        )

    with pytest.raises(RuntimeError, match="current document revision is missing or corrupt"):
        docs.merge_preview(editor, "corrupt.md", 1, "mine")
