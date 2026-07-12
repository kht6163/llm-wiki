"""Start an isolated real llm-wiki web app for Playwright only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
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

READY_PREFIX = "LLM_WIKI_E2E_READY "


def bind_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2048)
        return listener
    except Exception:
        listener.close()
        raise


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


def build_test_app(root: Path, *, gui_port: int, mcp_port: int) -> FastAPI:
    settings = Settings.model_validate(
        {
            "host": "127.0.0.1",
            "gui_port": gui_port,
            "mcp_port": mcp_port,
            "vault_path": root / "vault",
            "db_path": root / "data" / "wiki.db",
            "embedding_model": DeterministicEmbedder.model_name,
            "session_secret": "playwright-session-secret",
        }
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
    web = create_web_app(
        AppContext(
            settings=settings,
            db=db,
            embedder=embedder,
            docs=docs,
            events=events,
        )
    )
    web.state.e2e_db = db
    return web


if __name__ == "__main__":
    root = Path(os.environ["LLM_WIKI_E2E_ROOT"]).resolve()
    listener = bind_listener()
    host, port = listener.getsockname()
    mcp_port = 1 if port != 1 else 2
    app = build_test_app(root, gui_port=port, mcp_port=mcp_port)
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    print(READY_PREFIX + json.dumps({"url": f"http://{host}:{port}"}), flush=True)
    try:
        server.run(sockets=[listener])
    finally:
        app.state.e2e_db.close()
        listener.close()
