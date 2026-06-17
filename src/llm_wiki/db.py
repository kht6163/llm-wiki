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

SCHEMA_VERSION = 4

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

-- Explicitly-created folders. Folders are otherwise derived from document paths;
-- this table lets an empty folder persist as a first-class organizational unit
-- ("structure first, content later"). The DB is canonical; the vault directory is
-- a projection. Intermediate ancestors are derived at read time, so only the
-- created leaf folder needs a row.
CREATE TABLE IF NOT EXISTS folders (
  path       TEXT NOT NULL,
  path_norm  TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);

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
  via          TEXT NOT NULL DEFAULT 'web',  -- surface that authored it: web | mcp | cli
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
  id           INTEGER PRIMARY KEY,
  doc_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  heading      TEXT,
  text         TEXT NOT NULL,
  char_start   INTEGER NOT NULL,
  char_end     INTEGER NOT NULL,
  heading_path TEXT  -- "H1 > H2 > H3" breadcrumb of the chunk's enclosing headings
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

CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  actor   TEXT,                 -- username or api-key prefix ('-' for anonymous)
  via     TEXT NOT NULL,        -- web | mcp | cli
  action  TEXT NOT NULL,        -- login, login_failed, key_mint, doc_create, role_change, ...
  target  TEXT,                 -- document path / affected username / key prefix
  outcome TEXT NOT NULL,        -- ok | error | forbidden | conflict
  detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
"""

# Numbered forward migrations for EXISTING databases, applied in ascending order.
# New *tables* belong in SCHEMA_SQL (IF NOT EXISTS covers fresh and existing DBs);
# use a migration only for changes IF NOT EXISTS can't express — ALTER TABLE ADD
# COLUMN, index/constraint changes, data backfills.
#
# Each entry is (target_version, ddl) and MUST be a SINGLE SQL statement: the
# applier runs the statement and bumps schema_version in one transaction, so a
# failure rolls the step back atomically (no half-migrated DB) and leaves the
# version at the last fully-applied step (resumable). For a multi-step change, use
# several entries so each step is independently atomic. Example:
#   (3, "ALTER TABLE documents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"),
MIGRATIONS: list[tuple[int, str]] = [
    # v3: record which surface authored each revision (web | mcp | cli) so history
    # can tell a human edit from an agent edit. Pre-existing rows predate the MCP
    # write surface in practice, so 'web' is the safe backfill default.
    (3, "ALTER TABLE revisions ADD COLUMN via TEXT NOT NULL DEFAULT 'web'"),
    # v4: store each chunk's heading breadcrumb so search results can show the
    # section path and deep-link to it. NULL on pre-existing chunks until the
    # document is next edited or a full `reindex --reembed` rebuilds chunks.
    (4, "ALTER TABLE chunks ADD COLUMN heading_path TEXT"),
]


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
    # Cap WAL growth: auto-checkpoint every ~1000 pages on commit. The embedding
    # worker also issues a periodic wal_checkpoint(TRUNCATE) to reset the -wal file
    # that a long-lived reader connection could otherwise pin from growing back.
    conn.execute("PRAGMA wal_autocheckpoint=1000")
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
                self._apply_migrations(conn)
            finally:
                conn.close()

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """Bring an existing DB up to SCHEMA_VERSION. Fresh DBs get the latest
        schema from SCHEMA_SQL above and are simply stamped. Refuses to run against
        a DB written by a newer version of the code (downgrade guard)."""
        stored = get_meta(conn, "schema_version")
        if stored is None:  # brand-new DB — SCHEMA_SQL already created the latest schema
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))
            return
        current = int(stored)
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema_version is {current}, newer than this build supports "
                f"({SCHEMA_VERSION}). Upgrade llm-wiki to match the database."
            )
        for target, ddl in MIGRATIONS:
            if not (current < target <= SCHEMA_VERSION):
                continue
            # Apply the DDL and stamp the version together so a crash/failure can't
            # leave the schema changed with schema_version unbumped (a half-migrated
            # DB). On failure the step rolls back and the version stays at the last
            # fully-applied migration, so a re-run resumes from there.
            conn.execute("BEGIN IMMEDIATE")
            try:
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as e:
                    # ADD COLUMN is idempotent. A fresh DB already carries the column
                    # from SCHEMA_SQL and never runs migrations; but a DB whose stamp
                    # sits below this column's version (e.g. created with the current
                    # schema, then rewound) would re-attempt the ALTER. "Duplicate
                    # column" therefore means "already applied" — a no-op success, so
                    # still advance the stamp. Any other operational error is real.
                    if "duplicate column name" not in str(e).lower():
                        raise
                set_meta(conn, "schema_version", str(target))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            current = target
        if current < SCHEMA_VERSION:
            # Code is ahead of the DB but no DDL was needed up to SCHEMA_VERSION (e.g.
            # only new IF-NOT-EXISTS tables in SCHEMA_SQL); advance the stamp to match.
            set_meta(conn, "schema_version", str(SCHEMA_VERSION))

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
