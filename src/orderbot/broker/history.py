from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from random import uniform
from time import sleep

from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import DataConfig
from orderbot.core.ist_time import as_ist, parse_timestamp
from orderbot.data.candle_builder import Candle, minute_floor

from .angel_client import RateLimitError, RequestError


class HistoryError(ValueError):
    """Broker history is unavailable or malformed."""


def parse_minutes(rows, start: datetime, end: datetime) -> tuple[Candle, ...]:
    if not isinstance(rows, list):
        raise HistoryError("Historical candle response is not an array.")
    start, end = as_ist(start), as_ist(end)
    parsed: dict[datetime, Candle] = {}
    try:
        for stamp, opening, high, low, close, volume in rows:
            at = parse_timestamp(stamp)
            if not start <= at < end:
                continue
            if type(volume) is not int:
                raise ValueError("History candle volume is not integral.")
            if at in parsed:
                raise ValueError("Duplicate candle timestamp.")
            prices = (Decimal(str(price)) for price in (opening, high, low, close))
            parsed[at] = Candle(at, *prices, volume)
    except (TypeError, ValueError, InvalidOperation):
        raise HistoryError("SmartAPI returned malformed or duplicate candle data.") from None
    return tuple(parsed[at] for at in sorted(parsed))


def encode_minutes(candles: tuple[Candle, ...]) -> list:
    return [
        [bar.at.isoformat(), str(bar.open), str(bar.high), str(bar.low), str(bar.close), bar.volume]
        for bar in candles
    ]


class HistoryCache:
    def __init__(
        self, config: DataConfig, directory: Path, *, sleep=sleep, jitter=lambda cap: uniform(0, cap),
    ):
        self.config = config
        self.directory = directory / config.history_cache_dir
        self.sleep = sleep
        self.jitter = jitter

    def get(
        self, token: str, start: datetime, end: datetime, fetch: Callable[[], list],
    ) -> tuple[Candle, ...]:
        start, end = as_ist(start), as_ist(end)
        if not token.isascii() or not token.isdigit():
            raise HistoryError("History cache requires a numeric instrument token.")
        if start != minute_floor(start) or end != minute_floor(end) or start >= end:
            raise HistoryError("History requires an increasing range of whole minutes.")
        key = f"{start:%Y%m%d-%H%M}_{end:%Y%m%d-%H%M}.json"
        target = self.directory / token / key
        if target.is_file():
            try:
                return parse_minutes(json.loads(target.read_text(encoding="utf-8")), start, end)
            except (json.JSONDecodeError, UnicodeError):
                raise HistoryError("Cached SmartAPI history is unreadable.") from None

        backoff = self.config.history_backoff_seconds
        for attempt in range(self.config.history_attempts):
            try:
                rows = fetch()
            except RateLimitError:
                raise
            except RequestError as exc:
                if not exc.retryable or attempt + 1 == self.config.history_attempts:
                    raise HistoryError(str(exc)) from None
                self.sleep(self.jitter(backoff))
                backoff = min(backoff * 2, self.config.history_backoff_max_seconds)
                continue
            result = parse_minutes(rows, start, end)
            if result:
                with atomic_path(target) as temporary:
                    # Cache the closed range only. The broker may return the inclusive endpoint.
                    temporary.write_text(json.dumps(encode_minutes(result), separators=(",", ":")), encoding="utf-8")
            return result
        raise AssertionError("History retry loop exhausted without a result.")
