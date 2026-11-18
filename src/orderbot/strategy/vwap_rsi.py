from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from orderbot.broker.instruments import InstrumentMaster
from orderbot.core.config import Config, StrategyAConfig
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import MINUTE, TradingSession
from orderbot.data.indicators import PivotLevels
from orderbot.data.replay import AnalyzedBar
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import StrikeDecision, select_strike

from .iv_store import IVStore, nearest_rank
from .stop_plan import Side, StopPlan, build_stop_plan


GATES = (
    "closed_candle", "vwap_crossover", "consolidation", "volume_surge", "rsi",
    "pivot", "liquidity", "time_window", "iv",
)


@dataclass(frozen=True, slots=True)
class GateResult:
    status: Literal["pass", "fail", "disabled", "not_evaluated"]
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Signal:
    side: Side
    right: Literal["CE", "PE"]
    selection: StrikeDecision
    stop_plan: StopPlan
    target_bid: Decimal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    id: str
    candle_at: datetime
    at: datetime
    token: str
    gates: dict[str, GateResult]
    reason: str
    signal: Signal | None = None

    def view(self) -> dict:
        record = {
            "id": self.id, "candle_at": self.candle_at.isoformat(), "decision_at": self.at.isoformat(),
            "future_token": self.token, "reason": self.reason,
            "gates": {name: asdict(result) for name, result in self.gates.items()},
        }
        if self.signal is not None:
            record.update(
                side=self.signal.side, right=self.signal.right, option_token=self.signal.selection.selected_token,
                stop_plan=self.signal.stop_plan.view(), target_bid=str(self.signal.target_bid),
            )
        return record


