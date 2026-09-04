"""SQLite connection handling and migrations.

WAL plus foreign keys plus one transaction per transition is what makes
"crash then restart" boring instead of interesting.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .. import clock, config

SCHEMA_VERSION = 1
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


def migrate(conn):
    """Apply the schema. Idempotent: every statement is CREATE ... IF NOT EXISTS."""
    conn.executescript(_SCHEMA_FILE.read_text())
    current = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    if current is None:
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
