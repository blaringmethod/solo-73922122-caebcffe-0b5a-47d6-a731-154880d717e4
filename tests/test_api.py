"""端到端关键场景自测（FastAPI TestClient + 真实 SQLite 文件）。"""

from __future__ import annotations

import threading

T = "2026-11-22T08:00:00Z"
T2 = "2026-11-22T09:00:00Z"


def _readings(client, mapping, t=T):
    body = {"readings": [{"station": s, "utc_time": t, "gauge_mm": mm} for s, mm in mapping.items()]}
    r = client.post("/readings", json=body)
    assert r.status_code == 201, r.text
    return r


def _lines(client, lines):
    r = client.put("/lines", json={"lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


def _issue(client, up, down, t=T):
    return client.post("/approvals/issue", json={"utc_time": t, "upstream": up, "downstream": down})


def test_issue_happy_path_fall_formula(client):
    _readings(client, {"S1": 1000, "S2": 1020})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, "S1", "S2")
    assert r.status_code == 201, r.text
    data = r.json()
    # 落差 = 上游(读数+零点) - 下游(读数+零点) = (1000+15) - 1020 = -5
    assert data["fall_mm"] == -5
    assert data["zero_offset_mm"] == 15
    assert data["status"] == "valid"
    assert data["line_version"] == 1


def test_issue_missing_reading_rejected(client):
    _readings(client, {"S1": 1000})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, "S1", "S2")
    assert r.status_code == 422
    assert "缺读数" in r.json()["detail"] and "S2" in r.json()["detail"]


def test_issue_not_connected_rejected(client):
    _readings(client, {"S1": 1000, "S3": 900})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, "S1", "S3")
    assert r.status_code == 422
    assert "未连通" in r.json()["detail"]


def test_issue_connectivity_checked_before_readings(client):
    # S9 既无读数也不连通：应先报未连通，而不是缺读数
    _readings(client, {"S1": 1000})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    r = _issue(client, "S1", "S9")
    assert r.status_code == 422
    assert "未连通" in r.json()["detail"]


def test_issue_contradictory_cycle_rejected(client):
    _readings(client, {"S1": 1000, "S2": 1000, "S3": 1000})
    _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 10},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
            {"upstream": "S1", "downstream": "S3", "zero_offset_mm": 14},  # 应为 15
        ],
    )
    r = _issue(client, "S1", "S2")
    assert r.status_code == 422
    assert "闭环矛盾" in r.json()["detail"]


def test_duplicate_reading_whole_batch_rejected(client):
    _readings(client, {"S1": 1000})
    body = {
        "readings": [
            {"station": "S1", "utc_time": T, "gauge_mm": 1000},
            {"station": "S2", "utc_time": T, "gauge_mm": 900},
        ]
    }
    r = client.post("/readings", json=body)
    assert r.status_code == 409
    # 整单拒绝：S2 也不应被写入
    assert client.get("/readings").json() == [
        {"station": "S1", "utc_time": T, "gauge_mm": 1000}
    ]


def test_non_integer_reading_rejected(client):
    r = client.post(
        "/readings",
        json={"readings": [{"station": "S1", "utc_time": T, "gauge_mm": 12.5}]},
    )
    assert r.status_code == 422


def test_naive_datetime_rejected(client):
    r = client.post(
        "/readings",
        json={"readings": [{"station": "S1", "utc_time": "2026-11-22 08:00:00", "gauge_mm": 1000}]},
    )
    assert r.status_code == 422


def test_line_replace_revokes_changed_and_invalid(client):
    _readings(client, {"S1": 1000, "S2": 1020, "S3": 900})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    a1 = _issue(client, "S1", "S2").json()
    assert a1["fall_mm"] == -5

    # 换线：S1-S2 高差改变；新增 S2-S3
    result = _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 25},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
        ],
    )
    assert result["line_version_before"] == 1
    assert result["line_version_after"] == 2
    revoked_nos = [x["approval_no"] for x in result["revoked"]]
    assert revoked_nos == [a1["approval_no"]]
    assert "数值改变" in result["revoked"][0]["reason"]
    assert result["still_valid"] == []

    # 原单状态可查，带撤销原因；撤销后必须重新核发
    got = client.get(f"/approvals/{a1['approval_no']}").json()
    assert got["status"] == "revoked"
    assert got["revoke_reason"] and got["revoked_at"]
    assert got["fall_mm"] == -5  # 原数保留

    # 重新核发得到新单号、新落差：(1000+25)-1020 = 5
    a2 = _issue(client, "S1", "S2")
    assert a2.status_code == 201
    assert a2.json()["approval_no"] != a1["approval_no"]
    assert a2.json()["fall_mm"] == 5
    assert a2.json()["line_version"] == 2

    # 旧单仍为撤销态
    assert client.get(f"/approvals/{a1['approval_no']}").json()["status"] == "revoked"


