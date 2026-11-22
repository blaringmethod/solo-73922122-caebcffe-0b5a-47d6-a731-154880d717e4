"""微秒时刻、旧整秒待确认、确认合并与核发快照的端到端测试。"""

from __future__ import annotations

import os
import sqlite3
import threading

from app import db as db_mod

T_PRECISE = "2026-11-22T08:00:00.123456Z"
T_PRECISE_2 = "2026-11-22T08:00:00.654321Z"
T_WHOLE = "2026-11-22T08:00:00Z"


def _post_readings(client, items):
    return client.post(
        "/readings",
        json={"readings": [{"station": s, "utc_time": t, "gauge_mm": mm} for s, t, mm in items]},
    )


def _import_legacy(client, items):
    return client.post(
        "/readings/legacy-import",
        json={"readings": [{"station": s, "utc_time": t, "gauge_mm": mm} for s, t, mm in items]},
    )


def _confirm(client, station, legacy, exact, operator="巡测员-张三"):
    return client.post(
        "/readings/confirm",
        json={
            "station": station,
            "legacy_utc_time": legacy,
            "utc_time": exact,
            "operator": operator,
        },
    )


def _lines(client, lines):
    r = client.put("/lines", json={"lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


def _issue(client, t, up="S1", down="S2"):
    return client.post("/approvals/issue", json={"utc_time": t, "upstream": up, "downstream": down})


# ---------- 精确时刻录入 ----------

def test_microsecond_precision_is_stored_and_returned(client):
    r = _post_readings(client, [("S1", T_PRECISE, 1000)])
    assert r.status_code == 201, r.text
    rows = client.get("/readings").json()
    assert len(rows) == 1
    assert rows[0]["utc_time"] == T_PRECISE
    assert rows[0]["time_status"] == "confirmed"


def test_same_instant_different_timezone_is_duplicate(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000)]).status_code == 201
    # 同一瞬间的 +08:00 写法：16:00:00.123456+08:00 == 08:00:00.123456Z
    r = _post_readings(client, [("S1", "2026-11-22T16:00:00.123456+08:00", 1000)])
    assert r.status_code == 409, r.text
    assert "已有水尺读数" in r.json()["detail"]
    assert len(client.get("/readings").json()) == 1


def test_duplicate_inside_one_batch_rejected_whole(client):
    body = {
        "readings": [
            {"station": "S1", "utc_time": T_PRECISE, "gauge_mm": 1000},
            {"station": "S1", "utc_time": "2026-11-22T16:00:00.123456+08:00", "gauge_mm": 1000},
            {"station": "S2", "utc_time": T_PRECISE, "gauge_mm": 1020},
        ]
    }
    r = client.post("/readings", json=body)
    assert r.status_code == 409, r.text
    assert client.get("/readings").json() == []


def test_same_station_same_second_different_micros_both_kept(client):
    assert _post_readings(
        client,
        [("S1", T_PRECISE, 1000), ("S1", T_PRECISE_2, 1001)],
    ).status_code == 201
    rows = client.get("/readings?station=S1").json()
    assert [x["utc_time"] for x in rows] == [T_PRECISE, T_PRECISE_2]


def test_query_orders_by_actual_instant(client):
    assert _post_readings(
        client,
        [
            ("S2", "2026-11-22T08:00:00.900000Z", 1),
            ("S1", "2026-11-22T08:00:00.100000Z", 2),
            ("S3", "2026-11-22T08:00:00.500000Z", 3),
        ],
    ).status_code == 201
    rows = client.get("/readings").json()
    assert [x["utc_time"] for x in rows] == [
        "2026-11-22T08:00:00.100000Z",
        "2026-11-22T08:00:00.500000Z",
        "2026-11-22T08:00:00.900000Z",
    ]


def test_query_by_precise_time_and_timezone(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000)]).status_code == 201
    assert len(client.get(f"/readings?utc_time={T_PRECISE}").json()) == 1
    assert len(client.get("/readings?utc_time=2026-11-22T16:00:00.123456%2B08:00").json()) == 1
    assert client.get("/readings?utc_time=2026-11-22T08:00:00.123457Z").json() == []


# ---------- 旧整秒：待确认 + 补报 + 确认 ----------

