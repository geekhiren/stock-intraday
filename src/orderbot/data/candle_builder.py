from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from orderbot.broker.instruments import NSE_DERIVATIVES
from orderbot.core.config import MarketConfig
from orderbot.core.ist_time import IST, as_ist

if TYPE_CHECKING:
    from orderbot.broker.ws_client import MarketTick


MINUTE = timedelta(minutes=1)


def minute_floor(value: datetime) -> datetime:
    return as_ist(value).replace(second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class TradingSession:
    day: date
    opens_at: datetime
    closes_at: datetime

    @classmethod
    def from_config(cls, day: date, config: MarketConfig) -> TradingSession:
        return cls(day, datetime.combine(day, config.session_open, tzinfo=IST),
                   datetime.combine(day, config.session_close, tzinfo=IST))


@dataclass(frozen=True, slots=True)
class Candle:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self):
        if minute_floor(self.at) != self.at:
            raise ValueError("Candle timestamp must be a timezone-aware minute boundary.")
        if self.volume < 0 or not all(
            price.is_finite() and price > 0 for price in (self.open, self.high, self.low, self.close)
        ):
            raise ValueError("Candle needs positive finite prices and nonnegative volume.")
        if not (self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError("Candle OHLC values are inconsistent.")


class CandleBuilder:
    def __init__(
        self, session: TradingSession, grace_seconds: float, signal_token: str, *,
        started_at: datetime | None = None,
    ):
        self.session = session
        self.signal_token = signal_token
        self.grace = timedelta(seconds=grace_seconds)
        start = as_ist(started_at) if started_at is not None else session.opens_at
        first = minute_floor(start)
        if start != first:
            first += MINUTE
        self._first_full_minute = max(session.opens_at, first)
        self._last_received: datetime | None = None
        self._volume: int | None = None
        self.opening_volume_observed = False
        self._pending: dict[datetime, list[MarketTick]] = defaultdict(list)
        self.late_dropped = 0

    def step(self, received_at: datetime, tick: MarketTick | None = None) -> tuple[Candle, ...]:
        received = as_ist(received_at)
        if self._last_received is not None and received < self._last_received:
            raise ValueError("Capture timestamps moved backwards.")
        self._last_received = received
        cutoff = received - MINUTE - self.grace
        closed = []
        for start in sorted(at for at in self._pending if at <= cutoff):
            candle = self._close(start, self._pending.pop(start))
            if candle is not None:
                closed.append(candle)
        if tick is None or tick.token != self.signal_token or tick.exchange_type != NSE_DERIVATIVES:
            return tuple(closed)
        preopen_trade = (
            tick.trade_at is not None and tick.trade_at.date() == self.session.day
            and tick.trade_at < self.session.opens_at
        )
        preopen_zero_volume = (
            tick.exchange_at.date() == self.session.day and tick.exchange_at < self.session.opens_at
            and tick.volume == 0
        )
        if (
            self._first_full_minute == self.session.opens_at and cutoff < self.session.opens_at
            and (preopen_trade or preopen_zero_volume)
        ):
            self.opening_volume_observed = True
            self._volume = max(self._volume or 0, tick.volume)
        if tick.trade_at is None:
            return tuple(closed)
        start = minute_floor(tick.trade_at)
        if self.session.opens_at <= start < self.session.closes_at:
            if start <= cutoff:
                # Most repeated snapshots are quote updates with the last trade still attached.
                if self._volume is None or tick.volume > self._volume:
                    self.late_dropped += 1
            else:
                self._pending[start].append(tick)
        return tuple(closed)

    def _close(self, start: datetime, ticks: list[MarketTick]) -> Candle | None:
        ticks.sort(key=lambda tick: (tick.trade_at, tick.volume, tick.sequence))
        previous_volume = self._volume
        if previous_volume is None or start < self._first_full_minute:
            self._volume = max(previous_volume or 0, *(tick.volume for tick in ticks))
            return None
        prices = []
        for tick in ticks:
            if tick.volume > self._volume:
                prices.append(tick.price)
                self._volume = tick.volume
        if not prices:
            return None
        return Candle(start, prices[0], max(prices), min(prices), prices[-1], self._volume - previous_volume)