def test_line_replace_keeps_unchanged_approvals(client):
    _readings(client, {"S1": 1000, "S2": 1020, "S3": 900})
    _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
        ],
    )
    a1 = _issue(client, "S1", "S2").json()
    a2 = _issue(client, "S2", "S3").json()

    # 换线只新增一条不影响已有连通关系的独立测线
    _readings(client, {"S9": 100}, t=T2)
    result = _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
        ],
    )
    assert {x["approval_no"] for x in result["still_valid"]} == {a1["approval_no"], a2["approval_no"]}
    assert result["revoked"] == []

    status = client.get("/status").json()
    assert status["valid_approval_count"] == 2
    assert status["revoked_approval_count"] == 0


def test_line_replace_disconnect_revokes(client):
    _readings(client, {"S1": 1000, "S2": 1020})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    a1 = _issue(client, "S1", "S2").json()

    # 换线后 S1、S2 分别属于不同连通组
    result = _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S9", "zero_offset_mm": 1},
            {"upstream": "S2", "downstream": "S8", "zero_offset_mm": 1},
        ],
    )
    assert result["revoked"][0]["approval_no"] == a1["approval_no"]
    assert "未连通" in result["revoked"][0]["reason"]

    # 此时核发被拒
    r = _issue(client, "S1", "S2")
    assert r.status_code == 422 and "未连通" in r.json()["detail"]


def test_line_replace_contradiction_revokes_then_fix(client):
    _readings(client, {"S1": 1000, "S2": 1020, "S3": 900})
    _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
        ],
    )
    a1 = _issue(client, "S1", "S2").json()

    # 换入矛盾环，组内核定单撤销
    bad = _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 10},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
            {"upstream": "S1", "downstream": "S3", "zero_offset_mm": 14},
        ],
    )
    assert "闭环矛盾" in bad["revoked"][0]["reason"]

    # 修正闭环（14 -> 15），但 S1-S2 高差已由 15 变 10，旧单仍需重新核发
    fixed = _lines(
        client,
        [
            {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 10},
            {"upstream": "S2", "downstream": "S3", "zero_offset_mm": 5},
            {"upstream": "S1", "downstream": "S3", "zero_offset_mm": 15},
        ],
    )
    # 旧单已是 revoked，复算只看 valid，因此没有新撤销
    assert fixed["revoked"] == [] and fixed["still_valid"] == []

    r = _issue(client, "S1", "S2")
    assert r.status_code == 201
    # 新高差 10：(1000+10)-1020 = -10
    assert r.json()["fall_mm"] == -10


def test_invalid_line_set_rollback_keeps_old_data(client):
    _readings(client, {"S1": 1000, "S2": 1020})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    a1 = _issue(client, "S1", "S2").json()

    before = client.get("/lines").json()
    # 重复边（同站对，方向相反写两遍）必须 400
    r = client.put(
        "/lines",
        json={
            "lines": [
                {"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15},
                {"upstream": "S2", "downstream": "S1", "zero_offset_mm": -15},
            ]
        },
    )
    assert r.status_code == 400
    # 原数据保留
    assert client.get("/lines").json() == before
    got = client.get(f"/approvals/{a1['approval_no']}").json()
    assert got["status"] == "valid"
    assert client.get("/status").json()["line_version"] == 1


def test_empty_line_set_revokes_all(client):
    _readings(client, {"S1": 1000, "S2": 1020})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])
    _issue(client, "S1", "S2")
    result = _lines(client, [])
    assert len(result["revoked"]) == 1
    assert "未连通" in result["revoked"][0]["reason"]
    assert client.get("/lines").json() == []


def test_get_unknown_approval_404(client):
    r = client.get("/approvals/999")
    assert r.status_code == 404


def test_concurrent_issue_vs_replace_no_missed_revoke(client):
    """并发核发与换线：换线后不存在“本该撤销却仍 valid”的旧逻辑单。"""
    _readings(client, {"S1": 1000, "S2": 1020})
    _lines(client, [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 15}])

    errors: list[Exception] = []

    def issue_many(n: int):
        try:
            for _ in range(n):
                r = _issue(client, "S1", "S2")
                assert r.status_code == 201, r.text
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def replace_once():
        try:
            r = client.put(
                "/lines",
                json={"lines": [{"upstream": "S1", "downstream": "S2", "zero_offset_mm": 25}]},
            )
            assert r.status_code == 200, r.text
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_issue = threading.Thread(target=issue_many, args=(20,))
    t_replace = threading.Thread(target=replace_once)
    t_issue.start()
    t_replace.start()
    t_issue.join()
    t_replace.join()
    assert errors == []

    approvals = client.get("/approvals").json()
    assert approvals, "至少应有核定单"
    for a in approvals:
        if a["line_version"] == 1:
            # 换线前核发的单：高差 15 -> 25，必须已撤销
            assert a["status"] == "revoked", a
            assert "数值改变" in (a["revoke_reason"] or "")
        else:
            assert a["status"] == "valid"
            assert a["zero_offset_mm"] == 25
    # 撤销总数 + 有效总数 == 核发总数
    status = client.get("/status").json()
    assert status["valid_approval_count"] + status["revoked_approval_count"] == len(approvals)