def test_legacy_import_is_pending_and_queryable_and_coexists(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000)]).status_code == 201
    r = _import_legacy(client, [("S1", T_WHOLE, 999)])
    assert r.status_code == 201, r.text
    assert r.json() == {"inserted": 1, "pending_total": 1}

    rows = client.get("/readings?station=S1").json()
    # 同一秒内两条并存，按实际时刻排序：整秒(.000000) 在前
    assert [(x["utc_time"], x["gauge_mm"], x["time_status"]) for x in rows] == [
        ("2026-11-22T08:00:00.000000Z", 999, "pending"),
        (T_PRECISE, 1000, "confirmed"),
    ]
    pending = client.get("/readings?time_status=pending").json()
    assert len(pending) == 1 and pending[0]["utc_time"] == "2026-11-22T08:00:00.000000Z"


def test_legacy_import_rejects_non_whole_second_and_duplicate(client):
    assert _post_readings(client, [("S1", T_WHOLE, 1000)]).status_code == 201
    # 已经有精确读数并不妨碍补报同秒的整秒旧记录
    assert _import_legacy(client, [("S1", T_WHOLE, 1000)]).status_code == 201
    assert _import_legacy(client, [("S1", T_WHOLE, 1000)]).status_code == 409
    assert _import_legacy(client, [("S2", T_PRECISE, 1000)]).status_code == 422


def test_issue_ignores_pending_legacy_reading(client):
    _import_legacy(client, [("S1", T_WHOLE, 1000), ("S2", T_WHOLE, 1020)])
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, T_WHOLE)
    assert r.status_code == 422
    assert "待确认" in r.json()["detail"] and "请先确认" in r.json()["detail"]


def test_confirm_moves_pending_to_exact_time(client):
    _import_legacy(client, [("S1", T_WHOLE, 1000)])
    r = _confirm(client, "S1", T_WHOLE, T_PRECISE)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["merged"] is False
    assert data["reading"]["utc_time"] == T_PRECISE
    assert data["reading"]["time_status"] == "confirmed"
    assert data["reading"]["confirmed_by"] == "巡测员-张三"
    assert client.get("/readings?time_status=pending").json() == []


def test_confirm_requires_exact_time_within_legacy_second(client):
    _import_legacy(client, [("S1", T_WHOLE, 1000)])
    r = _confirm(client, "S1", T_WHOLE, "2026-11-22T08:00:01.000001Z")
    assert r.status_code == 422
    assert "一秒内" in r.json()["detail"]
    # 原数据与待确认状态保留
    rows = client.get("/readings").json()
    assert rows[0]["utc_time"] == "2026-11-22T08:00:00.000000Z"
    assert rows[0]["time_status"] == "pending"


def test_confirm_unknown_pending_404(client):
    r = _confirm(client, "S1", T_WHOLE, T_PRECISE)
    assert r.status_code == 404


def test_confirm_same_value_merges(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000)]).status_code == 201
    _import_legacy(client, [("S1", T_WHOLE, 1000)])
    r = _confirm(client, "S1", T_WHOLE, T_PRECISE)
    assert r.status_code == 200, r.text
    assert r.json()["merged"] is True
    rows = client.get("/readings?station=S1").json()
    assert len(rows) == 1
    assert rows[0]["utc_time"] == T_PRECISE and rows[0]["gauge_mm"] == 1000
    assert rows[0]["time_status"] == "confirmed"


def test_confirm_conflicting_value_rejected_and_preserved(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000)]).status_code == 201
    _import_legacy(client, [("S1", T_WHOLE, 999)])
    r = _confirm(client, "S1", T_WHOLE, T_PRECISE)
    assert r.status_code == 409
    assert "不同" in r.json()["detail"]
    rows = client.get("/readings?station=S1").json()
    assert [(x["utc_time"], x["gauge_mm"], x["time_status"]) for x in rows] == [
        ("2026-11-22T08:00:00.000000Z", 999, "pending"),
        (T_PRECISE, 1000, "confirmed"),
    ]


# ---------- 核发：同一精确时刻的两站读数 ----------

def test_issue_uses_only_confirmed_same_microsecond(client):
    assert _post_readings(
        client,
        [("S1", T_PRECISE, 1000), ("S2", T_PRECISE, 1020), ("S2", T_PRECISE_2, 9999)],
    ).status_code == 201
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, T_PRECISE)
    assert r.status_code == 201, r.text
    assert r.json()["utc_time"] == T_PRECISE
    assert r.json()["downstream_reading_mm"] == 1020
    assert r.json()["fall_mm"] == -5

    # 另一精确时刻缺上游读数 -> 拒发，绝不混用相邻时刻的读数
    r2 = _issue(client, T_PRECISE_2)
    assert r2.status_code == 422 and "缺读数" in r2.json()["detail"]


