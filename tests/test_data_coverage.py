from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from llm_wiki import embedding as embedding_mod
from llm_wiki import graph, indexing, markdown_render, markdown_utils, search
from llm_wiki.db import Database, get_meta, set_meta
from llm_wiki.embedding import Embedder
from llm_wiki.embedding_contract import (
    EMBEDDING_PIPELINE,
    EmbeddingBinding,
    EmbeddingBindingChanged,
)
from llm_wiki.markdown_utils import Link


class _Rows:
    def __init__(self, rows=(), *, rowcount=1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


def _fresh_db(tmp_path, *, dim: int = 2) -> Database:
    db = Database(tmp_path / "coverage.db")
    db.initialize("fake/model", dim, EMBEDDING_PIPELINE)
    return db


def _insert_indexed_doc(db: Database, path: str = "doc.md", body: str = "# Head\nalpha") -> int:
    with db.writer() as conn:
        cur = conn.execute(
            "INSERT INTO documents(path,path_norm,title,folder,content_hash,created_at,updated_at,vector_dirty) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (path, path.lower(), path[:-3], "", "hash-" + path, "2024-01-01", "2024-01-01"),
        )
        doc_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO revisions(doc_id,version,body,title,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (doc_id, 1, body, path[:-3], "hash-" + path, "2024-01-01"),
        )
        indexing.publish_prepared(conn, doc_id, path[:-3], "", indexing.prepare_markdown(body))
    return doc_id


def test_database_rollback_failure_preserves_original_error(tmp_path):
    class BrokenRollback:
        def __init__(self):
            self.commands = []

        def execute(self, sql, _params=()):
            self.commands.append(sql)
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("rollback unavailable")
            return _Rows()

    conn = BrokenRollback()
    db = Database(tmp_path / "unused.db")
    db._local.conn = conn
    with pytest.raises(RuntimeError, match="write failed"):
        with db.writer():
            raise RuntimeError("write failed")
    assert conn.commands == ["BEGIN IMMEDIATE", "ROLLBACK"]


def test_database_migration_rollback_failure_preserves_ddl_error(tmp_path):
    class BrokenMigration:
        def execute(self, sql, _params=()):
            if sql.startswith("SELECT v FROM meta"):
                return _Rows([("2",)])
            if sql == "BEGIN IMMEDIATE":
                return _Rows()
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("rollback unavailable")
            raise sqlite3.OperationalError("disk full during migration")

    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        Database(tmp_path / "unused.db")._apply_migrations(BrokenMigration())


def test_database_binding_requires_staged_token_and_close_is_idempotent(tmp_path):
    db = _fresh_db(tmp_path)
    db.close()
    db.close()
    with pytest.raises(RuntimeError, match="did not stage"):
        with db._embedding_binding_writer() as (conn, _stage):
            assert conn.in_transaction
    assert db.expected_embedding_binding() == EmbeddingBinding(
        "fake/model", 2, EMBEDDING_PIPELINE, 1
    )


@pytest.mark.parametrize("dim", [0, -1])
def test_database_rejects_nonpositive_embedding_dimensions(tmp_path, dim):
    db = Database(tmp_path / f"bad-{dim}.db")
    with pytest.raises(ValueError, match="positive"):
        db.ensure_vector_table(dim)
    with pytest.raises(ValueError, match="positive"):
        db.initialize("fake/model", dim)
    with pytest.raises(ValueError, match="positive"):
        db.rebind_model("fake/model", dim, EMBEDDING_PIPELINE)


def test_database_reports_corrupt_binding_values_and_vector_schema(tmp_path):
    db = _fresh_db(tmp_path)
    with db.writer() as conn:
        set_meta(conn, "embedding_dim", "0")
    other = Database(db.path)
    with pytest.raises(RuntimeError, match="positive integers"):
        other.initialize("fake/model", 2, EMBEDDING_PIPELINE)

    with db.writer() as conn:
        set_meta(conn, "embedding_dim", "not-an-int")
        conn.execute("BEGIN") if not conn.in_transaction else None
        with pytest.raises(EmbeddingBindingChanged, match="incomplete or invalid"):
            db.verify_embedding_binding(conn, db.expected_embedding_binding())

    odd = Database(tmp_path / "odd.db")
    odd.ensure_schema()
    with odd.writer() as conn:
        for key, value in {
            "embedding_model": "fake/model",
            "embedding_dim": "2",
            "embedding_pipeline": EMBEDDING_PIPELINE,
            "embedding_epoch": "1",
        }.items():
            set_meta(conn, key, value)
        conn.execute("CREATE TABLE chunk_vectors(chunk_id INTEGER PRIMARY KEY, embedding BLOB)")
    with pytest.raises(RuntimeError, match="readable embedding dimension"):
        odd.initialize("fake/model", 2, EMBEDDING_PIPELINE)


def test_database_can_create_vector_table_explicitly(tmp_path):
    db = Database(tmp_path / "vectors.db")
    db.ensure_schema()
    db.ensure_vector_table(2)
    with db.reader() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunk_vectors'"
        ).fetchone()[0]
    assert "float[2]" in sql


