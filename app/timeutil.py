"""时刻规范化工具：所有对外时刻统一为精确到微秒的 UTC 文本。

统一格式（定长，可直接按文本排序）：
    2026-11-22T08:00:00.123456Z

- 带时区偏移的写法（如 +08:00）先换算到 UTC；
- 同一时刻的不同时区写法得到同一文本，因此可用于判重；
- 旧库的整秒文本迁移后规范为 ``.000000Z``。
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC_MICRO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时刻必须带时区信息，建议使用 Z 结尾的 UTC 时刻，如 2026-11-22T08:00:00.123456Z")
    return value


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(timezone.utc)


def format_utc_micros(value: datetime) -> str:
    """规范成 ``YYYY-MM-DDTHH:MM:SS.ffffffZ``（固定 6 位小数）。"""
    return to_utc(value).strftime(UTC_MICRO_FORMAT)


def canonical_utc(value: datetime) -> str:
    return format_utc_micros(value)


def canonical_second(value: datetime) -> str:
    """把时刻截到整秒并规范成 ``.000000Z`` 文本。"""
    return to_utc(value).replace(microsecond=0).strftime(UTC_MICRO_FORMAT)


def floor_canonical_to_second(text: str) -> str:
    """对已规范的微秒文本截断到整秒文本。"""
    return text[:19] + ".000000Z"


def is_whole_second(text: str) -> bool:
    return text.endswith(".000000Z")


def parse_stored_utc(text: str) -> datetime:
    """解析库内旧时刻（可能无微秒、带 Z 或带偏移），统一返回 UTC。"""
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_stored_utc(text: str) -> str:
    return format_utc_micros(parse_stored_utc(text))
