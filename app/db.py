"""SQLite schema and connection helpers (standard library only)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_name  TEXT NOT NULL,
    recipient_phone TEXT NOT NULL,
    purpose         TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'manual',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','sending','sent','failed','rejected')),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Every body a draft ever had. Rows are never updated or deleted.
CREATE TABLE IF NOT EXISTS draft_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id),
    version_no  INTEGER NOT NULL,
    body        TEXT NOT NULL,
    author_id   INTEGER,
    author_name TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (draft_id, version_no)
);

-- Append only audit trail: who did what to which version.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id   INTEGER NOT NULL REFERENCES drafts(id),
    version_no INTEGER NOT NULL,
    action     TEXT NOT NULL,
    actor_id   INTEGER,
    actor_name TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- One row per send attempt handed to the sender.
CREATE TABLE IF NOT EXISTS outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id            INTEGER NOT NULL REFERENCES drafts(id),
    version_no          INTEGER NOT NULL,
    to_phone            TEXT NOT NULL,
    body                TEXT NOT NULL,
    provider            TEXT NOT NULL,
    provider_message_id TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL CHECK (status IN ('sent','failed')),
    error               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

-- A draft can be delivered at most once, whatever the application code does.
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_delivery
    ON outbox (draft_id) WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS drafts_status ON drafts (status, id);
CREATE INDEX IF NOT EXISTS events_draft ON events (draft_id, id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection in autocommit mode. Transactions are explicit (see ``transaction``)."""
    if db_path != ":memory:" and not db_path.startswith("file:"):
        folder = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,
        check_same_thread=False,
        timeout=10,
        uri=db_path.startswith("file:"),
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE so two reviewers cannot both act on the same stale row."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
