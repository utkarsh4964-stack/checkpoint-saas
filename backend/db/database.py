"""
Storage for Checkpoint.

Local dev / tests: SQLite (no DATABASE_URL set).
Production: Postgres, selected automatically when DATABASE_URL is a
postgres:// or postgresql:// URL (e.g. Render's managed Postgres).

Repositories are written once against a small connection API
(`execute`, `executemany`, `executescript`, `commit`, `rollback`,
`close`) that both backends satisfy, and against dict-like row access
(`row["col"]`) that both sqlite3.Row and psycopg2's RealDictCursor
rows support. `?` placeholders are used everywhere; the Postgres path
rewrites them to `%s` before sending the query.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent / "checkpoint.db"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    # SQLAlchemy/psycopg2 dislike the old "postgres://" scheme Render/Heroku emit.
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
IS_POSTGRES = DATABASE_URL.startswith("postgresql://")

_QMARK = re.compile(r"\?")


def _to_pg(query: str) -> str:
    return _QMARK.sub("%s", query)


SCHEMA_COMMON = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_active_at TEXT NOT NULL,
    runtime_handle TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    UNIQUE(session_id, sequence)
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    checkpoint_id TEXT,
    type TEXT NOT NULL,
    intent TEXT NOT NULL,
    target TEXT,
    parameters TEXT NOT NULL,
    reversible INTEGER NOT NULL,
    status TEXT NOT NULL,
    risk_score INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    diff TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (checkpoint_id) REFERENCES checkpoints(id)
);

CREATE TABLE IF NOT EXISTS risk_findings (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity INTEGER NOT NULL,
    message TEXT NOT NULL,
    confidence REAL NOT NULL,
    FOREIGN KEY (action_id) REFERENCES actions(id)
);

CREATE TABLE IF NOT EXISTS rollback_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (checkpoint_id) REFERENCES checkpoints(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(session_id);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);
CREATE INDEX IF NOT EXISTS idx_findings_action ON risk_findings(action_id);
CREATE INDEX IF NOT EXISTS idx_rollback_session ON rollback_events(session_id);
"""


# --------------------------------------------------------------------------
# Postgres adapter: wraps a psycopg2 connection so callers can keep using
# the sqlite-shaped `conn.execute(query, params)` API with `?` placeholders.
# --------------------------------------------------------------------------

class _PGConnection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, query: str, params=()):
        import psycopg2.extras

        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(_to_pg(query), tuple(params))
        return cur

    def executemany(self, query: str, seq_of_params):
        cur = self._raw.cursor()
        cur.executemany(_to_pg(query), [tuple(p) for p in seq_of_params])
        return cur

    def executescript(self, script: str):
        cur = self._raw.cursor()
        cur.execute(script)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def get_connection():
    if IS_POSTGRES:
        import psycopg2

        raw = psycopg2.connect(DATABASE_URL, connect_timeout=10, application_name="checkpoint")
        return _PGConnection(raw)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA_COMMON)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def health_check() -> bool:
    try:
        with transaction() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def dumps(value) -> str:
    return json.dumps(value, default=str)


def loads(value):
    if value is None:
        return None
    return json.loads(value)
