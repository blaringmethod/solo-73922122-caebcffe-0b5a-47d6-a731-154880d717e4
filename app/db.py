"""SQLite 访问层。

并发策略：
- 连接打开 WAL，并设置 busy_timeout；
- 所有写事务显式以 BEGIN IMMEDIATE 开始，写操作在库级串行化；
- “录入整批”“确认待确认读数”“换线 + 复算 + 撤销”“核发”都在单个写事务内完成，
  因此录入、确认与核发即使并发，核发读到的也是一致快照，只会采用同一精确时刻的
  两站**已确认**读数。

时刻存储：
- 新读数与核定单统一存 ``YYYY-MM-DDTHH:MM:SS.ffffffZ``（微秒、UTC）；
- 旧库整秒读数迁移为 ``.000000Z`` 且 ``time_status='pending'``，可查询、可确认；
- 已确认读数按 ``(station, utc_time)`` 唯一；待确认读数按 ``(station, 整秒)`` 唯一，
  两者允许并存（同一秒的旧整秒记录 + 该秒内精确时刻的新读数）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .graph import Edge
from .timeutil import (
    canonical_stored_utc,
    is_whole_second,
)

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station      TEXT NOT NULL,
    utc_time     TEXT NOT NULL,
    gauge_mm     INTEGER NOT NULL,
    time_status  TEXT NOT NULL CHECK (time_status IN ('confirmed', 'pending')),
    created_at   TEXT NOT NULL,
    confirmed_at TEXT,
    confirmed_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_confirmed
    ON readings(station, utc_time) WHERE time_status = 'confirmed';
CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_pending
    ON readings(station, utc_time) WHERE time_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_readings_station_time ON readings(station, utc_time);

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

_INIT_LOCK = threading.Lock()


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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def init_db(conn: sqlite3.Connection) -> None:
    """建表并按需把旧库（整秒读数）迁移到微秒 + 待确认模型。

    使用进程内锁避免同一进程多个请求同时迁移；真正的库级互斥由 BEGIN IMMEDIATE 保证。
    已初始化的库（user_version >= 2）直接返回，避免每个请求重复执行 DDL。
    """
    with _INIT_LOCK:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= 2:
            return
        if _table_exists(conn, "readings") and "time_status" not in _table_columns(conn, "readings"):
            _migrate_legacy_db(conn)
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 2")


def _migrate_legacy_db(conn: sqlite3.Connection) -> None:
    """v1（整秒文本）→ v2（微秒文本 + 待确认状态）。整段迁移在一个写事务内完成。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 多进程并发启动时，等锁后需重新确认是否已被别的进程迁移过。
        if "time_status" in _table_columns(conn, "readings"):
            conn.execute("COMMIT")
            return
        legacy_rows = conn.execute(
            "SELECT station, utc_time, gauge_mm, created_at FROM readings"
        ).fetchall()
        conn.execute("ALTER TABLE readings RENAME TO readings_legacy_v1")
        conn.execute(
            """
            CREATE TABLE readings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                station      TEXT NOT NULL,
                utc_time     TEXT NOT NULL,
                gauge_mm     INTEGER NOT NULL,
                time_status  TEXT NOT NULL CHECK (time_status IN ('confirmed', 'pending')),
                created_at   TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_by TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX ux_readings_confirmed ON readings(station, utc_time) "
            "WHERE time_status = 'confirmed'"
        )
        conn.execute(
            "CREATE UNIQUE INDEX ux_readings_pending ON readings(station, utc_time) "
            "WHERE time_status = 'pending'"
        )
        conn.execute("CREATE INDEX idx_readings_station_time ON readings(station, utc_time)")

        for row in legacy_rows:
            utc_time = canonical_stored_utc(row["utc_time"])
            # 旧库的整秒读数一律待确认；旧库若已有精确读数则视为已确认。
            status = "pending" if is_whole_second(utc_time) else "confirmed"
            conn.execute(
                """
                INSERT INTO readings
                    (station, utc_time, gauge_mm, time_status, created_at, confirmed_at, confirmed_by)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    row["station"],
                    utc_time,
                    int(row["gauge_mm"]),
                    status,
                    row["created_at"],
                    row["created_at"] if status == "confirmed" else None,
                ),
            )
        conn.execute("DROP TABLE readings_legacy_v1")

        # 核定单时刻也统一成微秒格式；快照读数保持不变，按号查询仍显示核发时的数据。
        if _table_exists(conn, "approvals"):
            for row in conn.execute("SELECT approval_no, utc_time FROM approvals").fetchall():
                conn.execute(
                    "UPDATE approvals SET utc_time = ? WHERE approval_no = ?",
                    (canonical_stored_utc(row["utc_time"]), row["approval_no"]),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


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

def insert_reading(
    conn: sqlite3.Connection,
    station: str,
    utc_time: str,
    gauge_mm: int,
    *,
    time_status: str = "confirmed",
    confirmed_by: str | None = None,
) -> int | None:
    """插入一条读数；唯一冲突返回 None（调用方负责整单回退）。"""
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO readings
            (station, utc_time, gauge_mm, time_status, created_at, confirmed_at, confirmed_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            station,
            utc_time,
            gauge_mm,
            time_status,
            now,
            now if time_status == "confirmed" else None,
            confirmed_by,
        ),
    )
    return int(cur.lastrowid) if cur.rowcount == 1 else None


