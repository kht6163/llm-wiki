"""Application configuration loaded from environment / .env via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Servers
    host: str = "127.0.0.1"
    gui_port: int = 8080
    mcp_port: int = 8081

    # Storage
    vault_path: Path = Path("./vault")
    db_path: Path = Path("./data/llm_wiki.db")

    # Embeddings (local HuggingFace sentence-transformers model)
    embedding_model: str = "intfloat/multilingual-e5-base"

    # Web session signing secret (empty -> generated and persisted in DB meta)
    session_secret: str = ""

    def ensure_dirs(self) -> None:
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
