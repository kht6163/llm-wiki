"""Embedding generation identity shared by storage, publishers, and readers."""
from __future__ import annotations

from dataclasses import dataclass

EMBEDDING_PIPELINE = "passage-input-v1"


@dataclass(frozen=True)
class EmbeddingBinding:
    model: str
    dim: int
    pipeline: str
    epoch: int


class EmbeddingBindingChanged(RuntimeError):
    """Raised when a process observes a different embedding generation."""