class StrategyA:
    def __init__(
        self, config: Config, master: InstrumentMaster, trading_sessions: tuple[date, ...], iv_store: IVStore | None,
    ):
        self.config, self.master, self.trading_sessions, self.iv_store = config, master, trading_sessions, iv_store

    def evaluate(
        self, row: AnalyzedBar, history: Sequence[AnalyzedBar], *, at: datetime, token: str, spot: Decimal | None,
        quotes: Mapping[str, OptionQuote], pivots: PivotLevels | None = None,
    ) -> StrategyDecision:
        now, opens = as_ist(at), as_ist(row.bar.at)
        uid = f"vwap-{token}-{opens:%Y%m%d%H%M}"
        gates = {name: GateResult("not_evaluated") for name in GATES}

        def skip(gate, reason, **detail):
            gates[gate] = GateResult("fail", detail | {"reason": reason})
            return StrategyDecision(uid, opens, now, token, gates, reason)

        cfg = self.config.strategy_a
        session = TradingSession.from_config(opens.date(), self.config.market)
        if row.source != "feed":
            return skip("closed_candle", "backfill_candle")
        if now < opens + MINUTE:
            return skip("closed_candle", "unclosed_candle")
        if (
            not session.opens_at <= opens < session.closes_at or now.date() != session.day
            or (now - opens - MINUTE).total_seconds() >= self.config.data.websocket_stale_seconds
        ):
            return skip("closed_candle", "stale_candle")
        if token != self.master.front_future(session.day).token:
            return skip("closed_candle", "wrong_signal_instrument")
        minutes_from_open = (opens - session.opens_at) // MINUTE
        if len(history) != minutes_from_open or any(
            as_ist(item.bar.at) != session.opens_at + index * MINUTE for index, item in enumerate(history)
        ):
            return skip("closed_candle", "incomplete_session_history")
        gates["closed_candle"] = GateResult("pass")

        market_expiry = any(contract.expiry == now.date() for contract in self.master.options)
        cutoff, cause = entry_window(now, market_expiry, cfg)
        if cause is not None:
            return skip("time_window", cause)
        gates["time_window"] = GateResult("pass", {"cutoff": cutoff.isoformat(), "expiry_day": market_expiry})

        vwap = row.indicators.vwap
        if not history or not _positive(vwap) or not _positive(history[-1].indicators.vwap):
            return skip("vwap_crossover", "vwap_unavailable")
        previous = history[-1]
        close, prev_close, prev_vwap = row.bar.close, previous.bar.close, previous.indicators.vwap
        if close > vwap and prev_close <= prev_vwap:
            side, right = "long", "CE"
        elif close < vwap and prev_close >= prev_vwap:
            side, right = "short", "PE"
        else:
            return skip("vwap_crossover", "no_crossover")
        gates["vwap_crossover"] = GateResult("pass", {
            "side": side, "close": str(close), "vwap": str(vwap),
            "previous_close": str(prev_close), "previous_vwap": str(prev_vwap),
        })

        prior_consolidation = history[-cfg.consolidation_lookback:]
        if len(prior_consolidation) != cfg.consolidation_lookback or any(
            not _positive(item.indicators.vwap) for item in prior_consolidation
        ):
            return skip("consolidation", "consolidation_history_unavailable")
        near = sum(
            abs(item.bar.close - item.indicators.vwap) < item.indicators.vwap * cfg.consolidation_band
            for item in prior_consolidation
        )
        if near > cfg.consolidation_max_near:
            return skip("consolidation", "consolidating", near_vwap=near)
        gates["consolidation"] = GateResult("pass", {"near_vwap": near})

        prior_volume = history[-cfg.volume_lookback:]
        if len(prior_volume) != cfg.volume_lookback:
            return skip("volume_surge", "volume_history_unavailable")
        total = sum(item.bar.volume for item in prior_volume)
        mean = Decimal(total) / cfg.volume_lookback
        volume_detail = {"prior_mean": str(mean), "volume": row.bar.volume}
        if total <= 0 or row.bar.volume * cfg.volume_lookback < cfg.volume_multiple * total:
            return skip("volume_surge", "no_volume", **volume_detail)
        gates["volume_surge"] = GateResult("pass", volume_detail)

        low, high = cfg.rsi_long_band if side == "long" else cfg.rsi_short_band
        rsi = row.indicators.rsi
        if not is_finite_decimal(rsi) or not low <= rsi <= high:
            return skip("rsi", "rsi", value=str(rsi) if rsi is not None else None)
        gates["rsi"] = GateResult("pass", {"value": str(rsi)})

        if cfg.require_pivot:
            if pivots is None:
                return skip("pivot", "pivot_unavailable")
            level = _pivot_match(close, side, pivots, cfg.pivot_tolerance)
            if level is None:
                return skip("pivot", "pivot")
            gates["pivot"] = GateResult("pass", {"level": level})
        else:
            gates["pivot"] = GateResult("disabled")

        if not _positive(spot):
            return skip("liquidity", "skipped_illiquid", cause="spot_unavailable")
        selection = select_strike(
            self.master, self.trading_sessions, quotes, at=now, spot=spot, right=right, lots=1,
            config=self.config.options,
        )
        if selection.outcome != "selected":
            return skip("liquidity", "skipped_illiquid", cause=selection.outcome,
                        expiry_reason=selection.expiry.reason,
                        candidates={str(item.strike): list(item.reasons) for item in selection.candidates})
        picked = next(item for item in selection.candidates if (
            item.contract is not None and item.contract.token == selection.selected_token
        ))
        gates["liquidity"] = GateResult("pass", {
            "token": picked.contract.token, "symbol": picked.contract.symbol, "bid": str(picked.quote.bids[0].price),
            "ask": str(picked.quote.asks[0].price), "spot": str(spot), "delta": str(picked.greeks.delta),
        })

        baseline = self.iv_store.before(now.date(), right, cfg.iv_lookback_sessions) if self.iv_store else ()
        atm = next((item.greeks.iv for item in selection.candidates if (
            item.strike == selection.atm and item.greeks is not None
        )), None)
        iv_detail = {"prior_sessions": len(baseline), "atm_iv": str(atm) if atm is not None else None}
        if len(baseline) < cfg.iv_lookback_sessions:
            gates["iv"] = GateResult("disabled", iv_detail)
        else:
            threshold = nearest_rank(baseline, cfg.iv_percentile)
            iv_detail["threshold"] = str(threshold)
            if atm is None:
                return skip("iv", "atm_iv_unavailable", **iv_detail)
            if atm >= threshold:
                return skip("iv", "iv_at_or_above_threshold", **iv_detail)
            gates["iv"] = GateResult("pass", iv_detail)

        if not _positive(row.indicators.atr):
            return StrategyDecision(uid, opens, now, token, gates, "atr_unavailable")
        projected_entry = picked.quote.asks[0].price + picked.contract.price_tick * self.config.costs.slippage_ticks
        try:
            plan = build_stop_plan(side, close, row.indicators.atr, picked.greeks.delta, projected_entry, cfg)
        except ValueError:
            return StrategyDecision(uid, opens, now, token, gates, "stop_plan_unavailable")
        # Hold the observed half-spread fixed for the Phase 2 target-friction estimate.
        target_bid = plan.premium_target - picked.spread / 2
        signal = Signal(side, right, selection, plan, target_bid)
        return StrategyDecision(uid, opens, now, token, gates, "signal", signal)


def entry_window(at: datetime, expiry_day: bool, config: StrategyAConfig) -> tuple[time, str | None]:
    current = as_ist(at).time()
    cutoff = config.expiry_entry_close if expiry_day else config.entry_close
    if current < config.entry_open or current >= cutoff:
        cause = "expiry_day_cutoff" if expiry_day and current >= cutoff else "outside_entry_window"
        return cutoff, cause
    return cutoff, None


def _positive(value) -> bool:
    return is_finite_decimal(value) and value > 0


def _pivot_match(close: Decimal, side: Side, pivots: PivotLevels, tolerance: Decimal) -> str | None:
    if side == "long":
        levels = (("P", pivots.pivot), ("R1", pivots.resistance_one), ("R2", pivots.resistance_two))
        direction = 1
    else:
        levels = (("P", pivots.pivot), ("S1", pivots.support_one), ("S2", pivots.support_two))
        direction = -1
    return next((label for label, level in levels
                 if _positive(level) and 0 <= direction * (close - level) <= level * tolerance), None)
