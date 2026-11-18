from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from orderbot.core.ist_time import as_ist

from .candle_builder import Candle


class PivotLevels(NamedTuple):
    pivot: Decimal
    resistance_one: Decimal
    resistance_two: Decimal
    support_one: Decimal
    support_two: Decimal


def floor_pivots(high: Decimal, low: Decimal, close: Decimal) -> PivotLevels:
    if not (low <= close <= high):
        raise ValueError("Previous-session OHLC values are inconsistent.")
    pivot = (high + low + close) / 3
    span = high - low
    return PivotLevels(pivot, 2 * pivot - low, pivot + span, 2 * pivot - high, pivot - span)


class WilderAverage:
    def __init__(self, period: int):
        if period <= 0:
            raise ValueError("Wilder period must be positive.")
        self.period = period
        self._seed_count = 0
        self._seed_total = Decimal(0)
        self.value: Decimal | None = None

    def update(self, value: Decimal) -> Decimal | None:
        if self.value is None:
            self._seed_count += 1
            self._seed_total += value
            if self._seed_count == self.period:
                self.value = self._seed_total / self.period
        else:
            self.value = (self.value * (self.period - 1) + value) / self.period
        return self.value


@dataclass(frozen=True, slots=True)
class BarIndicators:
    vwap: Decimal | None
    rsi: Decimal | None
    atr: Decimal | None


class IndicatorEngine:
    def __init__(self, rsi_period: int, atr_period: int):
        self._gain = WilderAverage(rsi_period)
        self._loss = WilderAverage(rsi_period)
        self._atr = WilderAverage(atr_period)
        self._session: date | None = None
        self._volume = 0
        self._weighted_sum = Decimal(0)
        self._last_at: datetime | None = None
        self._last_close: Decimal | None = None

    def update(self, candle: Candle) -> BarIndicators:
        if self._last_at is not None and candle.at <= self._last_at:
            raise ValueError("Indicators require strictly increasing closed candles.")
        day = as_ist(candle.at).date()
        if day != self._session:
            self._session = day
            self._volume = 0
            self._weighted_sum = Decimal(0)
        self._weighted_sum += (candle.high + candle.low + candle.close) * candle.volume
        self._volume += candle.volume
        vwap = self._weighted_sum / (3 * self._volume) if self._volume else None

        true_range = candle.high - candle.low
        rsi = None
        if self._last_close is not None:
            change = candle.close - self._last_close
            gain = self._gain.update(max(change, Decimal(0)))
            loss = self._loss.update(max(-change, Decimal(0)))
            if gain is not None and loss is not None:
                rsi = 100 * gain / (gain + loss) if gain or loss else Decimal(50)
            true_range = max(true_range, abs(candle.high - self._last_close), abs(candle.low - self._last_close))
        atr = self._atr.update(true_range)
        self._last_at, self._last_close = candle.at, candle.close
        return BarIndicators(vwap, rsi, atr)
