"""SQLite access: open connections, create the schema, seed the demo user."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from app.config import settings

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# There is no login in this prototype: every request acts as this one seeded user.
DEMO_USER_ID = 1
DEMO_USER_NAME = "Demo Student"
DEMO_USER_EMAIL = "demo.student@example.com"


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may run a request's dependency and handler on
    # different worker threads. Each connection still serves only one request.
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["balance_cents"]
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables if missing and seed the demo user. Safe to run repeatedly."""
    conn = connect()
    try:
        # WAL lets the UI keep reading while a webhook is writing.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_FILE.read_text())
        _add_column_if_missing(conn, "users", "stripe_customer_id", "TEXT")
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, name, email) VALUES (?, ?, ?)",
                (DEMO_USER_ID, DEMO_USER_NAME, DEMO_USER_EMAIL),
            )
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Tiny migration: lets a database created by an earlier stage gain new columns."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, always closed afterwards."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
