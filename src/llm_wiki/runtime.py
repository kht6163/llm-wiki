"""Process bootstrap: wire settings -> Database -> Embedder -> DocumentService.

``full=True`` loads the embedding model (slow) and creates/validates the vector
table; use it for serving and indexing. ``full=False`` only ensures the relational
schema (fast) and is enough for user/key management commands.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, get_settings
from .db import Database
from .embedding import DisabledEmbedder, Embedder, get_embedder
from .events import EventHub
from .indexing import EmbeddingWorker
from .search import FusionParams
from .services.documents import DocumentService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    embedder: Embedder
    docs: DocumentService
    events: EventHub
    embed_worker: EmbeddingWorker | None = None


def build_context(settings: Settings | None = None, *, full: bool = True,
                  start_embed_worker: bool = False) -> AppContext:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    try:
        if settings.embedding_enabled:
            embedder: Embedder = get_embedder(
                settings.embedding_model
            )  # model loads lazily on first use
            if full:
                db.initialize(settings.embedding_model, embedder.dim, embedder.pipeline)
            else:
                db.ensure_schema()
        else:
            embedder = DisabledEmbedder(settings.embedding_model)
            # Relational schema only — no vector table binding required.
            db.ensure_schema()
        events = EventHub()
        fusion = FusionParams(
            rrf_k=settings.rrf_k,
            candidate_factor=settings.search_candidate_factor,
            candidate_min=settings.search_candidate_min,
            vector_factor=settings.search_vector_factor,
            vector_cap=settings.search_vector_cap,
            title_exact_boost=settings.search_title_exact_boost,
            title_prefix_boost=settings.search_title_prefix_boost,
            proximity_weight=settings.search_proximity_weight,
        )
        # The worker is constructed here but started by the caller (after the startup
        # embed sweep) so writes during boot don't race an unstarted thread.
        embed_worker = (
            EmbeddingWorker(db, embedder)
            if start_embed_worker and settings.embedding_enabled
            else None
        )
        docs = DocumentService(db, embedder, settings.vault_path, events=events,
                               search_params=fusion, embed_worker=embed_worker)
        return AppContext(settings=settings, db=db, embedder=embedder, docs=docs,
                          events=events, embed_worker=embed_worker)
    except BaseException:
        db.close()
        raise
