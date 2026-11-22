"""核定业务服务：核发、换线复算与撤销。"""

from __future__ import annotations

import sqlite3

from . import db
from .graph import Edge, analyze_components, zero_offset_between
from .schemas import ApprovalOut, RevokedItem


class ServiceError(Exception):
    """业务拒绝（HTTP 4xx），message 为可展示的中文原因。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _fall(upstream_reading_mm: int, downstream_reading_mm: int, zero_offset_mm: int) -> int:
    """水面落差（毫米）= 上游(读数+零点高) - 下游(读数+零点高)。"""
    return upstream_reading_mm + zero_offset_mm - downstream_reading_mm


def _validate_edge_set(edges: list[Edge]) -> None:
    seen: set[tuple[str, str]] = set()
    for e in edges:
        if e.upstream == e.downstream:
            raise ServiceError(f"测线自环非法：{e.upstream} 不能与自身构成测线", 400)
        key = tuple(sorted((e.upstream, e.downstream)))
        if key in seen:
            raise ServiceError(
                f"同一对站号出现重复测线：{e.upstream} <-> {e.downstream}，请合并为一条",
                400,
            )
        seen.add(key)


def add_readings(conn: sqlite3.Connection, items: list[tuple[str, str, int]]) -> dict[str, int]:
    """录入水尺读数；重复（同站同时刻）整单拒绝，不做部分写入。"""
    with db.write_transaction(conn):
        for station, utc_time, _ in items:
            if db.get_reading(conn, station, utc_time) is not None:
                raise ServiceError(
                    f"读数已存在：站号 {station} 在 {utc_time} 已有水尺读数，拒绝重复录入",
                    409,
                )
        inserted = 0
        for station, utc_time, gauge_mm in items:
            if db.insert_reading(conn, station, utc_time, gauge_mm):
                inserted += 1
    return {"inserted": inserted}


def issue_approval(conn: sqlite3.Connection, utc_time: str, upstream: str, downstream: str) -> ApprovalOut:
    """按时刻与上下游站号核发水面落差；任何条件不满足都拒发并说明原因。"""
    with db.write_transaction(conn):
        # 先判定测线可用性（未连通 / 闭环矛盾），再核对读数是否齐备
        components = analyze_components(db.get_edges(conn))
        offset = zero_offset_between(components, upstream, downstream)
        if not offset.ok:
            raise ServiceError(offset.reason or "两尺零点高差无法确定")
        assert offset.zero_offset_mm is not None

        up_reading = db.get_reading(conn, upstream, utc_time)
        if up_reading is None:
            raise ServiceError(f"缺读数：上游站 {upstream} 在 {utc_time} 没有水尺读数")
        down_reading = db.get_reading(conn, downstream, utc_time)
        if down_reading is None:
            raise ServiceError(f"缺读数：下游站 {downstream} 在 {utc_time} 没有水尺读数")

        version = db.get_line_version(conn)
        fall_mm = _fall(up_reading, down_reading, offset.zero_offset_mm)
        no = db.insert_approval(
            conn,
            utc_time=utc_time,
            upstream=upstream,
            downstream=downstream,
            upstream_reading_mm=up_reading,
            downstream_reading_mm=down_reading,
            zero_offset_mm=offset.zero_offset_mm,
            fall_mm=fall_mm,
            line_version=version,
        )
        row = db.get_approval(conn, no)
    return _to_out(row)


def replace_lines_and_recompute(conn: sqlite3.Connection, edges: list[Edge]):
    """整批换线并在同一事务内复算全部有效核定单。

    - 算不出（缺读数由快照兜底不会发生；未连通/闭环矛盾）→ 撤销；
    - 零点高差或落差数值改变 → 撤销；
    - 其余保持有效。
    任何一步失败整体回滚，保留原数据。
    """
    _validate_edge_set(edges)
    with db.write_transaction(conn):
        version_before = db.get_line_version(conn)
        version_after = db.replace_lines(conn, edges)
        components = analyze_components(edges)

        valid_rows = db.list_approvals(conn, status="valid")
        revoked: list[RevokedItem] = []
        still_valid: list[ApprovalOut] = []

        for row in valid_rows:
            offset = zero_offset_between(components, row["upstream"], row["downstream"])
            if not offset.ok:
                reason = f"换线后{offset.reason}"
                db.revoke_approval(conn, row["approval_no"], reason)
                revoked.append(RevokedItem(approval_no=row["approval_no"], reason=reason))
                continue
            assert offset.zero_offset_mm is not None
            new_fall = _fall(
                row["upstream_reading_mm"],
                row["downstream_reading_mm"],
                offset.zero_offset_mm,
            )
            if offset.zero_offset_mm != row["zero_offset_mm"] or new_fall != row["fall_mm"]:
                reason = (
                    f"换线后数值改变：零点高差 {row['zero_offset_mm']} -> {offset.zero_offset_mm} 毫米，"
                    f"落差 {row['fall_mm']} -> {new_fall} 毫米，需重新核发"
                )
                db.revoke_approval(conn, row["approval_no"], reason)
                revoked.append(RevokedItem(approval_no=row["approval_no"], reason=reason))
                continue
            still_valid.append(_to_out(row))

    return {
        "line_version_before": version_before,
        "line_version_after": version_after,
        "line_count": len(edges),
        "still_valid": still_valid,
        "revoked": revoked,
    }


def _to_out(row: sqlite3.Row) -> ApprovalOut:
    return ApprovalOut(
        approval_no=row["approval_no"],
        utc_time=row["utc_time"],
        upstream=row["upstream"],
        downstream=row["downstream"],
        upstream_reading_mm=row["upstream_reading_mm"],
        downstream_reading_mm=row["downstream_reading_mm"],
        zero_offset_mm=row["zero_offset_mm"],
        fall_mm=row["fall_mm"],
        line_version=row["line_version"],
        status=row["status"],
        revoke_reason=row["revoke_reason"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )
