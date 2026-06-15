"""SQLite layer: connection factory (WAL + sqlite-vec), schema, and a small
writer/reader transaction API.

Concurrency model:
- WAL mode lets many readers run while a single writer holds the lock.
- Writes go through ``writer()`` which takes a process-local lock *and* opens an
  ``BEGIN IMMEDIATE`` transaction, so the version compare-and-swap is race-free
  both in-process and (via SQLite's own locking + busy_timeout) across processes.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

SCHEMA_VERSION = 1

# Everything except the vector table, whose dimension is only known once the
# embedding model is loaded (see ensure_vector_table).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK(role IN ('admin','editor','viewer')),
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  key_prefix   TEXT NOT NULL UNIQUE,
  key_hash     TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id           INTEGER PRIMARY KEY,
  path         TEXT NOT NULL,
  path_norm    TEXT NOT NULL UNIQUE,
  title        TEXT,
  version      INTEGER NOT NULL DEFAULT 1,
  content_hash TEXT NOT NULL,
  folder       TEXT NOT NULL DEFAULT '',
  file_mtime   REAL,
  file_state   TEXT NOT NULL DEFAULT 'clean' CHECK(file_state IN ('clean','pending')),
  vector_dirty INTEGER NOT NULL DEFAULT 0,
  is_deleted   INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  updated_at   TEXT NOT NULL,
  updated_by   INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder);
CREATE INDEX IF NOT EXISTS idx_documents_dirty ON documents(vector_dirty);

CREATE TABLE IF NOT EXISTS tags (
  doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tag    TEXT NOT NULL,
  PRIMARY KEY (doc_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS revisions (
  id           INTEGER PRIMARY KEY,
  doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version      INTEGER NOT NULL,
  body         TEXT NOT NULL,
  title        TEXT,
  content_hash TEXT NOT NULL,
  author_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  op           TEXT NOT NULL DEFAULT 'edit'
                 CHECK(op IN ('create','edit','rename','delete','external-reconcile')),
  created_at   TEXT NOT NULL,
  UNIQUE(doc_id, version)
);
CREATE INDEX IF NOT EXISTS idx_revisions_doc ON revisions(doc_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_author ON revisions(author_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  title, body,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS chunks (
  id         INTEGER PRIMARY KEY,
  doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal    INTEGER NOT NULL,
  heading    TEXT,
  text       TEXT NOT NULL,
  char_start INTEGER NOT NULL,
  char_end   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS links (
  id            INTEGER PRIMARY KEY,
  src_doc_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dst_doc_id    INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  dst_path_norm TEXT NOT NULL,
  dst_name      TEXT NOT NULL,
  dst_is_path   INTEGER NOT NULL DEFAULT 0,
  link_type     TEXT NOT NULL,
  alias         TEXT,
  anchor        TEXT,
  is_resolved   INTEGER NOT NULL DEFAULT 0,
  char_start    INTEGER,
  raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_doc_id);
CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst_doc_id);
CREATE INDEX IF NOT EXISTS idx_links_dst_path ON links(dst_path_norm);
CREATE INDEX IF NOT EXISTS idx_links_dst_name ON links(dst_name);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a configured connection: row access by name, sqlite-vec loaded, WAL
    pragmas applied. Autocommit mode (isolation_level=None) — transactions are
    issued explicitly by the writer() context manager."""
    conn = sqlite3.connect(
        str(path), timeout=10.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._write_lock = threading.RLock()
        # One reusable connection per thread. Opening a connection reloads the
        # sqlite-vec C extension and re-applies the PRAGMAs, so doing it per
        # operation is the single most pervasive overhead on read paths; caching
        # it per thread removes that fixed cost. Connections are autocommit, so a
        # cached reader holds no open transaction (no WAL checkpoint stall), and
        # writes are serialized in-process by ``_write_lock``.
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self.connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        # Reuses the thread-local connection; each SELECT is its own implicit
        # transaction in autocommit mode, so nothing is left open between calls.
        yield self._conn()

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def close(self) -> None:
        """Close this thread's cached connection (mainly for tests/teardown)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- schema / meta -----------------------------------------------------
    def ensure_schema(self) -> None:
        # executescript() implicitly COMMITs, so it must run outside our explicit
        # writer() transaction. Connections are autocommit (isolation_level=None).
        with self._write_lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
                if get_meta(conn, "schema_version") is None:
                    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
            finally:
                conn.close()

    def ensure_vector_table(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError("embedding dimension must be positive")
        with self.writer() as conn:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, embedding float[{int(dim)}] distance_metric=cosine)"
            )

    def initialize(self, embedding_model: str, embedding_dim: int) -> None:
        """Create the full schema and bind the embedding model. Refuses to start if
        a different model (i.e. a different vector dimension) was used before."""
        self.ensure_schema()
        with self.writer() as conn:
            existing = get_meta(conn, "embedding_model")
            if existing is None:
                set_meta(conn, "embedding_model", embedding_model)
                set_meta(conn, "embedding_dim", str(embedding_dim))
            elif existing != embedding_model:
                raise RuntimeError(
                    f"DB was initialized with embedding model '{existing}' "
                    f"(dim={get_meta(conn, 'embedding_dim')}); .env now requests "
                    f"'{embedding_model}'. Re-embed with `llm-wiki reindex --reembed` "
                    f"or restore the original model."
                )
        self.ensure_vector_table(embedding_dim)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
