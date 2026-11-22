"""SQLite 访问层。

并发策略：
- 连接打开 WAL，并设置 busy_timeout；
- 所有写事务显式以 BEGIN IMMEDIATE 开始，写操作在库级串行化；
- “换线 + 复算 + 撤销”在同一事务内完成，核发拿不到写锁时等待，
  因此并发核发不会在换线后漏撤。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .graph import Edge

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    station    TEXT NOT NULL,
    utc_time   TEXT NOT NULL,
    gauge_mm   INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (station, utc_time)
);

CREATE TABLE IF NOT EXISTS survey_lines (
    upstream        TEXT NOT NULL,
    downstream      TEXT NOT NULL,
    zero_offset_mm  INTEGER NOT NULL,
    PRIMARY KEY (upstream, downstream)
);

CREATE TABLE IF NOT EXISTS line_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO line_version (id, version) VALUES (1, 0)
ON CONFLICT(id) DO NOTHING;

CREATE TABLE IF NOT EXISTS approvals (
    approval_no           INTEGER PRIMARY KEY AUTOINCREMENT,
    utc_time              TEXT NOT NULL,
    upstream              TEXT NOT NULL,
    downstream            TEXT NOT NULL,
    upstream_reading_mm   INTEGER NOT NULL,
    downstream_reading_mm INTEGER NOT NULL,
    zero_offset_mm        INTEGER NOT NULL,
    fall_mm               INTEGER NOT NULL,
    line_version          INTEGER NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('valid', 'revoked')),
    revoke_reason         TEXT,
    created_at            TEXT NOT NULL,
    revoked_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def open_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 自动提交模式，事务全部显式管理
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE：立即获取写锁，配合 busy_timeout 串行化所有写事务。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- readings ----------

def insert_reading(conn: sqlite3.Connection, station: str, utc_time: str, gauge_mm: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO readings (station, utc_time, gauge_mm, created_at) VALUES (?, ?, ?, ?)",
        (station, utc_time, gauge_mm, utc_now_iso()),
    )
    return cur.rowcount == 1


def get_reading(conn: sqlite3.Connection, station: str, utc_time: str) -> int | None:
    row = conn.execute(
        "SELECT gauge_mm FROM readings WHERE station = ? AND utc_time = ?",
        (station, utc_time),
    ).fetchone()
    return None if row is None else int(row["gauge_mm"])


def count_readings(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"])


# ---------- survey lines ----------

def get_line_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT version FROM line_version WHERE id = 1").fetchone()["version"])


def get_edges(conn: sqlite3.Connection) -> list[Edge]:
    rows = conn.execute(
        "SELECT upstream, downstream, zero_offset_mm FROM survey_lines ORDER BY upstream, downstream"
    ).fetchall()
    return [Edge(r["upstream"], r["downstream"], int(r["zero_offset_mm"])) for r in rows]


def replace_lines(conn: sqlite3.Connection, edges: list[Edge]) -> int:
    """整批替换测线并把版本号 +1（须在写事务内调用）。"""
    conn.execute("DELETE FROM survey_lines")
    conn.executemany(
        "INSERT INTO survey_lines (upstream, downstream, zero_offset_mm) VALUES (?, ?, ?)",
        [(e.upstream, e.downstream, e.zero_offset_mm) for e in edges],
    )
    conn.execute("UPDATE line_version SET version = version + 1 WHERE id = 1")
    return get_line_version(conn)


# ---------- approvals ----------

def insert_approval(
    conn: sqlite3.Connection,
    *,
    utc_time: str,
    upstream: str,
    downstream: str,
    upstream_reading_mm: int,
    downstream_reading_mm: int,
    zero_offset_mm: int,
    fall_mm: int,
    line_version: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO approvals (
            utc_time, upstream, downstream, upstream_reading_mm,
            downstream_reading_mm, zero_offset_mm, fall_mm, line_version,
            status, revoke_reason, created_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'valid', NULL, ?, NULL)
        """,
        (
            utc_time, upstream, downstream, upstream_reading_mm,
            downstream_reading_mm, zero_offset_mm, fall_mm, line_version,
            utc_now_iso(),
        ),
    )
    return int(cur.lastrowid)


def revoke_approval(conn: sqlite3.Connection, approval_no: int, reason: str) -> None:
    conn.execute(
        "UPDATE approvals SET status = 'revoked', revoke_reason = ?, revoked_at = ? WHERE approval_no = ?",
        (reason, utc_now_iso(), approval_no),
    )


def get_approval(conn: sqlite3.Connection, approval_no: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM approvals WHERE approval_no = ?", (approval_no,)).fetchone()


def list_approvals(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status is None:
        rows = conn.execute("SELECT * FROM approvals ORDER BY approval_no").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status = ? ORDER BY approval_no", (status,)
        ).fetchall()
    return list(rows)


def count_approvals(conn: sqlite3.Connection, status: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) AS c FROM approvals WHERE status = ?", (status,)).fetchone()["c"]
    )
