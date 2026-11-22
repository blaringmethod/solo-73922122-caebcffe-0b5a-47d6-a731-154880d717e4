"""Pydantic 请求/响应模型（供 /docs 演示）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_text(value: datetime) -> str:
    """把时刻规范成 UTC 字符串存储与比较。"""
    if value.tzinfo is None:
        raise ValueError("时刻必须带时区信息，建议使用 Z 结尾的 UTC 时刻，如 2026-11-22T08:00:00Z")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReadingIn(BaseModel):
    station: str = Field(..., min_length=1, max_length=32, description="站号（水尺所在站）", examples=["S1"])
    utc_time: datetime = Field(..., alias="utc_time", description="UTC 时刻（带时区，Z 结尾）", examples=["2026-11-22T08:00:00Z"])
    gauge_mm: int = Field(..., strict=True, description="整数毫米水尺读数", examples=[1234])

    @field_validator("utc_time")
    @classmethod
    def _check_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("时刻必须带时区信息")
        return v

    model_config = ConfigDict(populate_by_name=True)


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1)


class ReadingOut(BaseModel):
    station: str
    utc_time: str
    gauge_mm: int


class LineIn(BaseModel):
    upstream: str = Field(..., min_length=1, max_length=32, description="上游尺站号", examples=["S1"])
    downstream: str = Field(..., min_length=1, max_length=32, description="下游尺站号", examples=["S2"])
    zero_offset_mm: int = Field(
        ...,
        strict=True,
        description="两尺零点的有向毫米高差：上游尺零点高程 - 下游尺零点高程",
        examples=[15],
    )

    @field_validator("downstream")
    @classmethod
    def _no_self_loop(cls, v: str, info):
        other = info.data.get("upstream")
        if other is not None and v == other:
            raise ValueError("同一站号不能构成本尺测线（自环）")
        return v


class LineReplaceIn(BaseModel):
    lines: list[LineIn] = Field(default_factory=list, description="新测线全集，整批替换旧测线")


class LineOut(LineIn):
    pass


class IssueIn(BaseModel):
    utc_time: datetime = Field(..., description="UTC 时刻（带时区）", examples=["2026-11-22T08:00:00Z"])
    upstream: str = Field(..., min_length=1, max_length=32, description="上游站号", examples=["S1"])
    downstream: str = Field(..., min_length=1, max_length=32, description="下游站号", examples=["S2"])

    @field_validator("utc_time")
    @classmethod
    def _check_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("时刻必须带时区信息")
        return v

    @field_validator("downstream")
    @classmethod
    def _distinct(cls, v: str, info):
        other = info.data.get("upstream")
        if other is not None and v == other:
            raise ValueError("上下游站号必须不同")
        return v


class ApprovalOut(BaseModel):
    approval_no: int
    utc_time: str
    upstream: str
    downstream: str
    upstream_reading_mm: int
    downstream_reading_mm: int
    zero_offset_mm: int
    fall_mm: int
    line_version: int
    status: Literal["valid", "revoked"]
    revoke_reason: str | None = None
    created_at: str
    revoked_at: str | None = None


class RevokedItem(BaseModel):
    approval_no: int
    reason: str


class ReplaceResult(BaseModel):
    line_version_before: int
    line_version_after: int
    line_count: int
    still_valid: list[ApprovalOut]
    revoked: list[RevokedItem]


class StatusOut(BaseModel):
    line_version: int
    line_count: int
    reading_count: int
    valid_approval_count: int
    revoked_approval_count: int


class ErrorOut(BaseModel):
    detail: str
