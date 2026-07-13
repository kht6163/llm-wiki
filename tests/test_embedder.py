"""A failed embedding-model load (bad EMBEDDING_MODEL, no network/HF access, broken
install) must surface as a ConfigError the CLI prints as one clear line, not a raw
SentenceTransformer traceback. serve/init-db/reindex --reembed/import all funnel
through Embedder._load, so wrapping it there covers every entry point."""

from types import SimpleNamespace

import pytest

from llm_wiki.config import ConfigError
from llm_wiki.embedding import Embedder


def test_embedder_load_failure_raises_config_error(monkeypatch):
    import sentence_transformers

    def boom(_name):
        raise OSError("Repository Not Found for url: ...")  # what HF raises for a bad id

    # _load does `from sentence_transformers import SentenceTransformer`, which reads
    # this module attribute — so patching it here makes the load fail without network.
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", boom)

    emb = Embedder("nonexistent/model-xyz")
    with pytest.raises(ConfigError) as ei:
        emb.warm()
    msg = str(ei.value)
    assert "nonexistent/model-xyz" in msg      # names the offending model
    assert "EMBEDDING_MODEL" in msg            # points at the knob to fix
    assert "reindex --reembed" in msg          # recovery hint for an intentional change
    assert "OSError" in msg                    # preserves the original error type


def test_embedder_uses_resolved_commit_for_binding_revision(monkeypatch):
    import sentence_transformers

    captured = {}

    class Model:
        _modules = {
            "0": SimpleNamespace(
                auto_model=SimpleNamespace(
                    config=SimpleNamespace(_commit_hash="resolved-commit")
                )
            )
        }

    def load(name, **kwargs):
        captured.update(name=name, **kwargs)
        return Model()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", load)
    embedder = Embedder("owner/model", "main")

    assert embedder.revision == "resolved-commit"
    assert captured == {"name": "owner/model", "revision": "main"}
