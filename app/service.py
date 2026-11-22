"""核定业务服务：录入/确认/核发、换线复算与撤销。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import db
from .graph import Edge, analyze_components, zero_offset_between
from .schemas import ApprovalOut, ReadingOut, RevokedItem
from .timeutil import (
    canonical_second,
    canonical_utc,
    floor_canonical_to_second,
    parse_stored_utc,
)


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


def _reading_out(row: sqlite3.Row) -> ReadingOut:
    return ReadingOut(
        id=int(row["id"]),
        station=row["station"],
        utc_time=row["utc_time"],
        gauge_mm=int(row["gauge_mm"]),
        time_status=row["time_status"],
        confirmed_at=row["confirmed_at"],
        confirmed_by=row["confirmed_by"],
    )


def add_readings(conn: sqlite3.Connection, items: list[tuple[str, str, int]]) -> dict[str, int]:
    """录入精确时刻读数；同站同刻（含不同时区写法）重复则整单拒绝，不部分写入。"""
    # 批内先按“站号 + 规范 UTC 微秒文本”自查重复（+08:00 与 Z 写法会得到同一文本）
    seen: set[tuple[str, str]] = set()
    for station, utc_time, _ in items:
        key = (station, utc_time)
        if key in seen:
            raise ServiceError(
                f"同一批内重复录入：站号 {station} 在 {utc_time} 出现多次，整单拒绝",
                409,
            )
        seen.add(key)

    with db.write_transaction(conn):
        for station, utc_time, _ in items:
            if db.get_reading_row(conn, station, utc_time, time_status="confirmed") is not None:
                raise ServiceError(
                    f"读数已存在：站号 {station} 在 {utc_time} 已有水尺读数，拒绝重复录入",
                    409,
                )
        inserted = 0
        for station, utc_time, gauge_mm in items:
            if db.insert_reading(conn, station, utc_time, gauge_mm, time_status="confirmed") is None:
                # 唯一索引兜底（例如并发写入），整单回滚
                raise ServiceError(
                    f"读数已存在：站号 {station} 在 {utc_time} 已有水尺读数，拒绝重复录入",
                    409,
                )
            inserted += 1
    return {"inserted": inserted}


def import_legacy_readings(conn: sqlite3.Connection, items: list[tuple[str, str, int]]) -> dict[str, int]:
    """补报旧库整秒读数：一律入库为待确认，可与该秒内的精确读数并存。"""
    seen: set[tuple[str, str]] = set()
    for station, whole_second, _ in items:
        key = (station, whole_second)
        if key in seen:
            raise ServiceError(
                f"同一批内重复补报：站号 {station} 的整秒时刻 {whole_second} 出现多次，整单拒绝",
                409,
            )
        seen.add(key)

    with db.write_transaction(conn):
        for station, whole_second, _ in items:
            if db.get_pending_reading(conn, station, whole_second) is not None:
                raise ServiceError(
                    f"待确认读数已存在：站号 {station} 在旧整秒 {whole_second} 已有待确认读数，拒绝重复补报",
                    409,
                )
        inserted = 0
        for station, whole_second, gauge_mm in items:
            if db.insert_pending_reading(conn, station, whole_second, gauge_mm) is None:
                raise ServiceError(
                    f"待确认读数已存在：站号 {station} 在旧整秒 {whole_second} 已有待确认读数",
                    409,
                )
            inserted += 1
        pending_total = db.count_readings_by_status(conn, "pending")
    return {"inserted": inserted, "pending_total": pending_total}


def confirm_legacy_reading(
    conn: sqlite3.Connection,
    *,
    station: str,
    legacy_utc_time: str,
    utc_time: str,
    operator: str,
) -> dict[str, object]:
    """凭仪器原记录，把某站某旧整秒的待确认读数确认到该秒内的精确时刻。

    - 精确时刻必须落在旧整秒所在的一秒内；
    - 该精确时刻若已有同站读数：同值合并（删除待确认行），异值拒绝；
    - 任何失败都不改动原数据，待确认状态保留。
    """
    legacy_second = parse_stored_utc(legacy_utc_time)
    exact = parse_stored_utc(utc_time)
    second_start = legacy_second.replace(microsecond=0)
    if not (second_start <= exact < second_start + timedelta(seconds=1)):
        raise ServiceError(
            f"精确时刻 {utc_time} 不在旧整秒 {legacy_utc_time} 所在的一秒内，拒绝确认",
            422,
        )

    canonical_exact = canonical_utc(exact)
    canonical_legacy = canonical_second(second_start)

    with db.write_transaction(conn):
        pending = db.get_pending_reading(conn, station, canonical_legacy)
        if pending is None:
            raise ServiceError(
                f"待确认读数不存在：站号 {station} 在旧整秒 {canonical_legacy} 没有待确认读数",
                404,
            )
        existing = db.get_reading_row(conn, station, canonical_exact, time_status="confirmed")
        if existing is not None:
            if int(existing["gauge_mm"]) != int(pending["gauge_mm"]):
                raise ServiceError(
                    f"确认冲突：站号 {station} 在 {canonical_exact} 已有读数 "
                    f"{int(existing['gauge_mm'])} 毫米，与待确认读数 {int(pending['gauge_mm'])} 毫米不同；"
                    "已保留原数据和待确认状态",
                    409,
                )
            # 同值合并：移除待确认行，保留既有精确读数
            db.delete_reading(conn, int(pending["id"]))
            return {"merged": True, "reading": _reading_out(existing)}

        db.confirm_reading(
            conn,
            reading_id=int(pending["id"]),
            utc_time=canonical_exact,
            operator=operator,
        )
        updated = db.get_reading_row(conn, station, canonical_exact, time_status="confirmed")
        assert updated is not None
        return {"merged": False, "reading": _reading_out(updated)}


def issue_approval(conn: sqlite3.Connection, utc_time: str, upstream: str, downstream: str) -> ApprovalOut:
    """按精确时刻与上下游站号核发水面落差；任何条件不满足都拒发并说明原因。"""
    with db.write_transaction(conn):
        # 同一写事务（BEGIN IMMEDIATE）内完成：并发录入/确认不会让核发混用不同时刻的数据。
        components = analyze_components(db.get_edges(conn))
        offset = zero_offset_between(components, upstream, downstream)
        if not offset.ok:
            raise ServiceError(offset.reason or "两尺零点高差无法确定")
        assert offset.zero_offset_mm is not None

        up_reading = db.get_reading(conn, upstream, utc_time)
        if up_reading is None:
            raise ServiceError(_missing_reading_message(conn, upstream, utc_time))
        down_reading = db.get_reading(conn, downstream, utc_time)
        if down_reading is None:
            raise ServiceError(_missing_reading_message(conn, downstream, utc_time))

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


def _missing_reading_message(conn: sqlite3.Connection, station: str, utc_time: str) -> str:
    whole_second = floor_canonical_to_second(utc_time)
    pending = db.get_pending_reading(conn, station, whole_second)
    if pending is not None:
        return (
            f"缺读数：站 {station} 在精确时刻 {utc_time} 没有已确认读数；"
            f"该秒内仅有待确认旧整秒读数 {whole_second}（{int(pending['gauge_mm'])} 毫米），请先确认"
        )
    return f"缺读数：站 {station} 在精确时刻 {utc_time} 没有已确认水尺读数"


def replace_lines_and_recompute(conn: sqlite3.Connection, edges: list[Edge]):
    """整批换线并在同一事务内复算全部有效核定单。

    - 算不出（未连通/闭环矛盾）→ 撤销；
    - 零点高差或落差数值改变 → 撤销；
    - 其余保持有效。
    复算只用核定单里的读数快照，不受后续读数录入/确认影响。
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
