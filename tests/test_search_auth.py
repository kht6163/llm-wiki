from contextlib import contextmanager
from threading import Event, Thread

import pytest

from llm_wiki import search as search_module
from llm_wiki.db import Database
from llm_wiki.embedding_contract import EmbeddingBindingChanged
from llm_wiki.search import search, search_page
from llm_wiki.services.auth import (
    authenticate,
    create_api_key,
    principal_from_api_key,
)
from llm_wiki.services.errors import EmbeddingUnavailableError, NotFoundError


def test_hybrid_search_finds_doc(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "python.md", "# Python\n\nPython is a programming language for data science.")
    docs.create(p, "cooking.md", "# Cooking\n\nHow to bake bread at home.")

    res = search(ctx.db, ctx.embedder, "programming language", mode="hybrid", top_k=5)
    assert res, "expected at least one hybrid result"
    assert res[0].path == "python.md"

    res_bm = search(ctx.db, ctx.embedder, "bread", mode="bm25", top_k=5)
    assert any(r.path == "cooking.md" for r in res_bm)

    res_vec = search(ctx.db, ctx.embedder, "software development", mode="vector", top_k=5)
    assert any(r.path == "python.md" for r in res_vec)


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_vector_search_fails_closed_after_same_dimension_rebind(
    ctx, principals, mode
):
    ctx.docs.create(
        principals["editor"], "stale.md", "# Stale\n\nvector generation fence"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    other.rebind_model(
        "test/different-same-dimension-model",
        ctx.embedder.dim,
        ctx.embedder.pipeline,
    )

    with pytest.raises(EmbeddingBindingChanged):
        search(ctx.db, ctx.embedder, "generation fence", mode=mode, top_k=5)


def test_bm25_search_still_works_after_embedding_rebind(ctx, principals):
    ctx.docs.create(
        principals["editor"], "lexical.md", "# Lexical\n\nlexical fallback remains available"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    other.rebind_model(
        "test/different-same-dimension-model",
        ctx.embedder.dim,
        ctx.embedder.pipeline,
    )

    results = search(
        ctx.db, ctx.embedder, "lexical fallback", mode="bm25", top_k=5
    )

    assert [result.path for result in results] == ["lexical.md"]


def test_search_rebind_during_query_encode_fails_before_knn(
    ctx, principals, monkeypatch
):
    ctx.docs.create(
        principals["editor"], "race.md", "# Race\n\nquery encoding generation race"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    original = ctx.embedder

    class RebindingEmbedder:
        model_name = original.model_name
        pipeline = original.pipeline
        dim = original.dim

        def embed_query(self, text):
            vector = original.embed_query(text)
            other.rebind_model(
                "test/rebound-during-query",
                self.dim,
                self.pipeline,
            )
            return vector

    knn_called = False

    def unexpected_knn(*args, **kwargs):
        nonlocal knn_called
        knn_called = True
        raise AssertionError("KNN must not run after the binding changes")

    monkeypatch.setattr(search_module, "_vector", unexpected_knn)

    with pytest.raises(EmbeddingBindingChanged):
        search(
            ctx.db,
            RebindingEmbedder(),
            "generation race",
            mode="vector",
            top_k=5,
        )
    assert knn_called is False


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_vector_search_rejects_non_finite_query_embedding(ctx, bad_value):
    original = ctx.embedder

    class NonFiniteEmbedder:
        model_name = original.model_name
        pipeline = original.pipeline

        def embed_query(self, _text):
            return [bad_value, *([0.0] * (original.dim - 1))]

    with pytest.raises(ValueError, match="finite"):
        search(
            ctx.db,
            NonFiniteEmbedder(),
            "non finite vector",
            mode="vector",
            top_k=5,
        )


def test_vector_search_rejects_float32_overflow_before_knn(ctx, monkeypatch):
    original = ctx.embedder
    knn_called = False

    class OverflowEmbedder:
        model_name = original.model_name
        pipeline = original.pipeline

        def embed_query(self, _text):
            return [1e39, *([0.0] * (original.dim - 1))]

    def unexpected_knn(*_args, **_kwargs):
        nonlocal knn_called
        knn_called = True
        raise AssertionError("KNN must not receive a float32-overflowed vector")

    monkeypatch.setattr(search_module, "_vector", unexpected_knn)

    with pytest.raises(ValueError, match="float32"):
        search(
            ctx.db,
            OverflowEmbedder(),
            "float32 overflow",
            mode="vector",
            top_k=5,
        )
    assert knn_called is False


def test_vector_search_keeps_verified_snapshot_when_rebind_commits(
    ctx, principals, monkeypatch
):
    ctx.docs.create(
        principals["editor"], "snapshot.md", "# Snapshot\n\nverified vector snapshot"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    original_vector = search_module._vector
    rebound = False

    def vector_then_rebind(conn, query_vector, limit):
        nonlocal rebound
        results = original_vector(conn, query_vector, limit)
        if not rebound:
            rebound = True
            other.rebind_model(
                "test/rebound-after-snapshot",
                ctx.embedder.dim,
                ctx.embedder.pipeline,
            )
        return results

    monkeypatch.setattr(search_module, "_vector", vector_then_rebind)

    current = search(
        ctx.db, ctx.embedder, "verified vector snapshot", mode="vector", top_k=5
    )

    assert [result.path for result in current] == ["snapshot.md"]
    with pytest.raises(EmbeddingBindingChanged):
        search(
            ctx.db,
            ctx.embedder,
            "verified vector snapshot",
            mode="vector",
            top_k=5,
        )


def test_vector_search_keeps_snapshot_when_rebind_commits_before_first_knn(
    ctx, principals, monkeypatch
):
    ctx.docs.create(
        principals["editor"],
        "pre-knn.md",
        "# Pre KNN\n\nverified snapshot before first knn",
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    original_snapshot = ctx.db.embedding_read_snapshot
    rebound = False

    @contextmanager
    def rebind_after_verification(expected):
        nonlocal rebound
        with original_snapshot(expected) as conn:
            if not rebound:
                rebound = True
                other.rebind_model(
                    "test/rebound-before-first-knn",
                    ctx.embedder.dim,
                    ctx.embedder.pipeline,
                )
            yield conn

    monkeypatch.setattr(
        ctx.db, "embedding_read_snapshot", rebind_after_verification
    )

    current = search(
        ctx.db,
        ctx.embedder,
        "verified snapshot before first knn",
        mode="vector",
        top_k=5,
    )

    assert [result.path for result in current] == ["pre-knn.md"]
    with pytest.raises(EmbeddingBindingChanged):
        search(
            ctx.db,
            ctx.embedder,
            "verified snapshot before first knn",
            mode="vector",
            top_k=5,
        )


def test_document_service_translates_stale_assemble_context_binding(ctx, principals):
    ctx.docs.create(
        principals["editor"], "rag.md", "# RAG\n\ncontext generation fence"
    )
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    other.rebind_model(
        "test/different-same-dimension-model",
        ctx.embedder.dim,
        ctx.embedder.pipeline,
    )

    with pytest.raises(EmbeddingUnavailableError) as exc_info:
        ctx.docs.assemble_context("context generation", mode="hybrid")
    assert exc_info.value.code == "embedding_unavailable"
    assert exc_info.value.suggested_action == "restart_service"


def test_related_vector_reads_share_one_verified_snapshot(
    ctx, principals, monkeypatch
):
    ctx.docs.create(
        principals["editor"], "source.md", "# Source\n\nshared semantic topic"
    )
    ctx.docs.create(
        principals["editor"], "neighbor.md", "# Neighbor\n\nshared semantic topic"
    )

    original_snapshot = ctx.db.embedding_read_snapshot
    snapshot_entries = 0
    path_reads = 0
    vector_reads = 0

    class CheckedConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, parameters=()):
            nonlocal path_reads, vector_reads
            if "from documents where path_norm" in " ".join(sql.lower().split()):
                assert self._conn.in_transaction
                path_reads += 1
            if "chunk_vectors" in sql:
                assert self._conn.in_transaction
                vector_reads += 1
            return self._conn.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def checked_snapshot(expected):
        nonlocal snapshot_entries
        snapshot_entries += 1
        with original_snapshot(expected) as conn:
            yield CheckedConnection(conn)

    monkeypatch.setattr(ctx.db, "embedding_read_snapshot", checked_snapshot)

    results = ctx.docs.related("source.md", limit=5)["related"]

    assert results
    assert snapshot_entries == 1
    assert path_reads == 1
    assert vector_reads >= 2  # source vector SELECT plus at least one KNN SELECT


def test_related_path_and_vectors_survive_concurrent_delete_in_one_snapshot(
    ctx, principals, monkeypatch
):
    ctx.docs.create(
        principals["editor"], "source.md", "# Source\n\nshared deletion race topic"
    )
    ctx.docs.create(
        principals["editor"], "neighbor.md", "# Neighbor\n\nshared deletion race topic"
    )
    with ctx.db.reader() as conn:
        source_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm='source.md'"
        ).fetchone()[0]

    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    path_selected = Event()
    delete_committed = Event()
    writer_errors = []
    original_reader = ctx.db.reader

    class RacingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, parameters=()):
            cursor = self._conn.execute(sql, parameters)
            normalized = " ".join(sql.lower().split())
            if "from documents where path_norm" in normalized:
                path_selected.set()
                assert delete_committed.wait(5)
            return cursor

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def racing_reader():
        with original_reader() as conn:
            yield RacingConnection(conn)

    monkeypatch.setattr(ctx.db, "reader", racing_reader)

    def delete_source():
        try:
            assert path_selected.wait(5)
            with other.writer() as conn:
                chunk_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM chunks WHERE doc_id=?", (source_id,)
                    )
                ]
                conn.executemany(
                    "DELETE FROM chunk_vectors WHERE chunk_id=?",
                    [(chunk_id,) for chunk_id in chunk_ids],
                )
                conn.execute("DELETE FROM chunks WHERE doc_id=?", (source_id,))
                conn.execute(
                    "UPDATE documents SET is_deleted=1, vector_dirty=0 WHERE id=?",
                    (source_id,),
                )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            delete_committed.set()

    writer = Thread(target=delete_source)
    writer.start()
    try:
        related = ctx.docs.related("source.md", limit=5)["related"]
    finally:
        writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert [item["path"] for item in related] == ["neighbor.md"]
    with pytest.raises(NotFoundError):
        ctx.docs.related("source.md", limit=5)


