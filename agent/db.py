"""SQLite persistence layer. One file, WAL mode, safe for a single-process agent."""
import sqlite3
from contextlib import contextmanager
from agent.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,           -- 'agent' | 'owner' | 'system'
    action TEXT NOT NULL,          -- tool name or event type
    detail TEXT NOT NULL,          -- JSON payload
    cost_usd REAL DEFAULT 0,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'fact' | 'preference' | 'task_result' | 'reflection'
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    importance INTEGER DEFAULT 3   -- 1..5, used for retrieval ranking
);
CREATE TABLE IF NOT EXISTS spend_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    vendor TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied | executed
    decided_ts TEXT,
    executed_ts TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',     -- open | done | blocked | dropped
    notes TEXT DEFAULT ''
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # two loops write concurrently; never crash on lock
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