@pytest.mark.parametrize(("epoch", "message"), [("bad", "valid integer"), ("0", "positive")])
def test_database_rebind_rejects_corrupt_epoch(tmp_path, epoch, message):
    db = _fresh_db(tmp_path)
    with db.writer() as conn:
        set_meta(conn, "embedding_epoch", epoch)
    with pytest.raises(RuntimeError, match=message):
        db.rebind_model("new/model", 3, EMBEDDING_PIPELINE)
    with db.reader() as conn:
        assert get_meta(conn, "embedding_model") == "fake/model"


def test_uninitialized_database_binding_is_observable(tmp_path):
    db = Database(tmp_path / "empty.db")
    with pytest.raises(RuntimeError, match="not initialized"):
        db.expected_embedding_binding()


def test_embedder_concurrent_load_e5_prefix_fallback_dimension_and_lru(monkeypatch):
    class Model:
        def __init__(self):
            self.calls = []

        def get_sentence_embedding_dimension(self):
            return 2

        def encode(self, texts, **kwargs):
            self.calls.append((list(texts), kwargs))
            return [[float(len(self.calls)), 0.5] for _ in texts]

    model = Model()
    emb = Embedder("multilingual-e5-test")

    class PublishingLock:
        def __enter__(self):
            emb._model = model

        def __exit__(self, *_exc):
            return False

    emb._lock = PublishingLock()
    assert emb._load() is model
    emb._lock = threading.RLock()
    assert emb.dim == 2
    assert emb.embed_passages(["alpha"])[0] == [1.0, 0.5]
    monkeypatch.setattr(embedding_mod, "_QUERY_CACHE_MAX", 1)
    first = emb.embed_query("one")
    assert emb.embed_query("one") is first
    emb.embed_query("two")
    assert list(emb._query_cache) == ["two"]
    assert model.calls[0][0] == ["passage: alpha"]
    assert model.calls[1][0] == ["query: one"]


def test_graph_invalid_duplicate_links_and_resolution_are_ignored(tmp_path):
    db = _fresh_db(tmp_path)
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO documents(path,path_norm,title,folder,content_hash,created_at,updated_at) "
            "VALUES('source.md','source.md','Source','','h','t','t')"
        )
        doc_id = conn.execute("SELECT id FROM documents WHERE path='source.md'").fetchone()[0]
        valid = Link("wikilink", "Missing", None, None, "[[Missing]]", 0, 11)
        invalid = Link("wikilink", "../escape", None, None, "[[../escape]]", 12, 24)
        graph.store_links(conn, doc_id, [invalid, valid, valid], "")
        rows = conn.execute("SELECT raw FROM links WHERE src_doc_id=?", (doc_id,)).fetchall()
        assert [r[0] for r in rows] == ["[[Missing]]"]
        assert graph.resolve_path(conn, "../escape") is None


@pytest.mark.parametrize(
    ("body", "offset", "radius", "expected"),
    [
        ("", 0, 3, None),
        ("text", None, 3, None),
        ("text", -1, 3, None),
        ("text", 5, 3, None),
        ("   ", 1, 3, None),
        ("before target after", 8, 2, "before target …"),
    ],
)
def test_graph_link_context_boundaries(body, offset, radius, expected):
    assert graph._link_context(body, offset, radius) == expected


