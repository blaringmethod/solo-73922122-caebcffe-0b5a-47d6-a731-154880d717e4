"""FastAPI 入口：水面落差核定 API。

启动后访问 /docs 可直接演示“录入读数 → 核发 → 换线 → 查单/撤销”。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, service
from .graph import Edge
from .schemas import (
    ApprovalOut,
    ErrorOut,
    IssueIn,
    LineOut,
    LineReplaceIn,
    ReadingBatchIn,
    ReadingOut,
    ReplaceResult,
    RevokedItem,
    StatusOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建库建表，保证容器首请求可用；请求连接在依赖中按需创建
    boot = db.open_connection()
    db.init_db(boot)
    boot.close()
    yield


app = FastAPI(
    title="河道水面落差核定 API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "水准测线换线后，复核已发水面落差核定单。\n\n"
        "- 录入：站号、UTC 时刻、整数毫米水尺读数\n"
        "- 测线：记录两尺零点的有向毫米高差（上游零点 − 下游零点），整批替换\n"
        "- 核发：按时刻与上下游站号计算落差，缺读数/未连通/闭环矛盾则拒发\n"
        "- 换线：同一事务内复算全部有效单，撤销算不出或数值改变的单\n"
    ),
)


def utc_canon(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn(request: Request):
    # 每请求一个连接：线程隔离，写事务经 BEGIN IMMEDIATE + busy_timeout 串行
    conn = db.open_connection()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(service.ServiceError)
async def _service_error_handler(request: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 读数 ----------

@app.post(
    "/readings",
    status_code=201,
    tags=["读数"],
    summary="录入水尺读数（可批量）",
    responses={409: {"model": ErrorOut, "description": "同站同时刻读数已存在，整单拒绝"}},
)
def add_readings(payload: ReadingBatchIn, conn=Depends(get_conn)) -> dict[str, int]:
    items = [(r.station, utc_canon(r.utc_time), r.gauge_mm) for r in payload.readings]
    return service.add_readings(conn, items)


@app.get("/readings", tags=["读数"], summary="查询读数（可按站号/时刻过滤）")
def list_readings(
    station: str | None = None,
    utc_time: datetime | None = None,
    conn=Depends(get_conn),
) -> list[ReadingOut]:
    sql = "SELECT station, utc_time, gauge_mm FROM readings"
    conds, params = [], []
    if station is not None:
        conds.append("station = ?")
        params.append(station)
    if utc_time is not None:
        conds.append("utc_time = ?")
        params.append(utc_canon(utc_time))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY utc_time, station"
    rows = conn.execute(sql, params).fetchall()
    return [ReadingOut(station=r["station"], utc_time=r["utc_time"], gauge_mm=r["gauge_mm"]) for r in rows]


# ---------- 测线 ----------

@app.put(
    "/lines",
    response_model=ReplaceResult,
    tags=["测线"],
    summary="整批替换测线并复算核定单",
    responses={400: {"model": ErrorOut, "description": "新测线集合含自环或重复边"}},
)
def replace_lines(payload: LineReplaceIn, conn=Depends(get_conn)) -> ReplaceResult:
    edges = [Edge(upstream=e.upstream, downstream=e.downstream, zero_offset_mm=e.zero_offset_mm) for e in payload.lines]
    return service.replace_lines_and_recompute(conn, edges)


@app.get("/lines", tags=["测线"], summary="查询当前测线全集")
def list_lines(conn=Depends(get_conn)) -> list[LineOut]:
    edges = db.get_edges(conn)
    return [
        LineOut(upstream=e.upstream, downstream=e.downstream, zero_offset_mm=e.zero_offset_mm)
        for e in edges
    ]


# ---------- 核定单 ----------

@app.post(
    "/approvals/issue",
    response_model=ApprovalOut,
    status_code=201,
    tags=["核定单"],
    summary="核发水面落差核定单",
    responses={422: {"model": ErrorOut, "description": "缺读数 / 两尺未连通 / 闭环矛盾，拒发"}},
)
def issue(payload: IssueIn, conn=Depends(get_conn)) -> ApprovalOut:
    return service.issue_approval(conn, utc_canon(payload.utc_time), payload.upstream, payload.downstream)


@app.get(
    "/approvals",
    response_model=list[ApprovalOut],
    tags=["核定单"],
    summary="查询核定单列表（可按状态过滤）",
)
def list_approvals(status: Literal["valid", "revoked"] | None = None, conn=Depends(get_conn)) -> list[ApprovalOut]:
    return [service._to_out(r) for r in db.list_approvals(conn, status)]


@app.get(
    "/approvals/{approval_no}",
    response_model=ApprovalOut,
    tags=["核定单"],
    summary="按单号查原数与状态（撤销后须重新核发）",
    responses={404: {"model": ErrorOut}},
)
def get_approval(approval_no: int, conn=Depends(get_conn)) -> ApprovalOut:
    row = db.get_approval(conn, approval_no)
    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"核定单 {approval_no} 不存在"})
    return service._to_out(row)


# ---------- 总览 ----------

@app.get("/status", response_model=StatusOut, tags=["系统"], summary="数据总览")
def status(conn=Depends(get_conn)) -> StatusOut:
    return StatusOut(
        line_version=db.get_line_version(conn),
        line_count=len(db.get_edges(conn)),
        reading_count=db.count_readings(conn),
        valid_approval_count=db.count_approvals(conn, "valid"),
        revoked_approval_count=db.count_approvals(conn, "revoked"),
    )
