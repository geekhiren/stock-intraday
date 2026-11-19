from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from orderbot.broker.instruments import InstrumentMaster
from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import MINUTE, TradingSession
from orderbot.data.replay import AnalyzedBar
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import select_strike

from .base import GateResult, Signal, StrategyDecision
from .stop_plan import Side, StopPlan


def build_range_stop_plan(
    side: Side, entry: Decimal, opposite: Decimal, atr: Decimal, delta: Decimal, option_entry: Decimal, config: Config,
) -> StopPlan:
    prices = (entry, opposite, atr, option_entry)
    if side not in {"long", "short"} or not all(is_finite_decimal(value) and value > 0 for value in prices):
        raise ValueError("A range stop needs a direction and positive finite prices and ATR.")
    if not is_finite_decimal(delta) or not 0 < abs(delta) <= 1:
        raise ValueError("A range stop needs a valid nonzero delta.")
    direction = 1 if side == "long" else -1
    distance = min(direction * (entry - opposite), config.strategy_a.k_sl * atr)
    target = entry + direction * config.strategy_b.ohl_target_r * distance
    if distance <= 0 or target <= 0:
        raise ValueError("The opening-range stop and target are inconsistent with the breakout direction.")
    return StopPlan(side, entry, entry - direction * distance, target, atr, abs(delta), option_entry,
                    config.strategy_a.max_premium_loss_fraction)


