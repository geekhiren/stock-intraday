from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import TradingSession


@dataclass(frozen=True, slots=True)
class Health:
    last_option_tick_at: datetime | None = None
    ws_disconnected_at: datetime | None = None
    rejections: int = 0
    position_mismatch: bool = False
    insufficient_funds: bool = False
    clock_drift_seconds: Decimal | None = None
    dashboard_stop: bool = False


class KillSwitch:
    def __init__(self, config: Config, directory: Path):
        self.config = config
        self.sentinel = directory / config.kill_switch.sentinel_path
        self._lows: deque[tuple[datetime, Decimal]] = deque()
        self._highs: deque[tuple[datetime, Decimal]] = deque()
        self._last: tuple[datetime, Decimal] | None = None

    def observe_underlying(self, at: datetime, price: Decimal) -> None:
        now = as_ist(at)
        if not is_finite_decimal(price) or price <= 0:
            raise ValueError("Underlying observations need a positive finite price.")
        if self._last is not None and now < self._last[0]:
            raise ValueError("Underlying observation timestamps moved backwards.")
        self._last = now, price
        while self._lows and self._lows[-1][1] >= price:
            self._lows.pop()
        while self._highs and self._highs[-1][1] <= price:
            self._highs.pop()
        self._lows.append((now, price))
        self._highs.append((now, price))
        self._discard_old(now)

    def _discard_old(self, now: datetime) -> None:
        oldest = now - timedelta(minutes=self.config.kill_switch.volatility_window_minutes)
        for observations in (self._lows, self._highs):
            while observations and (observations[0][0].date() != now.date() or observations[0][0] < oldest):
                observations.popleft()

    def _volatility_spike(self, now: datetime) -> bool:
        self._discard_old(now)
        if not self._lows or not self._highs:
            return False
        current = self._last[1]
        low, high = self._lows[0][1], self._highs[0][1]
        cap = self.config.kill_switch.volatility_move_fraction
        return current - low > low * cap or high - current > high * cap

    def evaluate(self, at: datetime, health: Health, *, position_opened_at: datetime | None) -> str | None:
        now = as_ist(at)
        cfg = self.config.kill_switch
        session = TradingSession.from_config(now.date(), self.config.market)
        if health.position_mismatch:
            return "position_mismatch"
        if health.insufficient_funds:
            return "insufficient_funds"
        if health.dashboard_stop:
            return "dashboard_stop"
        if self.sentinel.is_file():
            return "sentinel"
        if health.rejections >= cfg.rejection_limit:
            return "order_rejections"
        drift = health.clock_drift_seconds
        if drift is not None:
            if not is_finite_decimal(drift):
                raise ValueError("Clock drift needs a finite Decimal.")
            if abs(drift) > cfg.max_clock_drift_seconds:
                return "clock_drift"
        if health.ws_disconnected_at is not None and (
            now - as_ist(health.ws_disconnected_at)
        ).total_seconds() >= self.config.data.websocket_reconnect_seconds:
            return "websocket_down"
        if position_opened_at is not None and session.opens_at <= now < session.closes_at:
            last_tick = as_ist(health.last_option_tick_at) if health.last_option_tick_at is not None else None
            if last_tick is not None and last_tick > now:
                return "clock_drift"
            last_tick = max(as_ist(position_opened_at), last_tick) if last_tick is not None else as_ist(position_opened_at)
            if (now - last_tick).total_seconds() >= self.config.data.websocket_stale_seconds:
                return "stale_tick"
        if self._volatility_spike(now):
            return "volatility_spike"
        return None
