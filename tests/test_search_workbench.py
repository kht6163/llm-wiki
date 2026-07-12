from dataclasses import FrozenInstanceError
from urllib.parse import parse_qs, urlsplit

import pytest

from llm_wiki import search as search_module
from llm_wiki.config import Settings
from llm_wiki.runtime import build_context
from llm_wiki.search import parse_query_filters
from llm_wiki.services.auth import Principal, create_user


def test_parser_normalizes_repeated_inline_tags_in_request_order():
    text, filters = parse_query_filters("needle tag:release tag:todo tag:release")

    assert text == "needle"
    assert filters.tags == ("release", "todo", "release")


def test_workbench_page_is_frozen_and_matches_the_existing_rank_window(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    for index in range(6):
        docs.create(
            editor,
            f"ranked-{index}.md",
            f"stable pagination needle {index}",
            tags=["release", "todo"],
            embed=False,
        )

    all_hits, truncated = docs.search_page(
        "stable pagination needle", mode="bm25", top_k=5, tags=["release", "todo"]
    )
    page = docs.search_workbench_page(
        "stable pagination needle title:ranked tag:release",
        mode="bm25",
        page=2,
        per_page=2,
        folder="",
        tags=["release", "todo", "release"],
    )

    assert truncated
    assert [item.path for item in page.items] == [item.path for item in all_hits[2:4]]
    assert page.page == 2 and page.per_page == 2
    assert page.has_prev and page.has_next
    assert not page.bounded
    assert page.total_or_more is None
    assert page.prev_url is not None
    assert "folder=" not in page.prev_url
    assert page.filters.query == "stable pagination needle title:ranked tag:release"
    assert page.filters.mode == "bm25"
    assert page.filters.tags == ("release", "todo", "release")
    assert [(item.operator, item.value) for item in page.filters.normalized] == [
        ("title", "ranked"),
        ("tag", "release"),
    ]
    with pytest.raises(FrozenInstanceError):
        page.page = 3  # type: ignore[misc]


def test_workbench_page_clamps_bounds_and_reports_only_known_totals(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "only.md", "bounded needle", embed=False)

    first = docs.search_workbench_page("bounded needle", mode="not-a-mode", page=0, per_page=100)
    beyond = docs.search_workbench_page("bounded needle", mode="bm25", page=9, per_page=1)

    assert (first.page, first.per_page, first.filters.mode) == (1, 50, "hybrid")
    assert first.total_or_more is None
    assert not first.has_prev and not first.has_next
    assert not first.bounded
    assert first.next_url is None
    assert beyond.items == ()
    assert beyond.has_prev and not beyond.has_next
    assert beyond.total_or_more is None


def test_workbench_page_keeps_hybrid_pool_stable_and_bounds_huge_pages(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    for index in range(45):
        docs.create(editor, f"hybrid-{index}.md", f"hybrid stable needle {index}")

    first = docs.search_workbench_page("hybrid stable needle", mode="hybrid", page=1, per_page=10)
    second = docs.search_workbench_page("hybrid stable needle", mode="hybrid", page=2, per_page=10)
    repeated_first = docs.search_workbench_page(
        "hybrid stable needle", mode="hybrid", page=1, per_page=10
    )
    huge = docs.search_workbench_page(
        "hybrid stable needle", mode="bm25", page=10**100, per_page=50
    )
    bounded_edge = docs.search_workbench_page(
        "hybrid stable needle", mode="bm25", page=4, per_page=10
    )

    first_paths = [item.path for item in first.items]
    second_paths = [item.path for item in second.items]
    assert first_paths == [item.path for item in repeated_first.items]
    assert set(first_paths).isdisjoint(second_paths)
    assert huge.items == () and huge.total_or_more is None and huge.bounded
    assert len(bounded_edge.items) == 10 and bounded_edge.has_next


@pytest.mark.parametrize("mode", ["bm25", "vector", "hybrid"])
def test_tied_pages_are_stable_across_calls_and_insertion_orders(tmp_path, monkeypatch, mode):
    paths = ["delta.md", "alpha.md", "charlie.md", "bravo.md"]

    if mode == "hybrid":
        monkeypatch.setattr(
            search_module,
            "_prepare_query_vector",
            lambda db, _embedder, _query: (db.expected_embedding_binding(), b"vector"),
        )

        def bm25_by_path(conn, *_args, **_kwargs):
            rows = conn.execute("SELECT id FROM documents ORDER BY path_norm")
            return [(row["id"], 0.25) for row in rows]

        def vector_by_reverse_path(conn, *_args, **_kwargs):
            rows = conn.execute("SELECT id FROM documents ORDER BY path_norm DESC")
            return [
                (row["id"], {
                    "distance": 0.25, "heading": None, "text": "same tied pagination needle",
                    "heading_path": None, "chunk_id": row["id"], "ordinal": 0,
                    "char_start": 0, "char_end": 27,
                })
                for row in rows
            ]

        monkeypatch.setattr(search_module, "_bm25", bm25_by_path)
        monkeypatch.setattr(search_module, "_vector", vector_by_reverse_path)

    def traverse(name, insertion_order):
        settings = Settings(
            vault_path=tmp_path / name / "vault",
            db_path=tmp_path / name / "wiki.db",
            embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            session_secret="test-secret",
        )
        context = build_context(settings, full=True)
        user_id = create_user(context.db, "editor", "secret12", "editor")
        editor = Principal(user_id, "editor", "editor")
        for path in insertion_order:
            context.docs.create(
                editor,
                path,
                "same tied pagination needle",
                embed=mode == "vector",
            )

        first = context.docs.search_workbench_page(
            "same tied pagination needle", mode=mode, page=1, per_page=2
        )
        second = context.docs.search_workbench_page(
            "same tied pagination needle", mode=mode, page=2, per_page=2
        )
        repeated = context.docs.search_workbench_page(
            "same tied pagination needle", mode=mode, page=1, per_page=2
        )
        first_paths = [item.path for item in first.items]
        traversed = first_paths + [item.path for item in second.items]
        assert first_paths == [item.path for item in repeated.items]
        assert len(traversed) == len(set(traversed)) == len(paths)
        context.db.close()
        return traversed

    forward = traverse("forward", paths)
    reverse = traverse("reverse", reversed(paths))

    expected = (
        ["alpha.md", "delta.md", "bravo.md", "charlie.md"]
        if mode == "hybrid"
        else sorted(paths)
    )
    assert forward == reverse == expected


def test_workbench_exposes_the_600_result_boundary(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    for index in range(605):
        docs.create(
            editor,
            f"bounded-{index:03}.md",
            f"bounded corpus needle {index}",
            embed=False,
        )
    for index in range(10):
        docs.create(
            editor,
            f"vector-tail-{index:02}.md",
            f"bounded corpus needle semantic tail {index}",
        )

    before_edge = docs.search_workbench_page(
        "bounded corpus needle", mode="bm25", page=11, per_page=50
    )
    edge = docs.search_workbench_page("bounded corpus needle", mode="bm25", page=12, per_page=50)
    outside = docs.search_workbench_page("bounded corpus needle", mode="bm25", page=13, per_page=50)
    uneven_edge = docs.search_workbench_page(
        "bounded corpus needle", mode="hybrid", page=86, per_page=7
    )

    assert len(before_edge.items) == 50 and before_edge.has_next and not before_edge.bounded
    assert len(edge.items) == 50 and not edge.has_next and edge.bounded
    assert edge.total_or_more is None
    assert outside.items == () and not outside.has_next and outside.bounded
    assert len(uneven_edge.items) == 5
    assert not uneven_edge.has_next and uneven_edge.bounded and uneven_edge.next_url is None


def test_workbench_page_url_preserves_repeated_tags_query_mode_and_page_size(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    for index in range(3):
        docs.create(
            editor,
            f"notes/url-{index}.md",
            "url state needle",
            tags=["one", "two"],
            embed=False,
        )

    page = docs.search_workbench_page(
        "url state needle has:tag",
        mode="bm25",
        page=1,
        per_page=1,
        folder="notes",
        tags=["one", "two", "one"],
    )

    assert page.prev_url is None
    assert page.next_url is not None
    query = parse_qs(urlsplit(page.next_url).query)
    assert query == {
        "q": ["url state needle has:tag"],
        "mode": ["bm25"],
        "folder": ["notes"],
        "tag": ["one", "two", "one"],
        "page": ["2"],
        "per_page": ["1"],
    }


def test_malformed_unknown_operator_remains_search_text(ctx, principals):
    docs, editor = ctx.docs, principals["editor"]
    docs.create(editor, "literal.md", "unknown operator widget", embed=False)

    page = docs.search_workbench_page(
        'unknown operator widget strange:value title:"unterminated',
        mode="bm25",
        page=1,
        per_page=10,
    )

    assert [(item.operator, item.value) for item in page.filters.normalized] == [
        ("title", "unterminate"),
    ]
    assert page.items == ()
