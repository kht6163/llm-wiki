from dataclasses import FrozenInstanceError

import pytest

from llm_wiki.merge import MergeHunk, MergeResult, three_way_merge


def test_result_models_are_frozen_and_minimal() -> None:
    hunk = MergeHunk(1, "base", "mine", "current", None)
    result = MergeResult("base", (hunk,))

    assert result == MergeResult(text="base", conflicts=(hunk,))
    with pytest.raises(FrozenInstanceError):
        hunk.resolved = "mine"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.text = "mine"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("base", "mine", "current", "expected"),
    [
        ("alpha\nbeta\n", "ALPHA\nbeta\n", "alpha\nbeta\n", "ALPHA\nbeta\n"),
        ("alpha\nbeta\n", "alpha\nbeta\n", "alpha\nBETA\n", "alpha\nBETA\n"),
        ("alpha\n", "same\n", "same\n", "same\n"),
        ("", "", "", ""),
        ("", "created", "", "created"),
    ],
)
def test_invariant_shortcuts_preserve_the_selected_text(
    base: str, mine: str, current: str, expected: str
) -> None:
    assert three_way_merge(base, mine, current) == MergeResult(expected, ())


def test_disjoint_and_adjacent_edits_auto_merge() -> None:
    base = "one\ntwo\nthree\nfour\n"

    result = three_way_merge(base, "ONE\ntwo\nthree\nfour\n", "one\nTWO\nthree\nFOUR\n")

    assert result == MergeResult("ONE\nTWO\nthree\nFOUR\n", ())


def test_identical_overlapping_edit_auto_merges() -> None:
    base = "one\ntwo\nthree\n"
    changed = "one\nTWO AND A HALF\nthree\n"

    assert three_way_merge(base, changed, changed) == MergeResult(changed, ())


def test_ambiguous_overlap_keeps_base_text_and_reports_unresolved_hunk() -> None:
    result = three_way_merge("one\ntwo\nthree\n", "one\nMINE\nthree\n", "one\nTHEIRS\nthree\n")

    assert result.text == "one\ntwo\nthree\n"
    assert result.conflicts == (MergeHunk(2, "two\n", "MINE\n", "THEIRS\n", None),)
    assert "<<<<<<<" not in result.text


def test_delete_versus_edit_is_a_conflict() -> None:
    result = three_way_merge("one\ntwo\nthree\n", "one\nthree\n", "one\nTWO\nthree\n")

    assert result == MergeResult(
        "one\ntwo\nthree\n",
        (MergeHunk(2, "two\n", "", "TWO\n", None),),
    )


def test_different_insertions_at_the_same_point_conflict() -> None:
    result = three_way_merge("one\ntwo\n", "one\nmine\ntwo\n", "one\ncurrent\ntwo\n")

    assert result == MergeResult(
        "one\ntwo\n",
        (MergeHunk(2, "", "mine\n", "current\n", None),),
    )


def test_identical_insertions_at_the_same_point_auto_merge() -> None:
    result = three_way_merge("one\ntwo\n", "one\nshared\ntwo\n", "one\nshared\ntwo\n")

    assert result == MergeResult("one\nshared\ntwo\n", ())


def test_insert_at_replacement_boundary_is_adjacent_not_overlapping() -> None:
    result = three_way_merge("one\ntwo\n", "ONE\ntwo\n", "before\none\ntwo\n")

    assert result == MergeResult("before\nONE\ntwo\n", ())


def test_insert_inside_replacement_is_an_ambiguous_overlap() -> None:
    base = "one\ntwo\nthree\n"

    result = three_way_merge(base, "ONE\nTWO\nthree\n", "one\ninserted\ntwo\nthree\n")

    assert result == MergeResult(
        base,
        (MergeHunk(1, "one\ntwo\n", "ONE\nTWO\n", "one\ninserted\ntwo\n", None),),
    )


def test_identical_edit_merges_alongside_each_sides_disjoint_edit() -> None:
    result = three_way_merge(
        "a\nanchor-1\nb\nanchor-2\nc\n",
        "A\nanchor-1\nB\nanchor-2\nc\n",
        "a\nanchor-1\nB\nanchor-2\nC\n",
    )

    assert result == MergeResult("A\nanchor-1\nB\nanchor-2\nC\n", ())


def test_overlapping_changed_intervals_form_one_conflict() -> None:
    base = "a\nb\nc\nd\ne\n"
    mine = "a\nB\nC\nd\ne\n"
    current = "a\nb\nSEE\nDEE\ne\n"

    result = three_way_merge(base, mine, current)

    assert result == MergeResult(
        base,
        (MergeHunk(2, "b\nc\nd\n", "B\nC\nd\n", "b\nSEE\nDEE\n", None),),
    )


def test_disjoint_conflicts_stay_separate_and_ordered() -> None:
    base = "a\nb\nc\nd\ne\n"
    mine = "a\nB1\nc\nd\nE1\n"
    current = "a\nB2\nc\nd\nE2\n"

    result = three_way_merge(base, mine, current)

    assert result.conflicts == (
        MergeHunk(2, "b\n", "B1\n", "B2\n", None),
        MergeHunk(5, "e\n", "E1\n", "E2\n", None),
    )
    assert result.text == base


def test_frontmatter_and_cjk_content_are_plain_preserved_lines() -> None:
    base = "---\ntitle: 문서\ntags: [기본]\n---\n본문입니다.\n"
    mine = "---\ntitle: 새 문서\ntags: [기본]\n---\n본문입니다.\n"
    current = "---\ntitle: 문서\ntags: [기본, 한글]\n---\n본문입니다!\n"

    result = three_way_merge(base, mine, current)

    assert result == MergeResult(
        "---\ntitle: 새 문서\ntags: [기본, 한글]\n---\n본문입니다!\n",
        (),
    )


@pytest.mark.parametrize(
    ("base", "mine", "current", "expected", "has_conflicts"),
    [
        ("alpha\nbeta", "ALPHA\nbeta", "alpha\nBETA", "ALPHA\nBETA", False),
        ("alpha\n", "alpha", "alpha\n", "alpha", False),
        ("", "mine", "current", "", True),
    ],
)
def test_final_newline_and_empty_text_are_not_normalized(
    base: str, mine: str, current: str, expected: str, has_conflicts: bool
) -> None:
    result = three_way_merge(base, mine, current)

    assert result.text == expected
    assert bool(result.conflicts) is has_conflicts


def test_repeated_lines_merge_deterministically() -> None:
    base = "top\nrepeat\nrepeat\nbottom\n"
    mine = "top\nMINE\nrepeat\nbottom\n"
    current = "top\nrepeat\nCURRENT\nbottom\n"

    first = three_way_merge(base, mine, current)

    assert first == MergeResult("top\nMINE\nCURRENT\nbottom\n", ())
    assert three_way_merge(base, mine, current) == first


def test_long_lines_are_preserved_without_character_normalization() -> None:
    long_line = "가" * 20_000
    base = f"{long_line}\ntail"
    mine = f"{long_line}M\ntail"
    current = f"{long_line}\nTAIL"

    assert three_way_merge(base, mine, current) == MergeResult(f"{long_line}M\nTAIL", ())
