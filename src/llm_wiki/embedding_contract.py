"""Embedding generation identity shared by storage, publishers, and readers."""
from __future__ import annotations

from dataclasses import dataclass

EMBEDDING_PIPELINE = "passage-input-v1"
# Largest finite IEEE-754 binary32 value. sqlite-vec stores float32; accepting a
# larger finite Python float would silently serialize it as +/-infinity.
EMBEDDING_FLOAT32_MAX = float.fromhex("0x1.fffffep+127")


@dataclass(frozen=True)
class EmbeddingBinding:
    model: str
    dim: int
    pipeline: str
    epoch: int


class EmbeddingBindingChanged(RuntimeError):
    """Raised when a process observes a different embedding generation."""
