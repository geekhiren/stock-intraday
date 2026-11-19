"""Shared pieces for testing one intraday strategy at a time on one stock.

A strategy file exposes NAME, RULES (what the article says and what had to be assumed) and
`plans(day) -> list[Plan]`. `simulate` then fills each plan on one-minute candles:
entry at the signal bar's close plus one tick, stop first when a candle touches both stop and target,
one tick of slippage on stops, and full Indian intraday charges. Sizing follows the bot: a fixed rupee risk
per trade inside a fixed position size, no leverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import groupby

from orderbot.core.config import Config, MarketConfig
from orderbot.core.ist_time import parse_timestamp
from orderbot.strategy.stock_paper import intraday_charges

TICK = 0.05
SQUARE_OFF = "15:10"  # every strategy is flat by here
LAST_ENTRY = "14:45"


@dataclass(frozen=True, slots=True)
class Bar:
    clock: str  # HH:MM of the bar's last minute
    open: float
    high: float
    low: float
    close: float
    volume: int
    end: int  # index of its last one-minute bar in the day
    rsi: float | None = None  # RSI(14) of this timeframe, continuous across days


@dataclass(frozen=True, slots=True)
class Plan:
    at: int  # one-minute index whose close is the entry
    side: int  # +1 buy, -1 short
    stop: float
    target: float | None = None
    exit_at: int | None = None  # one-minute index of a rule-based exit, if the strategy has one
    time_exit: str = SQUARE_OFF


@dataclass(slots=True)
class Day:
    date: str
    minutes: list[Bar]
    previous_close: float | None
    earlier: list["Day"] = field(default_factory=list)  # prior sessions, oldest first
    _cache: dict = field(default_factory=dict)

    @property
    def vwap(self) -> list[float]:
        """Running VWAP at each one-minute bar."""
        if "vwap" not in self._cache:
            out, value, volume = [], 0.0, 0
            for bar in self.minutes:
                value += (bar.high + bar.low + bar.close) / 3 * bar.volume
                volume += bar.volume
                out.append(value / volume if volume else bar.close)
            self._cache["vwap"] = out
        return self._cache["vwap"]

    def bars(self, size: int) -> list[Bar]:
        """Completed `size`-minute bars of the session, with RSI(14) carried over from earlier sessions."""
        return self._cache[size]


def _offset(clock: str) -> int:
    """Minutes since the 09:15 open."""
    return int(clock[:2]) * 60 + int(clock[3:]) - 555


def _aggregate(minutes: list[Bar], size: int) -> list[Bar]:
    out: list[Bar] = []
    for slot, group in groupby(minutes, key=lambda bar: _offset(bar.clock) // size):
        bars = list(group)
        expected = list(range(slot * size, (slot + 1) * size))
        if [_offset(bar.clock) for bar in bars] != expected:
            continue
        first, last = bars[0], bars[-1]
        out.append(Bar(last.clock, first.open, max(bar.high for bar in bars), min(bar.low for bar in bars),
                       last.close, sum(bar.volume for bar in bars), last.end))
    return out


def build_days(rows: list, sizes=(1, 5, 15), *, market: MarketConfig | None = None) -> list[Day]:
    """rows: SmartAPI one-minute candles [timestamp, open, high, low, close, volume], oldest first."""
    days: list[Day] = []
    for at, opened, high, low, close, volume in sorted(
            ((parse_timestamp(stamp), *values) for stamp, *values in rows), key=lambda item: item[0]):
        clock = at.strftime("%H:%M")
        in_session = market.cash_phase(at) == "continuous" if market else "09:15" <= clock < "15:30"
        if at.second or at.microsecond or not in_session:
            continue
        day = at.date().isoformat()
        if not days or days[-1].date != day:
            days.append(Day(day, [], days[-1].minutes[-1].close if days else None, list(days)))
        minutes = days[-1].minutes
        minutes.append(Bar(clock, float(opened), float(high), float(low), float(close), int(volume), len(minutes)))
    for size in sizes:
        gain = loss = previous = None
        count = 0
        for day in days:
            bars = _aggregate(day.minutes, size)
            with_rsi = []
            for bar in bars:
                rsi = None
                if previous is not None:
                    change = bar.close - previous
                    up, down = max(change, 0.0), max(-change, 0.0)
                    count += 1
                    if count <= 14:
                        gain, loss = (gain or 0.0) + up / 14, (loss or 0.0) + down / 14
                    else:
                        gain, loss = (gain * 13 + up) / 14, (loss * 13 + down) / 14
                    if count >= 14:
                        rsi = 100 * gain / (gain + loss) if gain + loss else 50.0
                previous = bar.close
                with_rsi.append(Bar(bar.clock, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.end, rsi))
            day._cache[size] = with_rsi
    return days


def simulate(days: list[Day], plans, config: Config, *, risk: float | None = None, slot: float | None = None) -> list[dict]:
    """Run `plans(day)` on every day and return one dict per completed trade."""
    stocks = config.stocks
    if (risk is None and (stocks.capital_inr is None or stocks.risk_per_trade_pct is None)) or (
            slot is None and stocks.capital_inr is None):
        raise ValueError("Set stock paper capital and risk per trade or pass an explicit simulation budget.")
    risk = float(stocks.capital_inr * stocks.risk_per_trade_pct / 100) if risk is None else risk
    slot = float(stocks.capital_inr / stocks.max_positions) if slot is None else slot
    trades = []
    for day in days:
        free_from = 0  # one position at a time: a plan that starts before the last trade ended is skipped
        for plan in sorted(plans(day), key=lambda item: item.at):
            if plan.at < free_from or plan.at >= len(day.minutes) - 1 or day.minutes[plan.at].clock >= LAST_ENTRY:
                continue
            entry = day.minutes[plan.at].close + plan.side * TICK
            distance = abs(entry - plan.stop)
            quantity = int(min(risk / distance, slot / entry)) if distance > 0 else 0
            if quantity < 1 or plan.side * (entry - plan.stop) <= 0:
                continue
            exit_price, reason, index = day.minutes[-1].close, "close", len(day.minutes) - 1
            for index in range(plan.at + 1, len(day.minutes)):
                bar = day.minutes[index]
                adverse, favourable = (bar.low, bar.high) if plan.side > 0 else (bar.high, bar.low)
                if bar.clock >= plan.time_exit:
                    exit_price, reason = bar.open, "time"
                    break
                if plan.side * (adverse - plan.stop) <= 0:
                    exit_price, reason = (min(plan.stop, bar.open) if plan.side > 0 else max(plan.stop, bar.open)) - plan.side * TICK, "stop"
                    break
                if plan.target is not None and plan.side * (favourable - plan.target) >= 0:
                    exit_price, reason = plan.target, "target"
                    break
                if plan.exit_at is not None and index >= plan.exit_at:
                    exit_price, reason = bar.close - plan.side * TICK, "rule"
                    break
            buy, sell = (entry, exit_price) if plan.side > 0 else (exit_price, entry)
            charges = float(intraday_charges(Decimal(str(round(buy, 2))), Decimal(str(round(sell, 2))), quantity, config))
            gross = (sell - buy) * quantity
            trades.append({"day": day.date, "entry_at": day.minutes[plan.at].clock, "exit_at": day.minutes[index].clock,
                           "side": "buy" if plan.side > 0 else "short", "quantity": quantity, "entry": round(entry, 2),
                           "exit": round(exit_price, 2), "reason": reason, "gross": round(gross, 2), "net": round(gross - charges, 2)})
            free_from = index + 1
    return trades


def summary(trades: list[dict]) -> dict:
    wins = [trade["net"] for trade in trades if trade["net"] > 0]
    losses = [trade["net"] for trade in trades if trade["net"] <= 0]
    return {
        "trades": len(trades), "accuracy_pct": round(100 * len(wins) / len(trades), 1) if trades else None,
        "net": round(sum(wins) + sum(losses), 2), "gross": round(sum(trade["gross"] for trade in trades), 2),
        "average_win": round(sum(wins) / len(wins), 2) if wins else 0.0, "average_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if sum(losses) < 0 else None,
    }


def average(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0