def test_direct_related_documents_fails_closed_after_rebind(ctx, principals):
    ctx.docs.create(
        principals["editor"], "direct.md", "# Direct\n\ndirect related generation fence"
    )
    with ctx.db.reader() as conn:
        source_id = conn.execute(
            "SELECT id FROM documents WHERE path_norm='direct.md'"
        ).fetchone()[0]
    other = Database(ctx.settings.db_path)
    other.initialize(
        ctx.embedder.model_name, ctx.embedder.dim, ctx.embedder.pipeline
    )
    other.rebind_model(
        "test/different-same-dimension-model",
        ctx.embedder.dim,
        ctx.embedder.pipeline,
    )

    with pytest.raises(EmbeddingBindingChanged):
        search_module.related_documents(ctx.db, source_id, k=5)


def test_fts_body_excludes_frontmatter(ctx, principals):
    # The BM25 snippet leg must not leak `tags: [...]` / `---` from frontmatter;
    # only body prose is indexed (title is a separate column, tags a separate table).
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "fm.md", "---\ntitle: 제목\ntags: [alpha, beta]\n---\n\n본문 내용입니다\n")
    with ctx.db.reader() as conn:
        row = conn.execute(
            "SELECT f.body FROM documents_fts f JOIN documents d ON d.id = f.rowid "
            "WHERE d.path_norm = ?",
            ("fm.md",),
        ).fetchone()
    body = row[0]
    assert "본문 내용입니다" in body
    assert "tags:" not in body
    assert "alpha" not in body
    assert "---" not in body