def test_approval_snapshot_survives_reading_changes(client):
    assert _post_readings(client, [("S1", T_PRECISE, 1000), ("S2", T_PRECISE, 1020)]).status_code == 201
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    created = _issue(client, T_PRECISE).json()

    # 之后同秒补报旧整秒记录并确认到另一精确时刻，不得改动已发单
    _import_legacy(client, [("S1", T_WHOLE, 1), ("S2", T_WHOLE, 2)])
    assert _confirm(client, "S1", T_WHOLE, "2026-11-22T08:00:00.000001Z").status_code == 200
    assert _confirm(client, "S2", T_WHOLE, "2026-11-22T08:00:00.000002Z").status_code == 200

    got = client.get(f"/approvals/{created['approval_no']}").json()
    assert got["utc_time"] == T_PRECISE
    assert got["upstream_reading_mm"] == 1000
    assert got["downstream_reading_mm"] == 1020
    assert got["fall_mm"] == -5


# ---------- 旧库迁移 ----------

def _make_v1_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE readings (
            station    TEXT NOT NULL,
            utc_time   TEXT NOT NULL,
            gauge_mm   INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (station, utc_time)
        );
        CREATE TABLE survey_lines (
            upstream        TEXT NOT NULL,
            downstream      TEXT NOT NULL,
            zero_offset_mm  INTEGER NOT NULL,
            PRIMARY KEY (upstream, downstream)
        );
        CREATE TABLE line_version (
            id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL
        );
        INSERT INTO line_version (id, version) VALUES (1, 1);
        CREATE TABLE approvals (
            approval_no INTEGER PRIMARY KEY AUTOINCREMENT,
            utc_time TEXT NOT NULL, upstream TEXT NOT NULL, downstream TEXT NOT NULL,
            upstream_reading_mm INTEGER NOT NULL, downstream_reading_mm INTEGER NOT NULL,
            zero_offset_mm INTEGER NOT NULL, fall_mm INTEGER NOT NULL,
            line_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('valid','revoked')),
            revoke_reason TEXT, created_at TEXT NOT NULL, revoked_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO readings VALUES ('S1', '2026-11-22T08:00:00Z', 1000, '2026-11-22T08:00:01.000000Z')"
    )
    conn.execute(
        "INSERT INTO survey_lines VALUES ('S1', 'S2', 15)"
    )
    conn.execute(
        "INSERT INTO approvals (utc_time, upstream, downstream, upstream_reading_mm, "
        "downstream_reading_mm, zero_offset_mm, fall_mm, line_version, status, created_at) "
        "VALUES ('2026-11-22T08:00:00Z', 'S1', 'S2', 1000, 1020, 15, -5, 1, 'valid', "
        "'2026-11-22T08:00:01.000000Z')"
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_to_pending_and_confirmable(client):
    # 用独立路径模拟旧库，然后让请求连接指向它
    path = os.path.join(os.path.dirname(db_mod.DEFAULT_DB_PATH), "legacy-v1.db")
    _make_v1_db(path)
    db_mod.DEFAULT_DB_PATH = path

    health = client.get("/health")
    assert health.status_code == 200

    rows = client.get("/readings").json()
    assert [(x["utc_time"], x["gauge_mm"], x["time_status"]) for x in rows] == [
        ("2026-11-22T08:00:00.000000Z", 1000, "pending")
    ]
    # 已发单仍可查，显示核发时的时刻与读数
    a = client.get("/approvals/1").json()
    assert a["utc_time"] == "2026-11-22T08:00:00.000000Z"
    assert a["fall_mm"] == -5 and a["status"] == "valid"

    # 旧读数不能直接核发；补录下游并确认两侧后可核发
    assert _issue(client, T_WHOLE, "S1", "S2").status_code == 422
    _import_legacy(client, [("S2", T_WHOLE, 1020)])
    assert _confirm(client, "S1", T_WHOLE, T_PRECISE).status_code == 200
    assert _confirm(client, "S2", T_WHOLE, T_PRECISE).status_code == 200
    r = _issue(client, T_PRECISE)
    assert r.status_code == 201, r.text
    assert r.json()["fall_mm"] == -5


# ---------- 并发 ----------

def test_concurrent_confirm_then_issue_never_mixes_times(client):
    """两个时刻各自成对；并发确认后任何核定单都必须用同一精确时刻的两站读数。"""
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    _import_legacy(
        client,
        [
            ("S1", "2026-11-22T08:00:00Z", 1000),
            ("S2", "2026-11-22T08:00:00Z", 1020),
            ("S1", "2026-11-22T08:00:01Z", 2000),
            ("S2", "2026-11-22T08:00:01Z", 2030),
        ],
    )
    exact = {
        "2026-11-22T08:00:00Z": "2026-11-22T08:00:00.111111Z",
        "2026-11-22T08:00:01Z": "2026-11-22T08:00:01.333333Z",
    }
    errors: list[Exception] = []

    def confirm_worker(second: str) -> None:
        try:
            # 同一秒内上下游各自按仪器原记录确认到同一个精确时刻
            assert _confirm(client, "S1", second, exact[second]).status_code == 200
            assert _confirm(client, "S2", second, exact[second]).status_code == 200
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def issue_worker(exact_time: str) -> None:
        try:
            for _ in range(15):
                r = _issue(client, exact_time)
                if r.status_code == 201:
                    assert r.json()["utc_time"] == exact_time
                else:
                    assert r.status_code == 422, r.text
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=confirm_worker, args=("2026-11-22T08:00:00Z",)),
        threading.Thread(target=confirm_worker, args=("2026-11-22T08:00:01Z",)),
        threading.Thread(target=issue_worker, args=("2026-11-22T08:00:00.111111Z",)),
        threading.Thread(target=issue_worker, args=("2026-11-22T08:00:00.111111Z",)),
        threading.Thread(target=issue_worker, args=("2026-11-22T08:00:01.333333Z",)),
        threading.Thread(target=issue_worker, args=("2026-11-22T08:00:01.333333Z",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # 所有成功核发的单，上下游读数必须来自该单时刻对应的那一对
    expected = {
        "2026-11-22T08:00:00.111111Z": (1000, 1020),
        "2026-11-22T08:00:01.333333Z": (2000, 2030),
    }
    for a in client.get("/approvals?status=valid").json():
        pair = (a["upstream_reading_mm"], a["downstream_reading_mm"])
        assert pair == expected[a["utc_time"]], a

    # 并发确认完成后，两个精确时刻都能各自核发，且不会串用另一时刻的读数
    for exact_time, pair in expected.items():
        r = _issue(client, exact_time)
        assert r.status_code == 201, r.text
        assert (r.json()["upstream_reading_mm"], r.json()["downstream_reading_mm"]) == pair
    assert {a["utc_time"] for a in client.get("/approvals?status=valid").json()} == set(expected)


# ---------- 补充边界 ----------

def test_confirm_accepts_timezone_spelling_and_rejects_naive(client):
    _import_legacy(client, [("S1", T_WHOLE, 1000)])
    # 精确时刻写成 +08:00，等价于 08:00:00.123456Z
    r = _confirm(client, "S1", T_WHOLE, "2026-11-22T16:00:00.123456+08:00")
    assert r.status_code == 200, r.text
    assert r.json()["reading"]["utc_time"] == T_PRECISE

    _import_legacy(client, [("S2", T_WHOLE, 1020)])
    naive = client.post(
        "/readings/confirm",
        json={
            "station": "S2",
            "legacy_utc_time": T_WHOLE,
            "utc_time": "2026-11-22T08:00:00.100000",
            "operator": "巡测员-张三",
        },
    )
    assert naive.status_code == 422
    # 待确认读数未被改动
    pending = client.get("/readings?time_status=pending").json()
    assert [(x["station"], x["utc_time"]) for x in pending] == [("S2", "2026-11-22T08:00:00.000000Z")]


def test_query_naive_time_returns_422(client):
    r = client.get("/readings?utc_time=2026-11-22T08:00:00")
    assert r.status_code == 422


def test_confirm_legacy_second_with_micros_rejected(client):
    _import_legacy(client, [("S1", T_WHOLE, 1000)])
    r = client.post(
        "/readings/confirm",
        json={
            "station": "S1",
            "legacy_utc_time": "2026-11-22T08:00:00.500000Z",
            "utc_time": "2026-11-22T08:00:00.600000Z",
            "operator": "巡测员-张三",
        },
    )
    assert r.status_code == 422


def test_pending_legacy_and_precise_reading_coexist_and_issue_uses_confirmed(client):
    _import_legacy(client, [("S1", T_WHOLE, 1), ("S2", T_WHOLE, 2)])
    assert _post_readings(client, [("S1", T_PRECISE, 1000), ("S2", T_PRECISE, 1020)]).status_code == 201
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, T_PRECISE)
    assert r.status_code == 201, r.text
    assert r.json()["upstream_reading_mm"] == 1000
    assert r.json()["downstream_reading_mm"] == 1020
    # 待确认旧记录仍在，未被核发使用
    assert len(client.get("/readings?time_status=pending").json()) == 2


def test_migration_is_idempotent_and_preserves_readings():
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="migrate-"), "v1.db")
    _make_v1_db(path)
    conn = db_mod.open_connection(path)
    try:
        db_mod.init_db(conn)
        first = [tuple(r) for r in conn.execute("SELECT station, utc_time, gauge_mm, time_status FROM readings")]
        db_mod.init_db(conn)
        second = [tuple(r) for r in conn.execute("SELECT station, utc_time, gauge_mm, time_status FROM readings")]
        assert first == second == [("S1", "2026-11-22T08:00:00.000000Z", 1000, "pending")]
    finally:
        conn.close()