class StrategyB:
    def __init__(self, config: Config, master: InstrumentMaster, trading_sessions: tuple[date, ...]):
        self.config, self.master, self.trading_sessions = config, master, trading_sessions

    def evaluate(
        self, row: AnalyzedBar, history: Sequence[AnalyzedBar], *, at: datetime, token: str,
        previous_close: Decimal | None, opening_spot: Decimal | None, spot: Decimal | None,
        quotes: Mapping[str, OptionQuote],
    ) -> StrategyDecision:
        now, minute = as_ist(at), as_ist(row.bar.at)
        uid = f"ohl-{token}-{minute:%Y%m%d%H%M}"
        gates = {key: GateResult("not_evaluated") for key in (
            "closed_candle", "opening_range", "gap", "breakout", "liquidity",
        )}

        def decision(reason, signal=None):
            return StrategyDecision(uid, minute, now, token, gates, reason, signal, strategy="ohl")

        def skip(gate, reason, **detail):
            gates[gate] = GateResult("fail", detail | {"reason": reason})
            return decision(reason)

        session = TradingSession.from_config(minute.date(), self.config.market)
        future = self.master.front_future(session.day)
        if row.source != "feed":
            return skip("closed_candle", "backfill_candle")
        if now < minute + MINUTE:
            return skip("closed_candle", "unclosed_candle")
        if (now.date() != session.day or not session.opens_at <= minute < session.closes_at or
            (now - minute - MINUTE).total_seconds() >= self.config.data.websocket_stale_seconds):
            return skip("closed_candle", "stale_candle")
        if token != future.token:
            return skip("closed_candle", "wrong_signal_instrument")
        gates["closed_candle"] = GateResult("pass")

        cfg = self.config.strategy_b
        freeze = session.opens_at + cfg.range_minutes * MINUTE
        opening = [item.bar for item in (*history, row) if session.opens_at <= item.bar.at < freeze]
        expected = min(cfg.range_minutes, (minute - session.opens_at) // MINUTE + 1)
        if len(opening) != expected or any(item.at != session.opens_at + index * MINUTE for index, item in enumerate(opening)):
            return skip("opening_range", "opening_range_incomplete")
        futures_open = opening[0].open
        high, low = max(item.high for item in opening), min(item.low for item in opening)
        tolerance = futures_open * cfg.ohl_buffer_pct
        long_tag, short_tag = futures_open - low <= tolerance, high - futures_open <= tolerance
        tags = [direction for direction, tagged in (("long", long_tag), ("short", short_tag)) if tagged]
        detail = {
            "high": str(high), "low": str(low), "futures_open": str(futures_open),
            "spot_open": str(opening_spot) if opening_spot is not None else None, "sample_minutes": expected,
            "tag": "/".join(tags) or None, "frozen_at": freeze.isoformat() if now >= freeze else None,
        }
        if now < freeze:
            gates["opening_range"] = GateResult("not_evaluated", detail)
            return decision("opening_range_building")
        if expected != cfg.range_minutes:
            return skip("opening_range", "opening_range_incomplete", **detail)
        gates["opening_range"] = GateResult("pass", detail)
        if minute < freeze:
            return decision("range_candle")
        if now.time() >= cfg.entry_window_end:
            return decision("outside_ohl_window")
        if not tags or high == low:
            return skip("opening_range", "ohl_tag_unavailable", **detail)

        if cfg.require_gap_agreement:
            if not is_finite_decimal(previous_close) or previous_close <= 0:
                return skip("gap", "previous_close_unavailable")
            gap = futures_open - previous_close
            agreed = "long" if gap > 0 else "short" if gap < 0 else None
            if agreed not in tags:
                return skip("gap", "gap_disagrees", previous_close=str(previous_close), gap=str(gap))
            side = agreed
            gates["gap"] = GateResult("pass", {"previous_close": str(previous_close), "gap": str(gap), "side": side})
        else:
            if len(tags) != 1:
                return skip("opening_range", "ohl_tag_ambiguous", **detail)
            side = tags[0]
            gates["gap"] = GateResult("disabled")

        if len(history) != (minute - session.opens_at) // MINUTE or any(
            item.bar.at != session.opens_at + index * MINUTE for index, item in enumerate(history)
        ):
            return skip("breakout", "incomplete_session_history")
        upward = side == "long"
        edge, direction = (high, 1) if upward else (low, -1)
        tick = future.price_tick
        threshold = edge + direction * (tick + edge * cfg.breakout_buffer_pct)
        rounding = ROUND_CEILING if upward else ROUND_FLOOR
        level = (threshold / tick).to_integral_value(rounding=rounding) * tick
        breakout = {"side": side, "close": str(row.bar.close), "level": str(level), "tick": str(tick)}
        if direction * (row.bar.close - level) < 0:
            return skip("breakout", "no_range_breakout", **breakout)
        if any(direction * (item.bar.close - level) >= 0 for item in history[cfg.range_minutes:]):
            return skip("breakout", "ohl_breakout_already_seen", **breakout)
        gates["breakout"] = GateResult("pass", breakout)

        if not is_finite_decimal(spot) or spot <= 0:
            return skip("liquidity", "skipped_illiquid", cause="spot_unavailable")
        right = "CE" if upward else "PE"
        selection = select_strike(self.master, self.trading_sessions, quotes, at=now, spot=spot, right=right,
                                  lots=1, config=self.config.options)
        if selection.outcome != "selected":
            return skip("liquidity", "skipped_illiquid", cause=selection.outcome, expiry_reason=selection.expiry.reason,
                        candidates={str(item.strike): list(item.reasons) for item in selection.candidates})
        picked = next(item for item in selection.candidates if item.contract and item.contract.token == selection.selected_token)
        bid, ask = picked.quote.bids[0].price, picked.quote.asks[0].price
        gates["liquidity"] = GateResult("pass", {
            "token": picked.contract.token, "symbol": picked.contract.symbol, "bid": str(bid), "ask": str(ask),
            "spot": str(spot), "delta": str(picked.greeks.delta), "iv": str(picked.greeks.iv),
            "strike": str(picked.strike), "expiry": picked.contract.expiry.isoformat(),
        })
        if not is_finite_decimal(row.indicators.atr) or row.indicators.atr <= 0:
            return decision("atr_unavailable")
        try:
            plan = build_range_stop_plan(side, row.bar.close, low if upward else high, row.indicators.atr,
                                         picked.greeks.delta, ask + picked.contract.price_tick * self.config.costs.slippage_ticks,
                                         self.config)
        except ValueError:
            return decision("stop_plan_unavailable")
        return decision("signal", Signal(side, right, selection, plan, plan.premium_target - picked.spread / 2))
