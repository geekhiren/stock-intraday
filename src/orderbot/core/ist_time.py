from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def require_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Expected a timezone-aware datetime.")
    return value


def now_ist() -> datetime:
    return datetime.now(IST)


def as_ist(value: datetime) -> datetime:
    return require_aware(value).astimezone(IST)


def to_storage(value: datetime) -> str:
    return require_aware(value).astimezone(UTC).isoformat(timespec="microseconds")


def parse_timestamp(value: str) -> datetime:
    pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Expected an ISO 8601 timestamp with an explicit offset.")
    return as_ist(datetime.fromisoformat(value))


def from_storage(value: str) -> datetime:
    if not value.endswith("+00:00"):
        raise ValueError("Expected a timestamp with an explicit UTC offset.")
    return parse_timestamp(value)