def test_graph_empty_and_inconsistent_rows_remain_safe():
    class EmptyConn:
        def execute(self, sql, _params=()):
            if "COUNT(*)" in sql:
                return _Rows([(0,)])
            return _Rows()

    assert graph.build_graph(EmptyConn())["nodes"] == []

    class InconsistentConn:
        def execute(self, sql, _params=()):
            if sql.startswith("SELECT doc_id, tag"):
                return _Rows()
            if sql.startswith("SELECT id, path"):
                return _Rows([{"id": 1, "path": "a.md", "title": None, "folder": ""}])
            if sql.startswith("SELECT src_doc_id"):
                return _Rows([
                    {"src_doc_id": 99, "dst_doc_id": None, "dst_name": "ghost", "is_resolved": 0,
                     "link_type": "wikilink", "alias": None, "anchor": None},
                    {"src_doc_id": 1, "dst_doc_id": None, "dst_name": "ghost", "is_resolved": 0,
                     "link_type": "wikilink", "alias": None, "anchor": None},
                    {"src_doc_id": 1, "dst_doc_id": None, "dst_name": "ghost", "is_resolved": 0,
                     "link_type": "wikilink", "alias": None, "anchor": None},
                ])
            if "ORDER BY updated_at" in sql:
                return _Rows([(1,)])
            if "COUNT(*)" in sql:
                return _Rows([(1,)])
            raise AssertionError(sql)

    built = graph.build_graph(InconsistentConn())
    assert len(built["edges"]) == 2
    assert {n["id"] for n in built["nodes"]} == {"a.md", "unresolved:ghost"}


def test_graph_backlink_without_usable_context_omits_context():
    class Conn:
        calls = 0

        def execute(self, _sql, _params=()):
            self.calls += 1
            if self.calls == 1:
                return _Rows([{"src_id": 1, "src_path": "a.md", "src_title": "A",
                               "alias": None, "anchor": None, "link_type": "wikilink",
                               "char_start": None}])
            return _Rows([{"doc_id": 1, "body": "body"}])

    assert graph.get_backlinks(Conn(), 2, with_context=True) == [
        {"src_path": "a.md", "src_title": "A", "alias": None, "anchor": None,
         "link_type": "wikilink"}
    ]


def test_markdown_malformed_frontmatter_links_chunk_boundaries_and_rewrites():
    meta, end = markdown_utils.parse_frontmatter(
        "---\nignored line\naliases:\n  - One\n  - 'Two'\nempty:\n---\nBody"
    )
    assert meta == {"aliases": ["One", "Two"], "empty": ""}
    assert end > 0
    assert markdown_utils.section_text("# A\nbody\n## B\nchild", "A") == "# A\nbody\n## B\nchild"
    assert markdown_utils.document_properties("---\ntitle: T\ntags: [x]\nempty:\nowner: me\n---\nx") == [
        ("owner", ["me"])
    ]
    assert markdown_utils.set_frontmatter_tags("---\ntags:\n - one\n - two\nkeep: yes\n---\nB", []) == (
        "---\nkeep: yes\n---\nB"
    )
    assert markdown_utils.set_frontmatter_property("body", "note", " leading") .startswith(
        '---\nnote: " leading"'
    )
    assert markdown_utils.remove_frontmatter_property("body", "missing") == "body"
    assert markdown_utils._is_internal_md("") is False
    assert markdown_utils._is_internal_md("https://example.test") is False
    assert markdown_utils.extract_links("[[#same]] [same](#anchor) [[ok]]")[-1].target == "ok"
    malformed = Link("markdown", "x", None, None, "not a markdown link", 0, 19)
    assert markdown_utils.rewrite_link_target(malformed, "new") == malformed.raw
    assert markdown_utils.chunk_markdown("---\ntitle: x\n---\n   ") == []
    chunks = markdown_utils.chunk_markdown(
        "intro\n\n# A\n\n## B\n\n" + "one " * 8 + "\n\n" + "two " * 8,
        max_chars=25,
        overlap=0,
    )
    assert len(chunks) >= 3
    assert chunks[-1].heading_path == "A > B"
    assert all(c.text for c in chunks)