def test_search_folder_filter(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "work/report.md", "quarterly sales report")
    docs.create(p, "home/report.md", "home renovation report")
    res = search(ctx.db, ctx.embedder, "report", mode="bm25", top_k=10, folder="work")
    assert res and all(r.path.startswith("work/") for r in res)


def test_search_tag_filter_requires_all_tags(ctx, principals):
    # Exercises the batch-loaded tag filter path: only docs carrying ALL tags pass.
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "a.md", "shared report content", tags=["release", "todo"])
    docs.create(p, "b.md", "shared report content", tags=["release"])
    res, _ = search_page(ctx.db, ctx.embedder, "report content", mode="hybrid",
                         top_k=10, tags=["release", "todo"])
    paths = {r.path for r in res}
    assert "a.md" in paths and "b.md" not in paths


def test_bm25_folder_filter_pushed_into_sql(ctx, principals):
    # The folder filter must constrain the candidate LIMIT, not post-filter a fixed
    # top-k. With a small limit, a folder match that ranks LAST must still be found.
    from llm_wiki.search import _bm25, _fts_match
    docs, p = ctx.docs, principals["editor"]
    for i in range(3):
        docs.create(p, f"other/o{i}.md", "report report report")  # high tf -> ranks higher
    docs.create(p, "work/w.md", "report")  # low tf -> would fall outside a small window
    with ctx.db.reader() as conn:
        rows = _bm25(conn, _fts_match("report"), 2, folder="work")
        paths = [conn.execute("SELECT path FROM documents WHERE id=?", (did,)).fetchone()["path"]
                 for did, _ in rows]
    assert paths == ["work/w.md"]  # only the folder match, recovered despite ranking last


