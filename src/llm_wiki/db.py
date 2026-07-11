"""SQLite layer: connection factory (WAL + sqlite-vec), schema, and a small
writer/reader transaction API.

Concurrency model:
- WAL mode lets many readers run while a single writer holds the lock.
- Writes go through ``writer()`` which takes a process-local lock *and* opens an
  ``BEGIN IMMEDIATE`` transaction, so the version compare-and-swap is race-free
  both in-process and (via SQLite's own locking + busy_timeout) across processes.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .embedding_contract import (
    EMBEDDING_PIPELINE,
    EmbeddingBinding,
    EmbeddingBindingChanged,
)

SCHEMA_VERSION = 11

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
-- Covers the default listing/autocomplete sort (WHERE is_deleted=0 ORDER BY
-- updated_at DESC) so it seeks instead of full-scanning + sorting the table.
CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(is_deleted, updated_at DESC);

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
-- (tag, doc_id) so the recurring "SELECT doc_id FROM tags WHERE tag=?" tag-filter
-- subquery (search + listing) is covered/index-only instead of fetching each row.
CREATE INDEX IF NOT EXISTS idx_tags_tag_doc ON tags(tag, doc_id);

-- Per-user favourites (pinned documents), surfaced at the top of the sidebar. A new
-- IF-NOT-EXISTS table, so it lands on existing DBs via ensure_schema without a numbered
-- migration. Both FKs cascade, so deleting a user or hard-purging a doc clears its pins.
CREATE TABLE IF NOT EXISTS favorites (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doc_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY (user_id, doc_id)
);

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
-- Every hot chunk read is "WHERE doc_id=? ORDER BY ordinal" (passage assembly,
-- read_chunk, BM25 section anchoring, related_documents); (doc_id, ordinal) serves
-- both the lookup and the sort, so no separate single-column index is needed.
CREATE INDEX IF NOT EXISTS idx_chunks_doc_ord ON chunks(doc_id, ordinal);

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
-- The activity feed reconcile use case ("what did actor X — or every OTHER actor —
-- change") filters by actor and pages newest-first (ORDER BY id DESC); (actor, id DESC)
-- serves the filter and the ordering without a scan+sort over the whole log.
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, id DESC);

-- Idempotency ledger for retry-safe writes (e.g. append_to_document). A client that
-- retries a request after a lost response replays its key; the prior result is
-- returned instead of applying the write a second time. Uniqueness is per
-- (scope, user, key); the row is written inside the same transaction as the write
-- it guards, so a UNIQUE collision rolls the duplicate write back.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  id             INTEGER PRIMARY KEY,
  scope          TEXT NOT NULL,        -- operation family, e.g. 'append'
  user_id        INTEGER NOT NULL,     -- the requesting principal
  idem_key       TEXT NOT NULL,        -- client-supplied unique operation id
  doc_id         INTEGER,              -- document the write landed on
  result_version INTEGER,              -- version produced by the original write
  result_path    TEXT,                 -- path produced by the original write
  created_at     TEXT NOT NULL,
  UNIQUE(scope, user_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at);
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
    # v5: index the default listing/autocomplete sort (is_deleted + updated_at DESC)
    # so large vaults seek instead of full-scanning + sorting on every page render.
    (5, "CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(is_deleted, updated_at DESC)"),
    # v7-v11: query-shaped indexes. Each statement is its own numbered (atomic) step
    # because the applier advances the stamp past a target once any entry for it runs,
    # so multi-statement changes need ascending targets. Create each replacement before
    # dropping the old single-column index so a read between steps is never unindexed.
    #  - chunks (doc_id, ordinal): covers "WHERE doc_id=? ORDER BY ordinal" (the hot
    #    chunk-read shape) without a sort, superseding idx_chunks_doc(doc_id).
    #  - tags (tag, doc_id): makes the "SELECT doc_id FROM tags WHERE tag=?" tag-filter
    #    subquery index-only, superseding idx_tags_tag(tag).
    #  - audit_log (actor, id DESC): serves the actor-filtered activity feed (paged by
    #    id DESC) without scanning + sorting the whole log.
    (7, "CREATE INDEX IF NOT EXISTS idx_chunks_doc_ord ON chunks(doc_id, ordinal)"),
    (8, "DROP INDEX IF EXISTS idx_chunks_doc"),
    (9, "CREATE INDEX IF NOT EXISTS idx_tags_tag_doc ON tags(tag, doc_id)"),
    (10, "DROP INDEX IF EXISTS idx_tags_tag"),
    (11, "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, id DESC)"),
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
        self._expected_embedding_binding: EmbeddingBinding | None = None
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

    def integrity_check(self, *, quick: bool = False) -> dict:
        """Run SQLite's built-in corruption checks plus a vec0 referential check, and
        report. ``PRAGMA integrity_check`` (or the cheaper ``quick_check``, which skips the
        per-index ordering scan) verifies the b-tree/page structure; ``PRAGMA
        foreign_key_check`` lists rows whose FK target is missing. ``orphan_vectors`` counts
        ``chunk_vectors`` rows whose ``chunk_id`` no longer exists in ``chunks`` — a class of
        corruption ``foreign_key_check`` CANNOT see, because ``chunk_vectors`` is a
        sqlite-vec virtual table and virtual tables can't declare FKs (so the embed worker
        upserting a vector for a chunk that a concurrent delete already removed leaves a
        danglers the search leg then silently skips). Returns
        ``{ok, check, integrity, foreign_key_violations, orphan_vectors}``; ``ok`` is True
        only when integrity reports ``['ok']`` AND there are no FK violations AND no orphan
        vectors. Read-only: safe on a live DB. Worth running before a backup/snapshot, since
        a consistent copy of a corrupt DB is still corrupt."""
        check = "quick_check" if quick else "integrity_check"
        with self.reader() as conn:
            integrity = [r[0] for r in conn.execute(f"PRAGMA {check}").fetchall()]
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            orphans = conn.execute(
                "SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id NOT IN (SELECT id FROM chunks)"
            ).fetchone()[0]
        violations = [
            {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]} for r in fk
        ]
        ok = integrity == ["ok"] and not violations and not orphans
        return {"ok": ok, "check": check, "integrity": integrity,
                "foreign_key_violations": violations, "orphan_vectors": orphans}

    def delete_orphan_vectors(self) -> int:
        """Delete ``chunk_vectors`` rows whose ``chunk_id`` has no surviving ``chunks`` row
        and return how many were removed. The one safe, well-defined repair for the orphan
        class ``integrity_check`` surfaces: such a vector can never resolve to a chunk (the
        search leg already skips it), so dropping it only reclaims space and stops it from
        crowding the KNN window. Runs in its own write transaction."""
        with self.writer() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM chunk_vectors WHERE chunk_id NOT IN (SELECT id FROM chunks)"
            ).fetchone()[0]
            if before:
                conn.execute(
                    "DELETE FROM chunk_vectors WHERE chunk_id NOT IN (SELECT id FROM chunks)")
            return before

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

    def initialize(
        self,
        embedding_model: str,
        embedding_dim: int,
        embedding_pipeline: str = EMBEDDING_PIPELINE,
    ) -> EmbeddingBinding:
        """Create the full schema and validate the process embedding binding."""
        if embedding_dim <= 0:
            raise ValueError("embedding dimension must be positive")
        self.ensure_schema()
        with self.writer() as conn:
            values = {
                key: get_meta(conn, key)
                for key in (
                    "embedding_model",
                    "embedding_dim",
                    "embedding_pipeline",
                    "embedding_epoch",
                )
            }
            model = values["embedding_model"]
            raw_dim = values["embedding_dim"]
            pipeline = values["embedding_pipeline"]
            raw_epoch = values["embedding_epoch"]
            table_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vectors'"
            ).fetchone()

            if all(value is None for value in values.values()):
                if table_row is not None:
                    raise RuntimeError(
                        "DB embedding binding metadata is missing while chunk_vectors "
                        "already exists. Re-embed with `llm-wiki reindex --reembed` "
                        "instead of inferring provenance for existing vectors."
                    )
                binding = EmbeddingBinding(
                    embedding_model, embedding_dim, embedding_pipeline, 1
                )
                set_meta(conn, "embedding_model", embedding_model)
                set_meta(conn, "embedding_dim", str(embedding_dim))
                set_meta(conn, "embedding_pipeline", embedding_pipeline)
                set_meta(conn, "embedding_epoch", "1")
            else:
                legacy = (
                    model is not None
                    and raw_dim is not None
                    and pipeline is None
                    and raw_epoch is None
                )
                complete = all(value is not None for value in values.values())
                if not (legacy or complete):
                    raise RuntimeError(
                        "Database embedding binding is corrupt: model, dimension, "
                        "pipeline, and epoch must be stored together."
                    )
                assert model is not None and raw_dim is not None
                if legacy:
                    stored_pipeline = embedding_pipeline
                    epoch_value = "1"
                else:
                    assert pipeline is not None and raw_epoch is not None
                    stored_pipeline = pipeline
                    epoch_value = raw_epoch

                try:
                    stored_dim = int(raw_dim)
                    stored_epoch = int(epoch_value)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Database embedding binding is corrupt: dimension and epoch "
                        "must be valid integers."
                    ) from exc
                if stored_dim <= 0 or stored_epoch <= 0:
                    raise RuntimeError(
                        "Database embedding binding is corrupt: dimension and epoch "
                        "must be positive integers."
                    )

                binding = EmbeddingBinding(
                    model, stored_dim, stored_pipeline, stored_epoch
                )
                requested = EmbeddingBinding(
                    embedding_model,
                    embedding_dim,
                    embedding_pipeline,
                    stored_epoch,
                )
                if binding != requested:
                    raise RuntimeError(
                        f"DB embedding binding is {binding}; this process requests "
                        f"{requested}. Re-embed with `llm-wiki reindex --reembed` "
                        "or restore the original embedding configuration."
                    )
                if legacy:
                    set_meta(conn, "embedding_pipeline", embedding_pipeline)
                    set_meta(conn, "embedding_epoch", "1")

            if table_row is None:
                conn.execute(
                    "CREATE VIRTUAL TABLE chunk_vectors USING vec0("
                    f"chunk_id INTEGER PRIMARY KEY, embedding float[{embedding_dim}] "
                    "distance_metric=cosine)"
                )
                conn.execute(
                    "UPDATE documents SET vector_dirty=1 WHERE is_deleted=0"
                )
            else:
                match = re.search(
                    r"\bembedding\s+float\s*\[\s*(\d+)\s*\]",
                    table_row["sql"] or "",
                    flags=re.IGNORECASE,
                )
                if match is None:
                    raise RuntimeError(
                        "Database embedding binding is corrupt: chunk_vectors does "
                        "not declare a readable embedding dimension."
                    )
                table_dim = int(match.group(1))
                if table_dim != binding.dim:
                    raise RuntimeError(
                        f"DB embedding binding dimension is {binding.dim}, but "
                        f"chunk_vectors uses {table_dim}. Re-embed with "
                        "`llm-wiki reindex --reembed` to rebuild vector storage."
                    )

        self._expected_embedding_binding = binding
        return binding

    def expected_embedding_binding(self) -> EmbeddingBinding:
        """Return the immutable embedding generation expected by this process."""
        binding = self._expected_embedding_binding
        if binding is None:
            raise RuntimeError(
                "Embedding binding is not initialized for this Database instance."
            )
        return binding

    def verify_embedding_binding(
        self, conn: sqlite3.Connection, expected: EmbeddingBinding
    ) -> None:
        """Fence a transaction against a process-local embedding generation."""
        if not conn.in_transaction:
            raise RuntimeError(
                "Embedding binding verification requires an active transaction."
            )
        values = {
            key: get_meta(conn, key)
            for key in (
                "embedding_model",
                "embedding_dim",
                "embedding_pipeline",
                "embedding_epoch",
            )
        }
        try:
            current = EmbeddingBinding(
                model=values["embedding_model"] or "",
                dim=int(values["embedding_dim"] or ""),
                pipeline=values["embedding_pipeline"] or "",
                epoch=int(values["embedding_epoch"] or ""),
            )
        except ValueError as exc:
            raise EmbeddingBindingChanged(
                "Database embedding binding is incomplete or invalid."
            ) from exc
        if current != expected:
            raise EmbeddingBindingChanged(
                f"Embedding binding changed from {expected} to {current}."
            )

    def rebind_model(
        self,
        embedding_model: str,
        embedding_dim: int,
        embedding_pipeline: str,
    ) -> EmbeddingBinding:
        """Atomically create a new embedding generation and dirty live documents."""
        if embedding_dim <= 0:
            raise ValueError("embedding dimension must be positive")
        self.ensure_schema()
        with self.writer() as conn:
            values = {
                key: get_meta(conn, key)
                for key in (
                    "embedding_model",
                    "embedding_dim",
                    "embedding_pipeline",
                    "embedding_epoch",
                )
            }
            model = values["embedding_model"]
            dim = values["embedding_dim"]
            pipeline = values["embedding_pipeline"]
            raw_epoch = values["embedding_epoch"]
            if all(value is None for value in values.values()):
                previous_epoch = 0
            elif (
                model is not None
                and dim is not None
                and pipeline is None
                and raw_epoch is None
            ):
                previous_epoch = 1
            elif all(value is not None for value in values.values()):
                assert raw_epoch is not None
                try:
                    previous_epoch = int(raw_epoch)
                except ValueError as exc:
                    raise RuntimeError(
                        "Database embedding epoch must be a valid integer before rebind."
                    ) from exc
                if previous_epoch <= 0:
                    raise RuntimeError(
                        "Database embedding epoch must be positive before rebind."
                    )
            else:
                raise RuntimeError(
                    "Database embedding binding is partial: model, dimension, "
                    "pipeline, and epoch must be stored together."
                )
            binding = EmbeddingBinding(
                embedding_model,
                embedding_dim,
                embedding_pipeline,
                previous_epoch + 1,
            )
            conn.execute("DROP TABLE IF EXISTS chunk_vectors")
            conn.execute(
                f"CREATE VIRTUAL TABLE chunk_vectors USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, embedding float[{int(embedding_dim)}] distance_metric=cosine)"
            )
            conn.execute(
                "UPDATE documents SET vector_dirty="
                "CASE WHEN is_deleted=0 THEN 1 ELSE 0 END"
            )
            set_meta(conn, "embedding_model", binding.model)
            set_meta(conn, "embedding_dim", str(binding.dim))
            set_meta(conn, "embedding_pipeline", binding.pipeline)
            set_meta(conn, "embedding_epoch", str(binding.epoch))

        self._expected_embedding_binding = binding
        return binding


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )
