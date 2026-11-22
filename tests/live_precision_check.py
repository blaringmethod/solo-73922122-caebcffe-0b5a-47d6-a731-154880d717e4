"""真进程验证：旧整秒补报/确认与并发核发。

证明核发只采用同一精确时刻的两站已确认读数，确认失败不改动原数据。

用法：
  1. 启动服务：APP_DB_PATH=... uvicorn app.main:app --port 8766
  2. BASE_URL=http://127.0.0.1:8766 python3 -I tests/live_precision_check.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8766")

# 两个精确时刻，各自成对
SECOND_A = "2026-11-22T08:00:00Z"
EXACT_A = "2026-11-22T08:00:00.111111Z"
SECOND_B = "2026-11-22T08:00:01Z"
EXACT_B = "2026-11-22T08:00:01.222222Z"

# 每站读数：A 秒内 S1=1000/S2=1020，B 秒内 S1=2000/S2=2030
VALUES = {
    (SECOND_A, "S1"): 1000,
    (SECOND_A, "S2"): 1020,
    (SECOND_B, "S1"): 2000,
    (SECOND_B, "S2"): 2030,
}


def _request(url: str, body: dict | None, method: str) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def _wait_ready() -> None:
    import time

    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=2)
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("server not reachable")


def main() -> int:
    _wait_ready()

    # 测线 S1-S2 零点高差 15
    code, data = _request(
        f"{BASE}/lines",
        {"lines": [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}]},
        "PUT",
    )
    assert code == 200, (code, data)

    # 补报四个旧整秒读数（待确认）
    legacy = [
        {"station": s, "utc_time": sec, "gauge_mm": mm}
        for (sec, s), mm in VALUES.items()
    ]
    code, data = _request(f"{BASE}/readings/legacy-import", {"readings": legacy}, "POST")
    assert code == 201, (code, data)
    assert data["pending_total"] == 4, data

    # 待确认状态不能核发
    code, data = _request(
        f"{BASE}/approvals/issue",
        {"utc_time": SECOND_A, "upstream": "S1", "downstream": "S2"},
        "POST",
    )
    assert code == 422 and "待确认" in data["detail"], (code, data)

    # 异值确认被拒：先在 EXACT_A 写入 S1=9999，再尝试确认旧值 1000
    code, data = _request(
        f"{BASE}/readings",
        {"readings": [{"station": "S1", "utc_time": EXACT_A, "gauge_mm": 9999}]},
        "POST",
    )
    assert code == 201, (code, data)
    code, data = _request(
        f"{BASE}/readings/confirm",
        {
            "station": "S1",
            "legacy_utc_time": SECOND_A,
            "utc_time": EXACT_A,
            "operator": "巡测员-张三",
        },
        "POST",
    )
    assert code == 409, (code, data)
    rows = _get(f"{BASE}/readings?station=S1&time_status=pending")
    assert len(rows) == 2, rows  # 原待确认记录保留

    # 清理冲突行（测试专用：直接改用另一个精确时刻确认）
    # 换个不会冲突的精确时刻，验证确认成功并保留原读数快照语义
    EXACT_A2 = "2026-11-22T08:00:00.121212Z"
    code, data = _request(
        f"{BASE}/readings/confirm",
        {
            "station": "S1",
            "legacy_utc_time": SECOND_A,
            "utc_time": EXACT_A2,
            "operator": "巡测员-张三",
        },
        "POST",
    )
    assert code == 200 and data["merged"] is False, (code, data)

    # 并发确认剩余读数 + 并发核发，验证不会混用两个时刻
    barrier = threading.Barrier(5)
    errors: list[str] = []

    def confirm(station: str, sec: str, exact: str) -> None:
        barrier.wait()
        c, d = _request(
            f"{BASE}/readings/confirm",
            {
                "station": station,
                "legacy_utc_time": sec,
                "utc_time": exact,
                "operator": "巡测员-李四",
            },
            "POST",
        )
        if c != 200:
            errors.append(f"confirm {station} {exact}: {c} {d}")

    def issue(exact: str) -> None:
        barrier.wait()
        import time

        for _ in range(10):
            c, d = _request(
                f"{BASE}/approvals/issue",
                {"utc_time": exact, "upstream": "S1", "downstream": "S2"},
                "POST",
            )
            if c not in (201, 422):
                errors.append(f"issue {exact}: {c} {d}")
            time.sleep(0.01)

    threads = [
        threading.Thread(target=confirm, args=("S2", SECOND_A, EXACT_A2)),
        threading.Thread(target=confirm, args=("S1", SECOND_B, EXACT_B)),
        threading.Thread(target=confirm, args=("S2", SECOND_B, EXACT_B)),
        threading.Thread(target=issue, args=(EXACT_A2,)),
        threading.Thread(target=issue, args=(EXACT_B,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 两个时刻都能核发；每个单的上下游读数必须来自该时刻自己的那一对
    expected = {
        EXACT_A2: (1000, 1020),
        EXACT_B: (2000, 2030),
    }
    for exact in expected:
        c, d = _request(
            f"{BASE}/approvals/issue",
            {"utc_time": exact, "upstream": "S1", "downstream": "S2"},
            "POST",
        )
        assert c == 201, (c, d)
        assert (d["upstream_reading_mm"], d["downstream_reading_mm"]) == expected[exact], d

    approvals = _get(f"{BASE}/approvals")
    for a in approvals:
        assert (a["upstream_reading_mm"], a["downstream_reading_mm"]) == expected[a["utc_time"]], a
        assert a["utc_time"] in expected, a

    # 快照：再录入同秒其他精确读数，不影响已发单
    code, data = _request(
        f"{BASE}/readings",
        {"readings": [{"station": "S1", "utc_time": "2026-11-22T08:00:00.999999Z", "gauge_mm": 1}]},
        "POST",
    )
    assert code == 201, (code, data)
    snapshot = _get(f"{BASE}/approvals/{approvals[0]['approval_no']}")
    assert (snapshot["upstream_reading_mm"], snapshot["downstream_reading_mm"]) == expected[
        snapshot["utc_time"]
    ], snapshot

    print(
        f"LIVE PRECISION OK: approvals={len(approvals)} "
        f"times={sorted({a['utc_time'] for a in approvals})} (同刻成对、无混用)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