def test_markdown_remaining_scalar_link_and_chunk_shapes():
    assert markdown_utils.derive_content_title({"title": " Explicit "}, "# Ignored") == "Explicit"
    assert markdown_utils.derive_content_title({}, "```\n# hidden\n```\n## no h1") is None
    assert markdown_utils.derive_title({"title": " Explicit "}, "", "x.md") == "Explicit"
    assert markdown_utils.extract_tags({"tags": "one, #two"}, "text #three") == ["one", "three", "two"]
    assert markdown_utils.set_frontmatter_tags("plain", ["a b"]).startswith('---\ntags: ["a b"]')
    assert markdown_utils.set_frontmatter_tags("plain", []) == "plain"
    assert markdown_utils.set_frontmatter_tags("---\ntags: one\n---\nplain", ["two"]) == (
        "---\ntags: [two]\n---\nplain"
    )
    assert markdown_utils._emit_frontmatter_value("x", "plain") == "x: plain"
    assert markdown_utils._drop_key_lines(["aliases:", " - a", " - b", "other: x"], 0) == 3
    assert markdown_utils._is_internal_md("image.png") is False
    links = markdown_utils.extract_links("[[target|Alias]] [Label](folder/doc.md#Part \"title\")")
    assert markdown_utils.rewrite_link_target(links[0], "new") == "[[new|Alias]]"
    assert markdown_utils.rewrite_link_target(links[1], "other.md") == (
        '[Label](other.md#Part "title")'
    )
    chunks = markdown_utils.chunk_markdown("# Empty\n\n# Full\nbody", max_chars=100)
    assert [c.heading for c in chunks] == ["Empty", "Full"]
    split = markdown_utils._split_keep_offsets("a\n\n\n\nb")
    assert split[0][0].startswith("a\n") and split[-1][0] == "b"
    assert markdown_utils.extract_links("[[   ]]") == []
    assert "[[visible]]" in markdown_utils._mask("``` info`bad\n[[visible]]\n```")
    one = markdown_utils.chunk_markdown("x" * 30, max_chars=10)
    assert len(one) == 1 and one[0].text == "x" * 30
    packed = markdown_utils.chunk_markdown("aa\n\nbb\n\n" + "x" * 20, max_chars=10)
    assert packed[0].text == "aa\n\nbb"
    assert markdown_utils._split_keep_offsets("\n\nx")[-1][0] == "x"


def test_markdown_render_obscure_wikilinks_embeds_and_limits(monkeypatch):
    assert "[[#section]]" == markdown_render._wiki_repl(
        markdown_render.WIKILINK_RE.search("[[#section]]"), "a.md"
    )
    assert markdown_render._convert_inline("![[|alias]]", "a.md", None, 0, frozenset(), [1], []) == (
        "![[|alias]]"
    )
    monkeypatch.setattr(markdown_render, "_MAX_EMBED_DEPTH", 0)
    collapsed = markdown_render.render_markdown(
        "![[target]]", "a.md", resolve_embed=lambda _target: {"path": "target.md", "content": "body"}
    )
    assert "embed-collapsed" in collapsed and "펼치지 않음" in collapsed
    assert "==code==" in markdown_render.render_markdown("`==code==` ==mark==")


class _FakeEmbedder:
    model_name = "fake/model"
    pipeline = EMBEDDING_PIPELINE

    def __init__(self, output):
        self.output = output

    @property
    def dim(self):
        return 2

    def embed_passages(self, texts):
        return self.output(texts) if callable(self.output) else self.output

    def embed_query(self, text):
        return [0.5, 0.5]


