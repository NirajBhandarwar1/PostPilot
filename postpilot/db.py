"""Tiny SQLite store: one row per batch of ideas, plus a key/value table."""

import json
import sqlite3
from datetime import datetime, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL,          -- draft < sent < options_sent < built < posted, or skipped
    ideas_json  TEXT NOT NULL,
    chosen      INTEGER,
    built_at    TEXT,
    build_dir   TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS outcomes (       -- what a published post did, per build (O2, D20)
    batch_id      INTEGER NOT NULL,
    build_no      INTEGER NOT NULL DEFAULT 0, -- 1-based index into the batch's builds; 0 = legacy chosen
    posted_url    TEXT,
    posted_at     TEXT,
    recruiters    INTEGER DEFAULT 0,        -- recruiters who reached out after this post
    founders      INTEGER DEFAULT 0,        -- founders / hiring managers at AI startups
    other         INTEGER DEFAULT 0,        -- other inbound (peers, investors, ...)
    impressions   INTEGER,
    reactions     INTEGER,
    comments      INTEGER,
    profile_views INTEGER,
    updated_at    TEXT,
    PRIMARY KEY (batch_id, build_no)
)"""

OUTCOME_COLUMNS = ("posted_url", "posted_at", "recruiters", "founders", "other", "impressions",
                   "reactions", "comments", "profile_views", "updated_at")
BUILDS_MIGRATED = "migrated:builds_list"


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _backup(conn: sqlite3.Connection) -> None:
    """A full copy of the database before a one-time upgrade, so it can always be undone."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = sqlite3.connect(settings.data_dir / f"postpilot.db.bak-{stamp}")
    try:
        conn.backup(dst)
    finally:
        dst.close()


def _needs_migration(conn: sqlite3.Connection) -> bool:
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "outcomes_old" in tables or "build_no" not in _cols(conn, "outcomes"):
        return True
    return not conn.execute("SELECT 1 FROM meta WHERE key=?", (BUILDS_MIGRATED,)).fetchone()


def _migrate(conn: sqlite3.Connection) -> None:
    """One-time upgrade of an old database, in ONE transaction (all or nothing), after a backup:
    1. outcomes keyed by batch only -> keyed by (batch, build); also finishes a half-done
       earlier upgrade that left an `outcomes_old` table behind.
    2. old batches built before the builds list get one: [{idea: chosen, ...}], and their
       outcome row moves from build 0 to build 1, so a later rebuild can't hide or steal it."""
    if not _needs_migration(conn):
        return
    _backup(conn)
    conn.isolation_level = None                         # we manage the transaction ourselves
    try:
        conn.execute("BEGIN IMMEDIATE")                 # another process may be upgrading too
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = _cols(conn, "outcomes")
        if cols and "build_no" not in cols:             # the old table is still in place
            if "outcomes_old" in tables:                # ...next to a half-done upgrade: merge them
                common = ", ".join(c for c in cols if c in _cols(conn, "outcomes_old"))
                conn.execute(f"INSERT OR IGNORE INTO outcomes_old ({common}) SELECT {common} FROM outcomes")
                conn.execute("DROP TABLE outcomes")
            else:
                conn.execute("ALTER TABLE outcomes RENAME TO outcomes_old")
            tables.add("outcomes_old")
        conn.execute(OUTCOMES_DDL)
        if "outcomes_old" in tables:
            old = _cols(conn, "outcomes_old")
            keep = ", ".join(c for c in ("batch_id", *OUTCOME_COLUMNS) if c in old)
            build = "build_no" if "build_no" in old else "0"
            conn.execute(f"INSERT OR IGNORE INTO outcomes ({keep}, build_no) SELECT {keep}, {build} FROM outcomes_old")
            conn.execute("DROP TABLE outcomes_old")
        if not conn.execute("SELECT 1 FROM meta WHERE key=?", (BUILDS_MIGRATED,)).fetchone():
            rows = conn.execute("SELECT id, ideas_json, chosen, built_at, build_dir FROM batches "
                                "WHERE chosen IS NOT NULL AND built_at IS NOT NULL").fetchall()
            for r in rows:
                data = json.loads(r["ideas_json"])
                if data.get("builds"):
                    continue
                data["builds"] = [{"idea": r["chosen"], "option": None, "tag": "", "built_at": r["built_at"],
                                   "build_dir": r["build_dir"] or ""}]
                conn.execute("UPDATE batches SET ideas_json=? WHERE id=?", (json.dumps(data), r["id"]))
                conn.execute("UPDATE OR IGNORE outcomes SET build_no=1 WHERE batch_id=? AND build_no=0", (r["id"],))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (BUILDS_MIGRATED, now_iso()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.data_dir / "postpilot.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outcomes'").fetchone() and \
            not conn.execute("SELECT 1 FROM sqlite_master WHERE name='outcomes_old'").fetchone():
        conn.execute(OUTCOMES_DDL)
        if not conn.execute("SELECT 1 FROM batches LIMIT 1").fetchone():    # brand-new: nothing to upgrade
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (BUILDS_MIGRATED, now_iso()))
        conn.commit()
    _migrate(conn)
    return conn


def get_meta(key: str, default: str | None = None) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def add_meta_int(key: str, n: int) -> None:
    """Atomically add n to a numeric meta value (the daily paid-token counter)."""
    with connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET "
                  "value = CAST(CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER) AS TEXT)", (key, str(n)))


CLAIM_STALE_S = 2 * 3600       # a claim older than this belongs to a run that died: take it over


