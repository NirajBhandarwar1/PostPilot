"""Tiny SQLite store: one row per batch of ideas, plus a key/value table."""

import json
import sqlite3
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- sent | built | skipped | draft
    ideas_json  TEXT NOT NULL,
    chosen      INTEGER,
    built_at    TEXT,
    build_dir   TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS outcomes (       -- what a published post actually did (eng review O2)
    batch_id      INTEGER PRIMARY KEY,
    posted_url    TEXT,
    posted_at     TEXT,
    recruiters    INTEGER DEFAULT 0,        -- recruiters who reached out after this post
    founders      INTEGER DEFAULT 0,        -- founders / hiring managers at AI startups
    other         INTEGER DEFAULT 0,        -- other inbound (peers, investors, ...)
    impressions   INTEGER,
    reactions     INTEGER,
    comments      INTEGER,
    profile_views INTEGER,
    updated_at    TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "postpilot.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def add_batch(ideas: dict, status: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO batches(created_at,status,ideas_json) VALUES(?,?,?)",
                        (now_iso(), status, json.dumps(ideas)))
        return cur.lastrowid


def get_batch(batch_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def update_batch(batch_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE batches SET {cols} WHERE id=?", (*fields.values(), batch_id))


OUTCOME_FIELDS = {"posted_url", "posted_at", "recruiters", "founders", "other",
                  "impressions", "reactions", "comments", "profile_views"}


def set_outcome(batch_id: int, **fields) -> None:
    """Insert or update what a post did. Only the given fields change."""
    bad = set(fields) - OUTCOME_FIELDS
    if bad:
        raise ValueError(f"unknown outcome fields: {sorted(bad)}")
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO outcomes(batch_id, updated_at) VALUES(?, ?)", (batch_id, now_iso()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE outcomes SET {cols}, updated_at=? WHERE batch_id=?",
                      (*fields.values(), now_iso(), batch_id))


def get_outcome(batch_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM outcomes WHERE batch_id=?", (batch_id,)).fetchone()


def all_outcomes() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM outcomes ORDER BY batch_id").fetchall()


def recent_batches(limit: int = 10) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
