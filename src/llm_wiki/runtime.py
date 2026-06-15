"""Process bootstrap: wire settings -> Database -> Embedder -> DocumentService.

``full=True`` loads the embedding model (slow) and creates/validates the vector
table; use it for serving and indexing. ``full=False`` only ensures the relational
schema (fast) and is enough for user/key management commands.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, get_settings
from .db import Database
from .embedding import Embedder, get_embedder
from .services.documents import DocumentService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    embedder: Embedder
    docs: DocumentService


def build_context(settings: Settings | None = None, *, full: bool = True) -> AppContext:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    embedder = get_embedder(settings.embedding_model)  # model loads lazily on first use
    if full:
        db.initialize(settings.embedding_model, embedder.dim)
    else:
        db.ensure_schema()
    docs = DocumentService(db, embedder, settings.vault_path)
    return AppContext(settings=settings, db=db, embedder=embedder, docs=docs)