def insert_pending_reading(
    conn: sqlite3.Connection, station: str, whole_second_utc: str, gauge_mm: int
) -> int | None:
    return insert_reading(conn, station, whole_second_utc, gauge_mm, time_status="pending")


def get_reading_row(
    conn: sqlite3.Connection,
    station: str,
    utc_time: str,
    *,
    time_status: str | None = None,
) -> sqlite3.Row | None:
    sql = "SELECT * FROM readings WHERE station = ? AND utc_time = ?"
    params: list[object] = [station, utc_time]
    if time_status is not None:
        sql += " AND time_status = ?"
        params.append(time_status)
    return conn.execute(sql, params).fetchone()


def get_reading(conn: sqlite3.Connection, station: str, utc_time: str) -> int | None:
    """只取已确认读数；核发绝不使用待确认数据。"""
    row = get_reading_row(conn, station, utc_time, time_status="confirmed")
    return None if row is None else int(row["gauge_mm"])


def get_pending_reading(conn: sqlite3.Connection, station: str, whole_second_utc: str) -> sqlite3.Row | None:
    return get_reading_row(conn, station, whole_second_utc, time_status="pending")


def confirm_reading(
    conn: sqlite3.Connection, *, reading_id: int, utc_time: str, operator: str
) -> None:
    """把待确认读数改成已确认并落到精确时刻（须在写事务内调用）。"""
    conn.execute(
        """
        UPDATE readings
           SET utc_time = ?, time_status = 'confirmed', confirmed_at = ?, confirmed_by = ?
         WHERE id = ? AND time_status = 'pending'
        """,
        (utc_time, utc_now_iso(), operator, reading_id),
    )


def delete_reading(conn: sqlite3.Connection, reading_id: int) -> None:
    conn.execute("DELETE FROM readings WHERE id = ?", (reading_id,))


def count_readings(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"])


def count_readings_by_status(conn: sqlite3.Connection, time_status: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM readings WHERE time_status = ?", (time_status,)
        ).fetchone()["c"]
    )


def list_readings(
    conn: sqlite3.Connection,
    *,
    station: str | None = None,
    utc_time: str | None = None,
    time_status: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM readings"
    conds: list[str] = []
    params: list[object] = []
    if station is not None:
        conds.append("station = ?")
        params.append(station)
    if utc_time is not None:
        conds.append("utc_time = ?")
        params.append(utc_time)
    if time_status is not None:
        conds.append("time_status = ?")
        params.append(time_status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    # 按实际精确时刻排序；同一时刻先列已确认，再列待确认。
    sql += " ORDER BY utc_time, station, CASE time_status WHEN 'confirmed' THEN 0 ELSE 1 END, id"
    return list(conn.execute(sql, params).fetchall())


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
