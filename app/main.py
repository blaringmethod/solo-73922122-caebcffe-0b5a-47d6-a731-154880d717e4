"""FastAPI 入口：水面落差核定 API。

启动后访问 /docs 可直接演示“录入/补报 → 确认 → 核发 → 换线 → 查单/撤销”。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db, service
from .graph import Edge
from .schemas import (
    ApprovalOut,
    ConfirmIn,
    ConfirmResult,
    ErrorOut,
    IssueIn,
    LegacyBatchIn,
    LegacyImportResult,
    LineOut,
    LineReplaceIn,
    ReadingBatchIn,
    ReadingOut,
    ReplaceResult,
    StatusOut,
)
from .timeutil import canonical_second, canonical_utc, ensure_aware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建库建表并按需迁移旧库，保证容器首请求可用；请求连接在依赖中按需创建
    boot = db.open_connection()
    try:
        db.init_db(boot)
    finally:
        boot.close()
    yield


app = FastAPI(
    title="河道水面落差核定 API",
    version="2.0.0",
    lifespan=lifespan,
    description=(
        "按精确到微秒的 UTC 时刻录入读数、核发水面落差核定单。\n\n"
        "- 录入：站号、精确 UTC 时刻、整数毫米读数；同站同刻重复整批拒绝\n"
        "- 补报：旧库整秒读数入库为待确认，可查询、可与该秒内的精确读数并存\n"
        "- 确认：巡测员凭仪器原记录把待确认整秒读数确认到该秒内的精确时刻\n"
        "- 核发：只采用同一精确时刻的上下游已确认读数，缺读数/未连通/闭环矛盾则拒发\n"
        "- 换线：同一事务内复算全部有效单，撤销算不出或数值改变的单\n"
    ),
)


def get_conn(request: Request):
    # 每请求一个连接：线程隔离，写事务经 BEGIN IMMEDIATE + busy_timeout 串行
    conn = db.open_connection()
    try:
        db.init_db(conn)
        yield conn
    finally:
        conn.close()


def _exact_query_time(value: datetime) -> str:
    try:
        return canonical_utc(ensure_aware(value))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    summary="录入精确时刻读数（可批量）",
    responses={409: {"model": ErrorOut, "description": "同站同刻读数已存在（含不同时区写法），整单拒绝"}},
)
def add_readings(payload: ReadingBatchIn, conn=Depends(get_conn)) -> dict[str, int]:
    items = [(r.station, canonical_utc(r.utc_time), r.gauge_mm) for r in payload.readings]
    return service.add_readings(conn, items)


@app.post(
    "/readings/legacy-import",
    status_code=201,
    response_model=LegacyImportResult,
    tags=["读数"],
    summary="补报旧库整秒读数（入库为待确认）",
    responses={409: {"model": ErrorOut, "description": "该站该整秒已有待确认读数，整单拒绝"}},
)
def import_legacy_readings(payload: LegacyBatchIn, conn=Depends(get_conn)) -> LegacyImportResult:
    items = [(r.station, canonical_second(r.utc_time), r.gauge_mm) for r in payload.readings]
    return LegacyImportResult(**service.import_legacy_readings(conn, items))


@app.post(
    "/readings/confirm",
    response_model=ConfirmResult,
    tags=["读数"],
    summary="确认待确认读数的精确时刻（巡测员凭仪器原记录）",
    responses={
        404: {"model": ErrorOut, "description": "该站该旧整秒没有待确认读数"},
        409: {"model": ErrorOut, "description": "该精确时刻已有异值读数，拒绝确认并保留原数据"},
        422: {"model": ErrorOut, "description": "精确时刻不在旧整秒所在的一秒内"},
    },
)
def confirm_reading(payload: ConfirmIn, conn=Depends(get_conn)) -> ConfirmResult:
    result = service.confirm_legacy_reading(
        conn,
        station=payload.station,
        legacy_utc_time=canonical_second(payload.legacy_utc_time),
        utc_time=canonical_utc(payload.utc_time),
        operator=payload.operator,
    )
    return ConfirmResult(**result)


@app.get("/readings", response_model=list[ReadingOut], tags=["读数"], summary="查询读数（按实际精确时刻排序）")
def list_readings(
    station: str | None = None,
    utc_time: datetime | None = None,
    time_status: Literal["confirmed", "pending"] | None = None,
    conn=Depends(get_conn),
) -> list[ReadingOut]:
    rows = db.list_readings(
        conn,
        station=station,
        utc_time=None if utc_time is None else _exact_query_time(utc_time),
        time_status=time_status,
    )
    return [service._reading_out(r) for r in rows]


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
    summary="按精确时刻核发水面落差核定单",
    responses={422: {"model": ErrorOut, "description": "缺该精确时刻的已确认读数 / 未连通 / 闭环矛盾，拒发"}},
)
def issue(payload: IssueIn, conn=Depends(get_conn)) -> ApprovalOut:
    return service.issue_approval(conn, canonical_utc(payload.utc_time), payload.upstream, payload.downstream)


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
        confirmed_reading_count=db.count_readings_by_status(conn, "confirmed"),
        pending_reading_count=db.count_readings_by_status(conn, "pending"),
        valid_approval_count=db.count_approvals(conn, "valid"),
        revoked_approval_count=db.count_approvals(conn, "revoked"),
    )
