"""Pydantic 请求/响应模型（供 /docs 演示）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _AwareTimeMixin(BaseModel):
    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时刻必须带时区信息，建议使用 Z 结尾的 UTC 时刻，如 2026-11-22T08:00:00.123456Z")
        return value


class ReadingIn(_AwareTimeMixin):
    station: str = Field(..., min_length=1, max_length=32, description="站号（水尺所在站）", examples=["S1"])
    utc_time: datetime = Field(
        ...,
        description="精确到微秒的 UTC 时刻（带时区；+08:00 等写法会换算成 UTC）",
        examples=["2026-11-22T08:00:00.123456Z"],
    )
    gauge_mm: int = Field(..., strict=True, description="整数毫米水尺读数", examples=[1234])

    @field_validator("utc_time")
    @classmethod
    def _check_utc(cls, v: datetime) -> datetime:
        return cls._require_aware(v)

    model_config = ConfigDict(populate_by_name=True)


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1)


class LegacyReadingIn(_AwareTimeMixin):
    """补报旧库整秒读数：时刻只能精确到整秒，入库后为待确认。"""

    station: str = Field(..., min_length=1, max_length=32, description="站号", examples=["S1"])
    utc_time: datetime = Field(
        ...,
        description="旧整秒时刻（必须整秒，带时区）",
        examples=["2026-11-22T08:00:00Z"],
    )
    gauge_mm: int = Field(..., strict=True, description="整数毫米水尺读数", examples=[1000])

    @field_validator("utc_time")
    @classmethod
    def _check_whole_second(cls, v: datetime) -> datetime:
        cls._require_aware(v)
        if v.microsecond != 0:
            raise ValueError("补报旧整秒读数时，时刻必须正好是整秒（不得带微秒）")
        return v


class LegacyBatchIn(BaseModel):
    readings: list[LegacyReadingIn] = Field(..., min_length=1)


class ConfirmIn(_AwareTimeMixin):
    """巡测员凭仪器原记录，把某站某旧整秒的待确认读数确认到该秒内的精确时刻。"""

    station: str = Field(..., min_length=1, max_length=32, description="站号", examples=["S1"])
    legacy_utc_time: datetime = Field(
        ...,
        description="该行原旧整秒时刻（带时区，必须整秒）",
        examples=["2026-11-22T08:00:00Z"],
    )
    utc_time: datetime = Field(
        ...,
        description="仪器原记录给出的精确时刻，须落在旧整秒所在的一秒内",
        examples=["2026-11-22T08:00:00.123456Z"],
    )
    operator: str = Field(..., min_length=1, max_length=64, description="确认人（巡测员）", examples=["巡测员-张三"])

    @field_validator("legacy_utc_time")
    @classmethod
    def _check_legacy_whole_second(cls, v: datetime) -> datetime:
        cls._require_aware(v)
        if v.microsecond != 0:
            raise ValueError("旧整秒时刻必须正好是整秒（不得带微秒）")
        return v

    @field_validator("utc_time")
    @classmethod
    def _check_utc(cls, v: datetime) -> datetime:
        return cls._require_aware(v)


class ReadingOut(BaseModel):
    id: int
    station: str
    utc_time: str
    gauge_mm: int
    time_status: Literal["confirmed", "pending"]
    confirmed_at: str | None = None
    confirmed_by: str | None = None


class LegacyImportResult(BaseModel):
    inserted: int
    pending_total: int


class ConfirmResult(BaseModel):
    merged: bool = Field(..., description="该精确时刻已有同站读数且数值相同，已合并为一条")
    reading: ReadingOut


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


class IssueIn(_AwareTimeMixin):
    utc_time: datetime = Field(
        ...,
        description="核发所用的精确 UTC 时刻；上下游必须都有该精确时刻的已确认读数",
        examples=["2026-11-22T08:00:00.123456Z"],
    )
    upstream: str = Field(..., min_length=1, max_length=32, description="上游站号", examples=["S1"])
    downstream: str = Field(..., min_length=1, max_length=32, description="下游站号", examples=["S2"])

    @field_validator("utc_time")
    @classmethod
    def _check_utc(cls, v: datetime) -> datetime:
        return cls._require_aware(v)

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
    confirmed_reading_count: int
    pending_reading_count: int
    valid_approval_count: int
    revoked_approval_count: int


class ErrorOut(BaseModel):
    detail: str
