from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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


def parse_exchange_timestamp(value: str) -> datetime:
    """Parse a SmartAPI FULL exchange feed timestamp, whose displayed time is IST."""
    try:
        return parse_timestamp(value)
    except ValueError:
        if not isinstance(value, str):
            raise ValueError("Missing SmartAPI exchange feed timestamp.") from None
        match = re.fullmatch(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2})", value)
        if match is None or match[2].title() not in _MONTHS:
            raise ValueError("Unusable SmartAPI exchange feed timestamp.") from None
        day, month, year, hour, minute, second = match.groups()
        at = datetime(int(year), _MONTHS.index(month.title()) + 1, int(day),
                      int(hour), int(minute), int(second), tzinfo=IST)
        if at.astimezone(UTC).timestamp() == 0:
            raise ValueError("SmartAPI returned an exchange timestamp placeholder.") from None
        return at


def from_storage(value: str) -> datetime:
    if not value.endswith("+00:00"):
        raise ValueError("Expected a timestamp with an explicit UTC offset.")
    return parse_timestamp(value)