def test_indexing_rejects_bad_batch_sizes_and_embedder_dimension(tmp_path):
    db = _fresh_db(tmp_path)
    doc_id = _insert_indexed_doc(db)
    with pytest.raises(ValueError, match="batch_size"):
        indexing.embed_doc(db, _FakeEmbedder([]), doc_id, batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        indexing.embed_pending(db, _FakeEmbedder([]), batch_size=0)
    with pytest.raises(ValueError, match="doc_batch_size"):
        indexing.embed_pending(db, _FakeEmbedder([]), doc_batch_size=0)

    class WrongDim(_FakeEmbedder):
        @property
        def dim(self):
            return 3

    bad = WrongDim([])
    with pytest.raises(EmbeddingBindingChanged, match="does not match"):
        indexing._verify_embedder_identity(db.expected_embedding_binding(), bad)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (iter(()), "sized sequence"),
        ([], "output count"),
        ([["bad", 0.0]], "numeric values"),
        ([[1.0]], "dimension"),
        ([[float("nan"), 0.0]], "finite"),
        ([[float("inf"), 0.0]], "finite"),
        ([[4e38, 0.0]], "float32"),
    ],
)
def test_indexing_embedding_failures_leave_document_dirty(tmp_path, output, message):
    db = _fresh_db(tmp_path)
    doc_id = _insert_indexed_doc(db)
    with pytest.raises(ValueError, match=message):
        indexing.embed_doc(db, _FakeEmbedder(output), doc_id)
    with db.reader() as conn:
        assert conn.execute("SELECT vector_dirty FROM documents WHERE id=?", (doc_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0


def test_indexing_progress_single_doc_and_empty_page_checkpoint(tmp_path):
    db = _fresh_db(tmp_path)
    doc_id = _insert_indexed_doc(db)
    progress = []
    assert indexing.embed_pending(
        db, _FakeEmbedder([[0.5, 0.5]]), doc_id=doc_id, progress=lambda a, b: progress.append((a, b))
    ) == 1
    assert progress[-1] == (1, 1)

    class VanishingDb:
        calls = 0

        @contextmanager
        def reader(self):
            self.calls += 1

            class Conn:
                def execute(inner_self, sql, _params=()):
                    if "MAX(d.id)" in sql:
                        return _Rows([{"max_id": 1, "doc_count": 1}])
                    return _Rows([])

            yield Conn()

    assert indexing.embed_pending(VanishingDb(), _FakeEmbedder([])) == 0


def test_indexing_clear_empty_chunks_failed_cas_and_idle_stop(tmp_path):
    db = _fresh_db(tmp_path)
    doc_id = _insert_indexed_doc(db, "empty.md", "")
    with db.writer() as conn:
        indexing.clear_chunks(conn, doc_id)
        indexing.clear_chunks(conn, doc_id)
    worker = indexing.EmbeddingWorker(db, _FakeEmbedder([]))
    assert worker.status()["backlog"] == 1
    worker.stop()
    assert worker._stop.is_set()

    snapshot_row = {"doc_version": 1, "chunk_id": None, "text": None, "heading_path": None}

    class CasConn:
        in_transaction = True

        def execute(self, sql, _params=()):
            if sql.startswith("SELECT d.version"):
                return _Rows([snapshot_row])
            if sql.startswith("UPDATE documents"):
                return _Rows(rowcount=0)
            raise AssertionError(sql)

    class CasDb:
        def expected_embedding_binding(self):
            return EmbeddingBinding("fake/model", 2, EMBEDDING_PIPELINE, 1)

        @contextmanager
        def reader(self):
            yield CasConn()

        @contextmanager
        def writer(self):
            yield CasConn()

        def verify_embedding_binding(self, _conn, _expected):
            return None

    assert indexing.embed_doc(CasDb(), _FakeEmbedder([]), 1) is False


def test_embedding_worker_failure_backoff_status_and_checkpoint_cleanup(monkeypatch, caplog):
    class BrokenReaderDb:
        @contextmanager
        def reader(self):
            raise sqlite3.OperationalError("database unavailable")
            yield

    worker = indexing.EmbeddingWorker(BrokenReaderDb(), _FakeEmbedder([]), idle_interval=0.001)
    assert worker.status()["backlog"] is None

    attempts = []

    def sweep(_db, _embedder):
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 3:
            raise RuntimeError(f"failure-{len(attempts)}")
        worker._stop.set()
        return 0

    monkeypatch.setattr(indexing, "embed_pending", sweep)
    caplog.set_level(logging.DEBUG, logger="llm_wiki.indexing")
    worker.start()
    worker.notify()
    worker._thread.join(timeout=2)
    assert not worker._thread.is_alive()
    assert attempts == [1, 2, 3, 4]
    assert worker.status()["consecutive_failures"] == 0
    assert "sweep failed (3 consecutive)" in caplog.text


def test_embedding_worker_stop_warns_when_thread_does_not_finish(caplog):
    worker = indexing.EmbeddingWorker(SimpleNamespace(), _FakeEmbedder([]))

    class StuckThread:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 0

    worker._thread = StuckThread()
    caplog.set_level(logging.WARNING, logger="llm_wiki.indexing")
    worker.stop(timeout=0)
    assert "still running" in caplog.text


def test_search_filter_parsing_sql_and_empty_helpers(tmp_path):
    text, filters = search.parse_query_filters(
        'word title:"" title:Design path:"Docs/*.md" has:link has:backlink has:tag'
    )
    assert text == "word"
    sql, params = search._filter_sql(filters)
    assert "src_doc_id" in sql and "dst_doc_id" in sql and "doc_id FROM tags" in sql
    assert params == ["%design%", "docs/%.md"]
    assert search._fts_match("!!!") is None
    assert search._context_preview(None) is None
    assert search._context_preview(" \n ") is None
    assert search._tags_for_doc_ids(None, []) == {}
    assert search._docs_meta_for_ids(None, []) == {}
    assert search._doc_lengths_for_ids(None, []) == {}
    assert search._link_counts_for_ids(None, []) == {}
    assert search.SearchResult("p", "t", 1, "s", None, 1).to_dict()["path"] == "p"
    sql2, _ = search._filter_sql(search.QueryFilters(has=("tag", "link")))
    assert sql2.index("doc_id FROM tags") < sql2.index("src_doc_id")
    ignored, _ = search._filter_sql(search.QueryFilters(has=("unknown", "link")))
    assert "src_doc_id" in ignored


@pytest.mark.parametrize(
    ("identity", "values", "error"),
    [
        (("other", EMBEDDING_PIPELINE), [0.0, 0.0], EmbeddingBindingChanged),
        (("fake/model", EMBEDDING_PIPELINE), ["bad", 0.0], ValueError),
        (("fake/model", EMBEDDING_PIPELINE), [0.0], ValueError),
        (("fake/model", EMBEDDING_PIPELINE), [float("nan"), 0.0], ValueError),
        (("fake/model", EMBEDDING_PIPELINE), [4e38, 0.0], ValueError),
    ],
)
def test_search_query_embedding_validation(tmp_path, identity, values, error):
    db = _fresh_db(tmp_path)

    class QueryEmbedder:
        model_name, pipeline = identity

        def embed_query(self, _query):
            return values

    with pytest.raises(error):
        search._prepare_query_vector(db, QueryEmbedder(), "query")


def test_search_vector_orphans_reranking_and_rank_modes(monkeypatch):
    class VectorConn:
        calls = 0

        def execute(self, sql, _params=()):
            self.calls += 1
            if self.calls == 1:
                return _Rows([])
            if self.calls == 2:
                return _Rows([{"chunk_id": 9, "distance": 0.1}])
            return _Rows([])

    conn = VectorConn()
    assert search._vector(conn, b"v", 3) == []
    assert search._vector(conn, b"v", 3) == []

    class TiedVectorConn:
        def execute(self, sql, _params=()):
            if "embedding MATCH" in sql:
                return _Rows(
                    [
                        {"chunk_id": 21, "distance": 0.25},
                        {"chunk_id": 10, "distance": 0.25},
                        {"chunk_id": 20, "distance": 0.25},
                    ]
                )
            return _Rows(
                [
                    {"id": 10, "doc_id": 1, "ordinal": 0, "heading": None,
                     "text": "a", "heading_path": None, "char_start": 0,
                     "char_end": 1, "path_norm": "zeta.md"},
                    {"id": 20, "doc_id": 2, "ordinal": 0, "heading": None,
                     "text": "b0", "heading_path": None, "char_start": 0,
                     "char_end": 2, "path_norm": "alpha.md"},
                    {"id": 21, "doc_id": 2, "ordinal": 1, "heading": None,
                     "text": "b1", "heading_path": None, "char_start": 2,
                     "char_end": 4, "path_norm": "alpha.md"},
                ]
            )

    tied = search._vector(TiedVectorConn(), b"v", 3)
    assert [(doc_id, info["ordinal"]) for doc_id, info in tied] == [(2, 0), (1, 0)]

    params = search.FusionParams(proximity_weight=0.5)
    assert search._rerank_boost("", None, "q", params) == 0
    assert search._rerank_boost("q", {"distance": 2.0}, "q", params) == params.title_exact_boost
    assert search._rerank_boost("query long", {"distance": 0.5}, "query", params) == pytest.approx(
        params.title_prefix_boost + 0.25
    )

    monkeypatch.setattr(search, "_bm25", lambda *_a, **_k: [(1, 0.1), (2, 0.2)])
    monkeypatch.setattr(
        search,
        "_vector",
        lambda *_a, **_k: [
            (2, {"distance": 0.2}),
            (1, {"distance": 0.3}),
            (3, {"distance": 0.4}),
        ],
    )

    class Titles:
        def execute(self, _sql, _params=()):
            return _Rows([
                {"id": 1, "title": "unrelated", "path_norm": "zeta.md"},
                {"id": 2, "title": "other", "path_norm": "middle.md"},
                {"id": 3, "title": "", "path_norm": "alpha.md"},
            ])

    with pytest.raises(RuntimeError, match="prepared"):
        search._rank(Titles(), "query", mode="vector", k=3, folder=None, tags=None)
    bm, _, _ = search._rank(Titles(), "query", mode="bm25", k=3, folder=None, tags=None)
    vec, _, _ = search._rank(
        Titles(), "query", mode="vector", k=3, folder=None, tags=None, query_vector=b"v"
    )
    hybrid, _, _ = search._rank(
        Titles(), "query", mode="hybrid", k=3, folder=None, tags=None, query_vector=b"v"
    )
    assert [x[0] for x in bm] == [1, 2]
    assert [x[0] for x in vec] == [2, 1, 3]
    assert [x[0] for x in hybrid] == [2, 1, 3]


def test_vector_tie_at_vector_cap_uses_stable_subset_across_insertion_orders(tmp_path):
    paths = ["delta.md", "alpha.md", "charlie.md", "bravo.md"]
    query_vector = Embedder.serialize([1.0, 0.0])

    def tied_paths(name, insertion_order):
        root = tmp_path / name
        root.mkdir()
        db = _fresh_db(root)
        for path in insertion_order:
            _insert_indexed_doc(db, path, "# Same\nidentical vector passage")
        with db.writer() as conn:
            chunk_ids = [row["id"] for row in conn.execute("SELECT id FROM chunks")]
            conn.executemany(
                "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
                [(chunk_id, query_vector) for chunk_id in chunk_ids],
            )
        with db.reader() as conn:
            hits, _vec_info, _match = search._rank(
                conn,
                "identical vector passage",
                mode="vector",
                k=4,
                folder=None,
                tags=None,
                query_vector=query_vector,
                params=search.FusionParams(vector_factor=3, vector_cap=2),
            )
            return [
                conn.execute("SELECT path FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
                for doc_id, _score in hits
            ]

    forward = tied_paths("forward", paths)
    reverse = tied_paths("reverse", reversed(paths))

    assert forward == reverse == ["alpha.md", "bravo.md"]


def test_search_pass_filters_without_batch_and_passage_boundaries():
    class TagConn:
        def execute(self, _sql, _params=()):
            return _Rows([("x",)])

    d = {"id": 1, "folder": "docs/sub"}
    assert search._passes_filters(d, "other", None, TagConn()) is False
    assert search._passes_filters(d, "docs", ["x"], TagConn()) is True
    assert search._passes_filters(d, None, ["missing"], TagConn()) is False

    class PassageConn:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, _sql, _params=()):
            return _Rows(self.rows)

    assert search._passage_for_doc(PassageConn([]), 1, None, [], 10) is None
    blank = [{"ordinal": 0, "heading": None, "text": ""}]
    assert search._passage_for_doc(PassageConn(blank), 1, None, [], 10) is None
    rows = [
        {"ordinal": 0, "heading": "A", "text": "before"},
        {"ordinal": 1, "heading": "B", "text": "center token"},
        {"ordinal": 2, "heading": "C", "text": "after"},
    ]
    heading, passage, truncated = search._passage_for_doc(
        PassageConn(rows), 1, {"ordinal": 1}, ["token"], 40
    )
    assert heading == "B" and passage == "before\n\ncenter token\n\nafter" and truncated is False
    assert search._best_token_chunk(rows, []) == 0


def test_search_page_skips_stale_filtered_and_out_of_window_candidates(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    keep = _insert_indexed_doc(db, "keep.md", "# Keep\nneedle")
    old = _insert_indexed_doc(db, "old.md", "# Old\nneedle")
    future = _insert_indexed_doc(db, "future.md", "# Future\nneedle")
    deleted = _insert_indexed_doc(db, "deleted.md", "# Deleted\nneedle")
    with db.writer() as conn:
        conn.execute("UPDATE documents SET updated_at='2020-01-01' WHERE id=?", (old,))
        conn.execute("UPDATE documents SET updated_at='2030-01-01' WHERE id=?", (future,))
        conn.execute("UPDATE documents SET is_deleted=1 WHERE id=?", (deleted,))
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (keep,))

    def ranked(*_args, **_kwargs):
        return ([(9999, 9.0), (deleted, 8.0), (old, 7.0), (future, 6.0), (keep, 5.0)], {}, '"needle"')

    monkeypatch.setattr(search, "_rank", ranked)
    results, truncated = search.search_page(
        db,
        _FakeEmbedder([]),
        "needle",
        mode="bm25",
        since="2023-01-01",
        until="2025-01-01",
    )
    assert [r.path for r in results] == ["keep.md"]
    assert results[0].heading is None and truncated is False
    assert search.search(db, _FakeEmbedder([]), "needle", mode="bm25")[0].path == "old.md"


def test_search_page_invalid_mode_exact_truncation_and_operator_gate(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    first = _insert_indexed_doc(db, "first.md", "# First\nneedle")
    second = _insert_indexed_doc(db, "second.md", "# Second\nneedle")
    monkeypatch.setattr(
        search,
        "_rank",
        lambda *_a, **_k: ([(first, 2.0), (second, 1.0)], {}, None),
    )
    results, truncated = search.search_page(
        db, _FakeEmbedder([]), "needle", mode="invalid", top_k=1
    )
    assert [r.path for r in results] == ["first.md"] and truncated is True

    gated, _ = search.search_page(
        db, _FakeEmbedder([]), "needle title:second", mode="bm25", top_k=2
    )
    assert [r.path for r in gated] == ["second.md"]


def test_related_documents_empty_hits_orphans_deleted_and_best_update():
    class RelatedConn:
        def __init__(self):
            self.knn = 0

        def execute(self, sql, _params=()):
            if "v.embedding AS emb" in sql:
                return _Rows([{"emb": b"one"}, {"emb": b"two"}])
            if "embedding MATCH" in sql:
                self.knn += 1
                if self.knn == 1:
                    return _Rows([])
                return _Rows([
                    {"chunk_id": 10, "distance": 0.7},
                    {"chunk_id": 11, "distance": 0.2},
                    {"chunk_id": 13, "distance": 0.5},
                    {"chunk_id": 12, "distance": 0.1},
                ])
            if "SELECT id, doc_id FROM chunks" in sql:
                return _Rows([
                    {"id": 10, "doc_id": 2}, {"id": 11, "doc_id": 2},
                    {"id": 13, "doc_id": 2},
                ])
            if "SELECT id, path" in sql:
                return _Rows([{"id": 2, "path": "gone.md", "title": None, "folder": "", "is_deleted": 1}])
            raise AssertionError(sql)

    assert search._related_documents(RelatedConn(), 1) == []

    class NoBest(RelatedConn):
        def execute(self, sql, params=()):
            if "SELECT id, doc_id FROM chunks" in sql:
                return _Rows([{"id": 11, "doc_id": 1}])
            return super().execute(sql, params)

    assert search._related_documents(NoBest(), 1) == []


def test_search_passage_loop_boundaries():
    rows = [
        {"ordinal": 0, "heading": "A", "text": "left"},
        {"ordinal": 1, "heading": "B", "text": "center"},
        {"ordinal": 2, "heading": "C", "text": "right"},
    ]

    class Conn:
        def execute(self, _sql, _params=()):
            return _Rows(rows)

    assert search._passage_for_doc(Conn(), 1, {"ordinal": 1}, [], 50, max_chunks=1)[1] == "center"
    assert search._passage_for_doc(Conn(), 1, {"ordinal": 1}, [], 6)[1] == "center"
    assert search._passage_for_doc(Conn(), 1, {"ordinal": 1}, [], 20, max_chunks=2)[1] == (
        "center\n\nright"
    )


def test_assemble_context_candidate_boundaries(tmp_path, monkeypatch):
    db = _fresh_db(tmp_path)
    first = _insert_indexed_doc(db, "one.md", "# One\nalpha")
    second = _insert_indexed_doc(db, "two.md", "# Two\nalpha")

    monkeypatch.setattr(
        search, "_rank", lambda *_a, **_k: ([(first, 2.0), (second, 1.0)], {}, None)
    )
    limited = search.assemble_context(
        db, _FakeEmbedder([]), "alpha", mode="invalid", max_sources=1, max_chars=500
    )
    assert limited["count"] == 1 and limited["truncated"] is True

    monkeypatch.setattr(search, "_rank", lambda *_a, **_k: ([(9999, 2.0)], {}, None))
    missing = search.assemble_context(db, _FakeEmbedder([]), "alpha", mode="bm25")
    assert missing["count"] == 0

    monkeypatch.setattr(search, "_rank", lambda *_a, **_k: ([(first, 2.0)], {}, None))
    gated = search.assemble_context(
        db, _FakeEmbedder([]), "alpha title:two", mode="bm25"
    )
    assert gated["count"] == 0

    with db.writer() as conn:
        conn.execute("DELETE FROM chunks WHERE doc_id=?", (first,))
    no_passage = search.assemble_context(db, _FakeEmbedder([]), "alpha", mode="bm25")
    assert no_passage["count"] == 0
