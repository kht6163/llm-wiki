"""Local HuggingFace sentence-transformers embedding wrapper.

The model is loaded once per process (lazy) and guarded by a lock so concurrent
worker threads don't run forward passes on the same model object simultaneously.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Any

import sqlite_vec

from .embedding_contract import EMBEDDING_PIPELINE

# Query-embedding cache size. Search is read-dominant and the same queries recur
# (autocomplete, repeated agent lookups, popular terms); a forward pass on CPU is the
# dominant search latency, so a small LRU pays for itself. The model is fixed for the
# process lifetime (swapping it forces a reindex + restart), so cached vectors never
# go stale.
_QUERY_CACHE_MAX = 512


class Embedder:
    pipeline = EMBEDDING_PIPELINE
    enabled = True

    def __init__(self, model_name: str, model_revision: str = ""):
        self.model_name = model_name
        self.requested_revision = (model_revision or "").strip()
        self._resolved_revision: str | None = None
        self._model = None
        self._lock = threading.RLock()
        # E5-family models require "query:"/"passage:" prefixes; bge-m3 etc. do not.
        self._is_e5 = "e5" in model_name.lower()
        self._query_cache: OrderedDict[str, Any] = OrderedDict()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from sentence_transformers import SentenceTransformer

                        kwargs = (
                            {"revision": self.requested_revision}
                            if self.requested_revision
                            else {}
                        )
                        self._model = SentenceTransformer(self.model_name, **kwargs)
                        # Transformers records the immutable Hub commit used to load the
                        # weights on its config. Prefer it over a mutable requested tag such
                        # as ``main``; fall back only for local/custom model implementations.
                        resolved = ""
                        for module in getattr(self._model, "_modules", {}).values():
                            config = getattr(getattr(module, "auto_model", None), "config", None)
                            commit = getattr(config, "_commit_hash", None)
                            if isinstance(commit, str) and commit.strip():
                                resolved = commit.strip()
                                break
                        self._resolved_revision = (
                            resolved or self.requested_revision or "unresolved"
                        )
                    except Exception as e:
                        # A bad EMBEDDING_MODEL, no network/HF access, or a broken
                        # install would otherwise surface as a raw multi-line
                        # traceback on `serve`/`init-db`/`reindex`/`import`. Convert
                        # it to a ConfigError the CLI prints as one clear line with
                        # remediation steps.
                        from .config import ConfigError

                        raise ConfigError(
                            f"could not load embedding model {self.model_name!r}: "
                            f"{type(e).__name__}: {e}\n"
                            "  - check EMBEDDING_MODEL for typos\n"
                            "  - ensure network / HuggingFace access (or a populated local "
                            "model cache)\n"
                            "  - if dependencies are missing, run 'uv sync'\n"
                            "  - if you changed the model on purpose, run "
                            "'llm-wiki reindex --reembed' to rebuild vectors at the new dimension"
                        ) from e
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        """Eagerly load the model so the first request (and readiness) isn't slow."""
        self._load()

    @property
    def revision(self) -> str:
        """Immutable artifact identity used by the database embedding binding."""
        self._load()
        return self._resolved_revision or self.requested_revision or "unresolved"

    @property
    def dim(self) -> int:
        model = self._load()
        getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
        return int(getter())

    def _prefix(self, texts: list[str], kind: str) -> list[str]:
        if not self._is_e5:
            return texts
        p = "query: " if kind == "query" else "passage: "
        return [p + t for t in texts]

    def embed_passages(self, texts: list[str]):
        model = self._load()
        with self._lock:
            return model.encode(
                self._prefix(list(texts), "passage"),
                normalize_embeddings=True,
                convert_to_numpy=True,
            )

    def embed_query(self, text: str):
        # Cache by exact query text (the encoder is deterministic and the model is
        # fixed for the process). The cached vector is treated as read-only by callers
        # (they only serialize it), so it's shared rather than copied.
        with self._lock:
            cached = self._query_cache.get(text)
            if cached is not None:
                self._query_cache.move_to_end(text)
                return cached
        model = self._load()
        with self._lock:
            arr = model.encode(
                self._prefix([text], "query"),
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            vec = arr[0]
            self._query_cache[text] = vec
            self._query_cache.move_to_end(text)
            while len(self._query_cache) > _QUERY_CACHE_MAX:
                self._query_cache.popitem(last=False)
        return vec

    @staticmethod
    def serialize(vec) -> bytes:
        return sqlite_vec.serialize_float32([float(x) for x in vec])


class DisabledEmbedder(Embedder):
    """Stand-in when ``EMBEDDING_ENABLED=false``: no model load, no vectors."""

    enabled = False

    def __init__(self, model_name: str = "disabled", model_revision: str = ""):
        self.model_name = model_name
        self.requested_revision = (model_revision or "").strip()
        self._resolved_revision = "disabled"
        self._model = None
        self._lock = threading.RLock()
        self._is_e5 = False
        self._query_cache: OrderedDict[str, Any] = OrderedDict()

    def _load(self):
        raise RuntimeError("embeddings are disabled (EMBEDDING_ENABLED=false)")

    @property
    def is_loaded(self) -> bool:
        return False

    def warm(self) -> None:
        return None

    @property
    def revision(self) -> str:
        return "disabled"

    @property
    def dim(self) -> int:
        return 0

    def embed_passages(self, texts: list[str]):
        raise RuntimeError("embeddings are disabled (EMBEDDING_ENABLED=false)")

    def embed_query(self, text: str):
        raise RuntimeError("embeddings are disabled (EMBEDDING_ENABLED=false)")


@lru_cache(maxsize=8)
def get_embedder(model_name: str, model_revision: str = "") -> Embedder:
    return Embedder(model_name, model_revision)
