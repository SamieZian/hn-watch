"""SQLite persistence. Single connection, WAL mode, sync calls.

All writes happen on the server event-loop thread; operations are sub-ms,
so blocking the loop is acceptable at this scale (aiosqlite is the upgrade path).
"""
import sqlite3
from datetime import datetime, timezone

from . import config

_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  interval_minutes INTEGER NOT NULL DEFAULT 30,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_run_at TEXT,
  last_cursor INTEGER,
  last_status TEXT,
  last_error TEXT,
  total_cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_items (
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  hn_id INTEGER NOT NULL,
  PRIMARY KEY (monitor_id, hn_id)
);

CREATE TABLE IF NOT EXISTS feed_items (
  id INTEGER PRIMARY KEY,
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  hn_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  hn_url TEXT NOT NULL,
  author TEXT,
  points INTEGER,
  num_comments INTEGER,
  created_at_hn TEXT,
  summary TEXT NOT NULL,
  matched_at TEXT NOT NULL,
  UNIQUE (monitor_id, hn_id)
);

CREATE TABLE IF NOT EXISTS swarm_runs (
  id INTEGER PRIMARY KEY,
  feed_item_id INTEGER NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  brief_md TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS swarm_agents (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES swarm_runs(id) ON DELETE CASCADE,
  angle TEXT NOT NULL,
  status TEXT NOT NULL,
  output_md TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ---- monitors ----------------------------------------------------------------

def create_monitor(name: str, prompt: str, interval_minutes: int) -> dict:
    cur = connect().execute(
        "INSERT INTO monitors (name, prompt, interval_minutes, created_at)"
        " VALUES (?, ?, ?, ?)",
        (name, prompt, interval_minutes, now_iso()),
    )
    connect().commit()
    return get_monitor(cur.lastrowid)


def get_monitor(monitor_id: int) -> dict | None:
    row = connect().execute(
        "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    return dict(row) if row else None


def list_monitors() -> list[dict]:
    rows = connect().execute("SELECT * FROM monitors ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_monitor(monitor_id: int, **fields) -> dict | None:
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        connect().execute(
            f"UPDATE monitors SET {cols} WHERE id = ?",
            (*fields.values(), monitor_id),
        )
        connect().commit()
    return get_monitor(monitor_id)


def delete_monitor(monitor_id: int) -> None:
    connect().execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
    connect().commit()


def add_monitor_cost(monitor_id: int, cost: float) -> None:
    connect().execute(
        "UPDATE monitors SET total_cost_usd = total_cost_usd + ? WHERE id = ?",
        (cost, monitor_id),
    )
    connect().commit()


# ---- seen / feed ---------------------------------------------------------------

def unseen(monitor_id: int, hn_ids: list[int]) -> set[int]:
    if not hn_ids:
        return set()
    qs = ",".join("?" * len(hn_ids))
    seen = {
        r[0]
        for r in connect().execute(
            f"SELECT hn_id FROM seen_items WHERE monitor_id = ? AND hn_id IN ({qs})",
            (monitor_id, *hn_ids),
        )
    }
    return set(hn_ids) - seen


def mark_seen(monitor_id: int, hn_ids: list[int]) -> None:
    connect().executemany(
        "INSERT OR IGNORE INTO seen_items (monitor_id, hn_id) VALUES (?, ?)",
        [(monitor_id, i) for i in hn_ids],
    )
    connect().commit()


def insert_feed_item(monitor_id: int, item, summary: str) -> dict | None:
    """Returns the inserted row, or None if it was a duplicate."""
    cur = connect().execute(
        "INSERT OR IGNORE INTO feed_items"
        " (monitor_id, hn_id, title, url, hn_url, author, points, num_comments,"
        "  created_at_hn, summary, matched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (monitor_id, item.hn_id, item.title, item.url, item.hn_url, item.author,
         item.points, item.num_comments, item.created_at, summary, now_iso()),
    )
    connect().commit()
    if cur.rowcount == 0:
        return None
    return get_feed_item(cur.lastrowid)


def get_feed_item(item_id: int) -> dict | None:
    row = connect().execute(
        "SELECT f.*, m.name AS monitor_name FROM feed_items f"
        " JOIN monitors m ON m.id = f.monitor_id WHERE f.id = ?",
        (item_id,),
    ).fetchone()
    return dict(row) if row else None


def list_feed(limit: int = 50, before_id: int | None = None) -> list[dict]:
    q = (
        "SELECT f.*, m.name AS monitor_name,"
        " (SELECT r.id FROM swarm_runs r WHERE r.feed_item_id = f.id"
        "  ORDER BY r.id DESC LIMIT 1) AS latest_run_id,"
        " (SELECT r.status FROM swarm_runs r WHERE r.feed_item_id = f.id"
        "  ORDER BY r.id DESC LIMIT 1) AS latest_run_status"
        " FROM feed_items f JOIN monitors m ON m.id = f.monitor_id"
    )
    args: list = []
    if before_id is not None:
        q += " WHERE f.id < ?"
        args.append(before_id)
    q += " ORDER BY f.id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in connect().execute(q, args)]


# ---- swarm ---------------------------------------------------------------------

def create_swarm_run(feed_item_id: int, angles: list[str]) -> tuple[int, list[int]]:
    conn = connect()
    cur = conn.execute(
        "INSERT INTO swarm_runs (feed_item_id, status, started_at) VALUES (?, 'running', ?)",
        (feed_item_id, now_iso()),
    )
    run_id = cur.lastrowid
    agent_ids = []
    for angle in angles:
        c = conn.execute(
            "INSERT INTO swarm_agents (run_id, angle, status, started_at)"
            " VALUES (?, ?, 'running', ?)",
            (run_id, angle, now_iso()),
        )
        agent_ids.append(c.lastrowid)
    conn.commit()
    return run_id, agent_ids


def update_swarm_run(run_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    connect().execute(
        f"UPDATE swarm_runs SET {cols} WHERE id = ?", (*fields.values(), run_id)
    )
    connect().commit()


def add_swarm_cost(run_id: int, cost: float) -> None:
    connect().execute(
        "UPDATE swarm_runs SET cost_usd = cost_usd + ? WHERE id = ?", (cost, run_id)
    )
    connect().commit()


def update_swarm_agent(agent_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    connect().execute(
        f"UPDATE swarm_agents SET {cols} WHERE id = ?", (*fields.values(), agent_id)
    )
    connect().commit()


def get_swarm_run(run_id: int) -> dict | None:
    row = connect().execute(
        "SELECT r.*, f.title, f.url, f.hn_url FROM swarm_runs r"
        " JOIN feed_items f ON f.id = r.feed_item_id WHERE r.id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["agents"] = [
        dict(a)
        for a in connect().execute(
            "SELECT * FROM swarm_agents WHERE run_id = ? ORDER BY id", (run_id,)
        )
    ]
    return run
