"""Start an isolated real llm-wiki web app for Playwright only."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from llm_wiki.config import Settings
from llm_wiki.db import Database
from llm_wiki.embedding import Embedder
from llm_wiki.embedding_contract import EMBEDDING_PIPELINE
from llm_wiki.events import EventHub
from llm_wiki.runtime import AppContext
from llm_wiki.search import DEFAULT_FUSION
from llm_wiki.services.auth import Principal, create_user
from llm_wiki.services.documents import DocumentService
from llm_wiki.web.app import create_web_app


class DeterministicEmbedder(Embedder):
    model_name = "playwright-deterministic-v1"
    pipeline = EMBEDDING_PIPELINE

    def __init__(self) -> None:
        # This fixture never loads a HuggingFace model.
        pass

    @property
    def dim(self) -> int:
        return 8

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [float(digest[index] - 127) for index in range(8)]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

def build_test_app(root: Path) -> FastAPI:
    port = int(os.environ["LLM_WIKI_E2E_PORT"])
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # pydantic-settings source control
        host="127.0.0.1",
        gui_port=port,
        mcp_port=int(os.environ["LLM_WIKI_E2E_MCP_PORT"]),
        vault_path=root / "vault",
        db_path=root / "data" / "wiki.db",
        embedding_model=DeterministicEmbedder.model_name,
        session_secret="playwright-session-secret",
    )
    settings.ensure_dirs()
    db = Database(settings.db_path)
    embedder = DeterministicEmbedder()
    db.initialize(embedder.model_name, embedder.dim, embedder.pipeline)
    events = EventHub()
    docs = DocumentService(
        db,
        embedder,
        settings.vault_path,
        events=events,
        search_params=DEFAULT_FUSION,
    )
    user_id = create_user(db, "admin", "e2e-secret12", "admin")
    admin = Principal(user_id, "admin", "admin")
    docs.create(admin, "start.md", "# 시작 안내\n\n키보드 탐색 기준 문서")
    docs.create(admin, "conflict.md", "# 충돌 문서\n\n최초 본문")
    return create_web_app(
        AppContext(
            settings=settings,
            db=db,
            embedder=embedder,
            docs=docs,
            events=events,
        )
    )


if __name__ == "__main__":
    root = Path(os.environ["LLM_WIKI_E2E_ROOT"]).resolve()
    uvicorn.run(
        build_test_app(root),
        host="127.0.0.1",
        port=int(os.environ["LLM_WIKI_E2E_PORT"]),
        log_level="warning",
        access_log=False,
    )
