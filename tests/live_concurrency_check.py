"""真并发验证：对已运行的 uvicorn 服务多线程打核发 + 换线，
验证换线后不会有“本应撤销却仍 valid”的核定单。

用法：
  1. 启动服务：APP_DB_PATH=... uvicorn app.main:app --port 8765
  2. BASE_URL=http://127.0.0.1:8765 python3 -I tests/live_concurrency_check.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request
import urllib.error

T = "2026-11-22T08:00:00Z"
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8765")
ISSUERS = 4
PER_ISSUER = 10


def _request(url: str, body: dict, method: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=2)
            break
        except OSError:
            import time

            time.sleep(0.5)
    else:
        raise RuntimeError("server not reachable")

    code, _ = _request(
        f"{BASE}/readings",
        {"readings": [
            {"station": "S1", "utc_time": T, "gauge_mm": 1000},
            {"station": "S2", "utc_time": T, "gauge_mm": 1020},
        ]},
        "POST",
    )
    assert code == 201, code
    code, _ = _request(
        f"{BASE}/lines",
        {"lines": [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}]},
        "PUT",
    )
    assert code == 200, code

    errors: list[str] = []
    barrier = threading.Barrier(ISSUERS + 1)

    def issue_worker(n: int) -> None:
        barrier.wait()
        for _ in range(n):
            c, data = _request(
                f"{BASE}/approvals/issue",
                {"utc_time": T, "upstream": "S1", "downstream": "S2"},
                "POST",
            )
            if c != 201:
                errors.append(f"issue failed: {c} {data}")

    def replace_worker() -> None:
        barrier.wait()
        import time

        time.sleep(0.05)
        c, data = _request(
            f"{BASE}/lines",
            {"lines": [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 25}]},
            "PUT",
        )
        if c != 200:
            errors.append(f"replace failed: {c} {data}")

    threads = [threading.Thread(target=issue_worker, args=(PER_ISSUER,)) for _ in range(ISSUERS)]
    threads.append(threading.Thread(target=replace_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    approvals = _get(f"{BASE}/approvals")
    expected = ISSUERS * PER_ISSUER
    assert len(approvals) == expected, (len(approvals), expected)
    for a in approvals:
        if a["line_version"] == 1:
            assert a["status"] == "revoked", f"stale valid approval: {a}"
            assert a["zero_offset_mm"] == 15
            assert "数值改变" in a["revoke_reason"]
        else:
            assert a["status"] == "valid"
            assert a["zero_offset_mm"] == 25 and a["fall_mm"] == 5
    valid = _get(f"{BASE}/approvals?status=valid")
    revoked = _get(f"{BASE}/approvals?status=revoked")
    assert len(valid) + len(revoked) == expected
    print(f"LIVE OK: total={expected} valid={len(valid)} revoked={len(revoked)} (无漏撤)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
