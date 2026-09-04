"""SQLite connection handling and migrations.

WAL plus foreign keys plus one transaction per transition is what makes
"crash then restart" boring instead of interesting.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .. import clock, config

SCHEMA_VERSION = 3

# Columns added after v1. Applied idempotently so an existing runtime root
# upgrades in place: SQLite has no "ADD COLUMN IF NOT EXISTS", and a failed
# migration on someone's live state is not an acceptable way to find that out.
_ADDED_COLUMNS = (
    ("runs", "model_resolved", "TEXT"),
    ("runs", "effort", "TEXT"),
    ("runs", "routing_source", "TEXT"),
    ("runs", "tools", "TEXT"),
    ("runs", "max_budget_usd", "REAL"),
    ("runs", "review_outcome", "TEXT"),
    ("jobs", "effort", "TEXT"),
    ("jobs", "permission_mode", "TEXT"),
    ("runs", "permission_mode", "TEXT"),
    ("runs", "bypass_permissions", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "worktree", "TEXT"),
)
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(path=None):
    """Open (and migrate) the state database."""
    target = Path(path) if path else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Apply the schema, then any additive column migrations. Idempotent."""
    conn.executescript(_SCHEMA_FILE.read_text())
    for table, column, column_type in _ADDED_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    current = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    if current != SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, clock.now_iso()),
        )
    return SCHEMA_VERSION


@contextmanager
def transaction(conn):
    """One atomic unit. Rolls back on any exception, including guard failures."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def all_rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()
