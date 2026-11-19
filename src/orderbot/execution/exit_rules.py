from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from orderbot.core.config import ExitConfig
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import MINUTE
from orderbot.data.replay import AnalyzedBar
from orderbot.strategy.stop_plan import StopPlan


@dataclass(frozen=True, slots=True)
class ExitFrame:
    at: datetime
    kind: Literal["tick", "candle", "timer"]
    underlying: Decimal | None = None
    option_ltp: Decimal | None = None
    candle: AnalyzedBar | None = None
    option_token: str | None = None
    timer_due: bool = False

    def __post_init__(self):
        object.__setattr__(self, "at", as_ist(self.at))
        if self.kind not in {"tick", "candle", "timer"} or any(
            value is not None and (not is_finite_decimal(value) or value <= 0)
            for value in (self.underlying, self.option_ltp)
        ):
            raise ValueError("Invalid exit event or market price.")


@dataclass(frozen=True, slots=True)
class TrailState:
    peak: Decimal | None = None
    stop: Decimal | None = None
    last_candle_at: datetime | None = None


def square_off_due(at: datetime, config: ExitConfig, expiry_cutoff: datetime | None = None) -> bool:
    return as_ist(at).time() >= config.square_off_time or (
        expiry_cutoff is not None and as_ist(at) >= as_ist(expiry_cutoff)
    )


def evaluate_exit(
    plan: StopPlan, opened_at: datetime, trail: TrailState, frame: ExitFrame, config: ExitConfig, *,
    kill: str | None = None, break_even: Decimal | None = None, expiry_cutoff: datetime | None = None,
) -> tuple[TrailState, str | None]:
    opened = as_ist(opened_at)
    if frame.at < opened:
        raise ValueError("Exit event precedes the entry fill.")
    if square_off_due(frame.at, config, expiry_cutoff):
        return trail, "forced_square_off"
    if kill is not None:
        return trail, "kill_switch"

    risk = plan.option_entry - plan.premium_stop
    peak = max(plan.option_entry, trail.peak) if trail.peak is not None else plan.option_entry
    if frame.kind == "tick" and frame.option_ltp is not None:
        peak = max(peak, frame.option_ltp)
    gain = peak - plan.option_entry
    state = replace(trail, peak=peak)
    closed = frame.candle if frame.kind == "candle" else None
    if closed is not None and not (
        closed.source == "feed" and opened < closed.bar.at + MINUTE <= frame.at
        and as_ist(closed.bar.at).date() == frame.at.date()
        and (state.last_candle_at is None or closed.bar.at > state.last_candle_at)
    ):
        closed = None
    if break_even is not None and gain >= config.break_even_r * risk:
        state = replace(state, stop=max(plan.premium_stop, state.stop or plan.premium_stop, break_even))
    if closed is not None:
        state = replace(state, last_candle_at=closed.bar.at)
        if gain > config.trail_start_r * risk:
            threshold = plan.option_entry + config.trail_peak_fraction * gain
            state = replace(state, stop=max(plan.premium_stop, state.stop or plan.premium_stop, threshold))

    direction = 1 if plan.side == "long" else -1
    if frame.kind == "tick":
        premium, underlying = frame.option_ltp, frame.underlying
        if premium is not None and premium <= plan.premium_floor:
            return state, "premium_floor"
        if underlying is not None and direction * (underlying - plan.underlying_stop) <= 0:
            return state, "underlying_stop"
        if premium is not None and premium <= (state.stop or plan.premium_stop):
            reason = "trailing_stop" if state.stop is not None and state.stop > plan.premium_stop else "premium_stop"
            return state, reason
        if premium is not None and premium >= plan.premium_target:
            return state, "premium_target"
        if underlying is not None and direction * (underlying - plan.underlying_target) >= 0:
            return state, "underlying_target"

    age = frame.at - opened
    timer = frame.kind == "timer" or frame.timer_due
    if timer and age >= timedelta(minutes=config.stall_minutes) and gain < config.stall_minimum_r * risk:
        return state, "stall"
    if closed is not None:
        rsi, vwap = closed.indicators.rsi, closed.indicators.vwap
        if is_finite_decimal(rsi) and (
            (plan.side == "long" and rsi > config.long_exit_rsi)
            or (plan.side == "short" and rsi < config.short_exit_rsi)
        ):
            return state, "rsi_exhaustion"
        if is_finite_decimal(vwap) and direction * (closed.bar.close - vwap) < 0:
            return state, "vwap_break"
    if timer and age >= timedelta(minutes=config.max_hold_minutes):
        return state, "max_hold"
    return state, None
