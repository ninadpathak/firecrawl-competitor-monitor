"""SQLite storage for competitors. Monitor state lives in Firecrawl;
we only persist the competitor -> monitor mapping."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    urls TEXT NOT NULL,
    goal TEXT,
    schedule TEXT NOT NULL,
    monitor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _to_dict(row):
    d = dict(row)
    d["urls"] = json.loads(d["urls"])
    return d


def list_competitors():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM competitors ORDER BY created_at DESC").fetchall()
    return [_to_dict(r) for r in rows]


def get_competitor(competitor_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM competitors WHERE id = ?", (competitor_id,)
        ).fetchone()
    return _to_dict(row) if row else None


def add_competitor(name, urls, goal, schedule, monitor_id):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO competitors (name, urls, goal, schedule, monitor_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                json.dumps(urls),
                goal,
                schedule,
                monitor_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def delete_competitor(competitor_id):
    with connect() as conn:
        conn.execute("DELETE FROM competitors WHERE id = ?", (competitor_id,))


# --- API response cache (stale-while-revalidate; see app.py) ---


def cache_get(key):
    """Return (value, age_seconds) or None. Stale entries are still returned —
    the caller decides whether to refresh in the background."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value, fetched_at FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["value"]), time.time() - row["fetched_at"]


def cache_put(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO api_cache (key, value, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " fetched_at = excluded.fetched_at",
            (key, json.dumps(value), time.time()),
        )


def cache_delete(key):
    with connect() as conn:
        conn.execute("DELETE FROM api_cache WHERE key = ?", (key,))
