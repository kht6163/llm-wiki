"""Local HuggingFace sentence-transformers embedding wrapper.

The model is loaded once per process (lazy) and guarded by a lock so concurrent
worker threads don't run forward passes on the same model object simultaneously.
"""
from __future__ import annotations

import threading
from functools import lru_cache

import sqlite_vec


class Embedder:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._lock = threading.RLock()
        # E5-family models require "query:"/"passage:" prefixes; bge-m3 etc. do not.
        self._is_e5 = "e5" in model_name.lower()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def warm(self) -> None:
        """Eagerly load the model so the first request (and readiness) isn't slow."""
        self._load()

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
        model = self._load()
        with self._lock:
            arr = model.encode(
                self._prefix([text], "query"),
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
        return arr[0]

    @staticmethod
    def serialize(vec) -> bytes:
        return sqlite_vec.serialize_float32([float(x) for x in vec])


@lru_cache(maxsize=4)
def get_embedder(model_name: str) -> Embedder:
    return Embedder(model_name)