def claim(key: str) -> str:
    """Atomically claim a piece of work (one email reply) across processes. Returns 'claimed'
    (you do it), 'done' (already finished) or 'busy' (another run is on it right now)."""
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        value = row["value"] if row else ""
        if value and not value.startswith("busy:"):
            state = "done"
        elif value and (datetime.now(timezone.utc) - datetime.fromisoformat(value[5:])).total_seconds() < CLAIM_STALE_S:
            state = "busy"
        else:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, "busy:" + now_iso()))
            state = "claimed"
        conn.execute("COMMIT")
        return state
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def release(key: str, done: bool) -> None:
    """Finish a claim: mark it done, or drop it so the next run retries."""
    with connect() as c:
        if done:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, "done:" + now_iso()))
        else:
            c.execute("DELETE FROM meta WHERE key=?", (key,))


def add_batch(ideas: dict, status: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO batches(created_at,status,ideas_json) VALUES(?,?,?)",
                        (now_iso(), status, json.dumps(ideas)))
        return cur.lastrowid


def get_batch(batch_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def update_batch(batch_id: int, **fields) -> None:
    if "status" in fields:      # status only moves forward: decide it inside one write transaction
        edit_batch(batch_id, None, status=fields.pop("status"), **fields)
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE batches SET {cols} WHERE id=?", (*fields.values(), batch_id))


# A batch's status only moves forward; "skipped" is allowed only before anything was built.
STATUS_ORDER = ["draft", "sent", "options_sent", "built", "posted"]


def forward_status(old: str, new: str, data: dict) -> str:
    """'skipped' only before anything was built. A skipped batch comes back only when you ask for
    something on it (options, a build, a post); background steps like 'sent' never revive it."""
    built = bool(data.get("builds")) or old in ("built", "posted")
    if new == "skipped":
        return old if built else "skipped"
    if old == "skipped":
        return new if new in ("options_sent", "built", "posted") else old
    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    return new if rank.get(new, 0) >= rank.get(old, 0) else old


def edit_batch(batch_id: int, change=None, status: str | None = None, **fields) -> dict:
    """Read-merge-write in ONE write transaction (eng review D26): re-read the latest ideas_json,
    apply `change(data)` to it, and save. Two saves can no longer overwrite each other's changes,
    even when one of them spent minutes on model calls in between. Returns the saved data."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")               # take the write lock before reading
        row = conn.execute("SELECT status, ideas_json FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise ValueError(f"No batch #{batch_id}")
        data = json.loads(row["ideas_json"])
        if change:
            change(data)
        cols = {"ideas_json": json.dumps(data), **fields}
        if status:
            cols["status"] = forward_status(row["status"], status, data)
        conn.execute(f"UPDATE batches SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                     (*cols.values(), batch_id))
        conn.commit()
        return data
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


OUTCOME_FIELDS = {"posted_url", "posted_at", "recruiters", "founders", "other",
                  "impressions", "reactions", "comments", "profile_views"}


def latest_build_no(batch_id: int) -> int:
    """1-based number of the batch's most recent build; 0 for a batch with no builds."""
    row = get_batch(batch_id)
    return len(json.loads(row["ideas_json"]).get("builds", [])) if row else 0


def posted_build_no(batch_id: int) -> int:
    """The build you recorded as posted (the latest such), else the latest build. Inbound and
    stats belong to the post, not to whatever was built after it."""
    with connect() as c:
        row = c.execute("SELECT MAX(build_no) AS n FROM outcomes WHERE batch_id=? AND posted_url IS NOT NULL",
                        (batch_id,)).fetchone()
    return row["n"] if row and row["n"] is not None else latest_build_no(batch_id)


def _check_build_no(batch_id: int, n: int) -> None:
    """Valid: a recorded build, or 0 = 'no build' (a post recorded before any build, which keeps
    taking its inbound and stats after a later build)."""
    builds = latest_build_no(batch_id)
    if n == 0 and builds:
        with connect() as c:
            if c.execute("SELECT 1 FROM outcomes WHERE batch_id=? AND build_no=0", (batch_id,)).fetchone():
                return
    if not (1 <= n <= builds or (n == 0 and builds == 0)):
        raise ValueError(f"Batch #{batch_id} has builds 1-{builds}" if builds else
                         f"Batch #{batch_id} has no builds; use --build 0 or leave it out")


def set_outcome(batch_id: int, build_no: int | None = None, **fields) -> int:
    """Insert or update what a post did, for one build. Default: the latest build when you record
    the post itself, else the build you recorded as posted. Returns the build number used."""
    bad = set(fields) - OUTCOME_FIELDS
    if bad:
        raise ValueError(f"unknown outcome fields: {sorted(bad)}")
    if build_no is None:
        n = latest_build_no(batch_id) if "posted_url" in fields else posted_build_no(batch_id)
    else:
        n = build_no
    _check_build_no(batch_id, n)
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO outcomes(batch_id, build_no, updated_at) VALUES(?, ?, ?)",
                  (batch_id, n, now_iso()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE outcomes SET {cols}, updated_at=? WHERE batch_id=? AND build_no=?",
                      (*fields.values(), now_iso(), batch_id, n))
    return n


def get_outcome(batch_id: int, build_no: int | None = None) -> sqlite3.Row | None:
    n = posted_build_no(batch_id) if build_no is None else build_no
    with connect() as c:
        return c.execute("SELECT * FROM outcomes WHERE batch_id=? AND build_no=?", (batch_id, n)).fetchone()


def all_outcomes() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM outcomes ORDER BY batch_id, build_no").fetchall()


def recent_batches(limit: int = 10) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
