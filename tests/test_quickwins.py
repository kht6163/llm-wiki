"""Quick wins: query-embedding cache + index/health gauges."""
from llm_wiki.db import SCHEMA_VERSION
from llm_wiki.metrics import collect_index_gauges


def test_query_embedding_is_cached(ctx):
    emb = ctx.embedder
    v1 = emb.embed_query("hybrid search latency")
    v2 = emb.embed_query("hybrid search latency")
    # Same text -> served from the LRU cache (same array object), not re-encoded.
    assert v1 is v2
    # A different query is a distinct vector.
    assert emb.embed_query("something else entirely") is not v1


def test_collect_index_gauges_counts(ctx, principals):
    docs, p = ctx.docs, principals["editor"]
    docs.create(p, "good.md", "# Good\n\nbody")
    docs.create(p, "bad.md", "points at [[nonexistent-target]]")
    stats = collect_index_gauges(ctx.db)
    assert stats["documents"] >= 2
    assert stats["broken_links"] >= 1            # the [[nonexistent-target]] link
    assert stats["schema_version"] == SCHEMA_VERSION
    assert stats["pending_files"] == 0           # all writes projected cleanly
