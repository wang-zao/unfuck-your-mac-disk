"""SQLite state & memory storage."""
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from . import config

_write_lock = threading.Lock()
_local = threading.local()


def connect() -> sqlite3.Connection:
    """One connection per thread, WAL mode, dict rows."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT,
    mode        TEXT,
    roots       TEXT,           -- JSON list of scanned roots
    status      TEXT,           -- running|done|error|cancelled
    started_at  REAL,
    finished_at REAL,
    total_bytes INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 0,
    total_dirs  INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL,
    path        TEXT NOT NULL,
    parent_path TEXT,
    name        TEXT,
    category    TEXT,
    is_dir      INTEGER,
    depth       INTEGER,
    size_bytes  INTEGER,
    file_count  INTEGER,
    dir_count   INTEGER,
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nodes_scan        ON nodes(scan_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent      ON nodes(scan_id, parent_path);
CREATE INDEX IF NOT EXISTS idx_nodes_size        ON nodes(scan_id, size_bytes DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_cat         ON nodes(scan_id, category);

CREATE TABLE IF NOT EXISTS analyses (
    path           TEXT PRIMARY KEY,
    category       TEXT,
    size_bytes     INTEGER,
    score          INTEGER,
    explanation    TEXT,
    reason         TEXT,
    risk           TEXT,
    recommendation TEXT,
    source         TEXT,        -- gpt-5|heuristic
    model          TEXT,
    created_at     REAL
);

CREATE TABLE IF NOT EXISTS deletions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path           TEXT,
    size_bytes     INTEGER,
    method         TEXT,        -- trash|quarantine|permanent
    status         TEXT,        -- done|restored|failed
    quarantine_path TEXT,
    note           TEXT,
    created_at     REAL,
    restored_at    REAL
);
"""


def init_db() -> None:
    conn = connect()
    with _write_lock:
        conn.executescript(SCHEMA)
        for k, v in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v)
            )
        conn.commit()


# ---------------- settings ----------------
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _write_lock:
        conn = connect()
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def all_settings() -> dict:
    rows = connect().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---------------- scans ----------------
def create_scan(label: str, mode: str, roots: str) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO scans(label,mode,roots,status,started_at) VALUES(?,?,?,?,?)",
            (label, mode, roots, "running", time.time()),
        )
        conn.commit()
        return cur.lastrowid


def finish_scan(scan_id: int, status: str, totals: dict, error: str = None) -> None:
    with _write_lock:
        conn = connect()
        conn.execute(
            "UPDATE scans SET status=?, finished_at=?, total_bytes=?, total_files=?, "
            "total_dirs=?, error=? WHERE id=?",
            (status, time.time(), totals.get("bytes", 0), totals.get("files", 0),
             totals.get("dirs", 0), error, scan_id),
        )
        conn.commit()


def insert_nodes(rows: Iterable[tuple]) -> None:
    with _write_lock:
        conn = connect()
        conn.executemany(
            "INSERT INTO nodes(scan_id,path,parent_path,name,category,is_dir,depth,"
            "size_bytes,file_count,dir_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()


def get_scan(scan_id: int) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()


def latest_scan() -> Optional[sqlite3.Row]:
    return connect().execute(
        "SELECT * FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_scans(limit: int = 25):
    return connect().execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def children(scan_id: int, parent_path: Optional[str], limit: int = 200):
    if parent_path is None:
        return connect().execute(
            "SELECT * FROM nodes WHERE scan_id=? AND depth=0 ORDER BY size_bytes DESC LIMIT ?",
            (scan_id, limit),
        ).fetchall()
    return connect().execute(
        "SELECT * FROM nodes WHERE scan_id=? AND parent_path=? ORDER BY size_bytes DESC LIMIT ?",
        (scan_id, parent_path, limit),
    ).fetchall()


def category_summary(scan_id: int):
    return connect().execute(
        "SELECT category, SUM(size_bytes) AS bytes, COUNT(*) AS items "
        "FROM nodes WHERE scan_id=? AND depth=0 GROUP BY category ORDER BY bytes DESC",
        (scan_id,),
    ).fetchall()


def top_items(scan_id: int, limit: int = 300, category: str = None, only_dirs=False):
    q = "SELECT * FROM nodes WHERE scan_id=?"
    args: list[Any] = [scan_id]
    if category:
        q += " AND category=?"
        args.append(category)
    if only_dirs:
        q += " AND is_dir=1"
    q += " ORDER BY size_bytes DESC LIMIT ?"
    args.append(limit)
    return connect().execute(q, args).fetchall()


# ---------------- analyses ----------------
def upsert_analysis(rec: dict) -> None:
    with _write_lock:
        conn = connect()
        conn.execute(
            "INSERT INTO analyses(path,category,size_bytes,score,explanation,reason,"
            "risk,recommendation,source,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET category=excluded.category,"
            "size_bytes=excluded.size_bytes,score=excluded.score,"
            "explanation=excluded.explanation,reason=excluded.reason,risk=excluded.risk,"
            "recommendation=excluded.recommendation,source=excluded.source,"
            "model=excluded.model,created_at=excluded.created_at",
            (rec["path"], rec.get("category"), rec.get("size_bytes"), rec.get("score"),
             rec.get("explanation"), rec.get("reason"), rec.get("risk"),
             rec.get("recommendation"), rec.get("source"), rec.get("model"), time.time()),
        )
        conn.commit()


def get_analysis(path: str) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM analyses WHERE path=?", (path,)).fetchone()


def get_analyses_for(paths: list[str]) -> dict:
    if not paths:
        return {}
    qmarks = ",".join("?" * len(paths))
    rows = connect().execute(
        f"SELECT * FROM analyses WHERE path IN ({qmarks})", paths
    ).fetchall()
    return {r["path"]: dict(r) for r in rows}


# ---------------- deletions ----------------
def record_deletion(rec: dict) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO deletions(path,size_bytes,method,status,quarantine_path,note,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (rec["path"], rec.get("size_bytes", 0), rec["method"], rec["status"],
             rec.get("quarantine_path"), rec.get("note"), time.time()),
        )
        conn.commit()
        return cur.lastrowid


def list_deletions(limit: int = 100):
    return connect().execute(
        "SELECT * FROM deletions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def get_deletion(del_id: int) -> Optional[sqlite3.Row]:
    return connect().execute("SELECT * FROM deletions WHERE id=?", (del_id,)).fetchone()


def mark_restored(del_id: int) -> None:
    with _write_lock:
        conn = connect()
        conn.execute(
            "UPDATE deletions SET status='restored', restored_at=? WHERE id=?",
            (time.time(), del_id),
        )
        conn.commit()