def test_issue_never_combines_readings_from_different_microseconds(client):
    # 两站各自只在一个精确时刻有读数，且时刻不同
    assert _post_readings(
        client,
        [("S1", "2026-11-22T08:00:00.111111Z", 1000), ("S2", "2026-11-22T08:00:00.222222Z", 1020)],
    ).status_code == 201
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    for t in ("2026-11-22T08:00:00.111111Z", "2026-11-22T08:00:00.222222Z"):
        r = _issue(client, t)
        assert r.status_code == 422, r.text
        assert "缺读数" in r.json()["detail"]
    assert client.get("/approvals").json() == []


def test_concurrent_reading_entry_and_confirmation_are_serialized(client):
    """同站同刻，新精确读数与旧整秒确认并发：只能有一条已确认读数，绝不双写。"""
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    rounds = 8
    for i in range(rounds):
        second = f"2026-11-22T10:00:{i:02d}Z"
        exact = f"2026-11-22T10:00:{i:02d}.500000Z"
        legacy_value, precise_value = 1000 + i, 2000 + i
        assert _import_legacy(client, [("S1", second, legacy_value)]).status_code == 201

        barrier = threading.Barrier(2)
        codes: dict[str, int] = {}

        def do_entry():
            barrier.wait()
            codes["entry"] = _post_readings(client, [("S1", exact, precise_value)]).status_code

        def do_confirm():
            barrier.wait()
            codes["confirm"] = _confirm(client, "S1", second, exact).status_code

        ts = [threading.Thread(target=do_entry), threading.Thread(target=do_confirm)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert codes["entry"] in (201, 409), codes
        assert codes["confirm"] in (200, 409), codes
        # 成功方唯一；另一方必须被拒
        assert (codes["entry"] == 201) != (codes["confirm"] == 200), codes

        rows = client.get(f"/readings?station=S1&utc_time={exact}").json()
        assert len(rows) == 1, rows
        assert rows[0]["time_status"] == "confirmed"
        expected_value = precise_value if codes["entry"] == 201 else legacy_value
        assert rows[0]["gauge_mm"] == expected_value


def test_confirm_to_exact_whole_second_merges_or_conflicts_with_confirmed(client):
    # 精确录入落在 .000000Z，与旧整秒文本同一时刻；待确认记录可与其并存
    assert _post_readings(client, [("S1", T_WHOLE, 1000)]).status_code == 201
    assert _import_legacy(client, [("S1", T_WHOLE, 1000)]).status_code == 201
    rows = client.get("/readings?station=S1").json()
    assert sorted((x["utc_time"], x["time_status"]) for x in rows) == [
        ("2026-11-22T08:00:00.000000Z", "confirmed"),
        ("2026-11-22T08:00:00.000000Z", "pending"),
    ]
    # 同值确认到整秒本身：合并
    r = _confirm(client, "S1", T_WHOLE, T_WHOLE)
    assert r.status_code == 200, r.text
    assert r.json()["merged"] is True
    rows = client.get("/readings?station=S1").json()
    assert len(rows) == 1 and rows[0]["time_status"] == "confirmed"

    # 异值：待确认行保留
    assert _import_legacy(client, [("S1", T_WHOLE, 999)]).status_code == 201
    r = _confirm(client, "S1", T_WHOLE, T_WHOLE)
    assert r.status_code == 409
    rows = client.get("/readings?station=S1&time_status=pending").json()
    assert len(rows) == 1 and rows[0]["gauge_mm"] == 999