def test_search_page_truncation_is_exact(ctx, principals):
    # A corpus of exactly top_k matches must report truncated=False (no misleading
    # 'raise top_k'); fewer slots than matches reports True.
    docs, p = ctx.docs, principals["editor"]
    for i in range(3):
        docs.create(p, f"k{i}.md", "shared keyword apple here")
    res, trunc = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=3)
    assert len(res) == 3 and trunc is False
    res2, trunc2 = search_page(ctx.db, ctx.embedder, "apple", mode="bm25", top_k=2)
    assert len(res2) == 2 and trunc2 is True


def test_password_and_api_key_auth(ctx, principals):
    # password auth
    assert authenticate(ctx.db, "alice", "secret12") is not None
    assert authenticate(ctx.db, "alice", "wrong") is None

    # api key auth round-trip
    token = create_api_key(ctx.db, principals["viewer"], "test-key")
    pr = principal_from_api_key(ctx.db, token)
    assert pr is not None and pr.username == "bob" and pr.role == "viewer"
    assert pr.can_write is False

    assert principal_from_api_key(ctx.db, "lw_bogustokenvalue") is None
    assert principal_from_api_key(ctx.db, None) is None


def test_reindex_external_edit(ctx, principals):
    docs = ctx.docs
    p = principals["editor"]
    docs.create(p, "ext.md", "original")
    # simulate an external editor writing directly to the vault
    (ctx.settings.vault_path / "ext.md").write_text("externally changed", encoding="utf-8")
    (ctx.settings.vault_path / "new_external.md").write_text("# Brand New\n\nhello", encoding="utf-8")

    res = docs.reindex_all()
    assert res["created"] >= 1  # new_external.md
    assert res["updated"] >= 1  # ext.md changed
    assert "externally changed" in docs.get("ext.md")["content"]
    assert docs.get("new_external.md")["title"] == "Brand New"
