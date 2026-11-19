from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal

from orderbot.core.ist_time import as_ist, parse_timestamp
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import MINUTE
from orderbot.data.replay import AnalyzedBar
from orderbot.execution.paper_executor import ExecutionResult, PaperExecutor
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import revalidate_selection

from .base import StrategyDecision
from .opening_range import StrategyB
from .precedence import ohl_filled_on
from .vwap_rsi import StrategyA, entry_window


class SessionStrategies:
    def __init__(self, executor: PaperExecutor, vwap: StrategyA, ohl: StrategyB):
        self.executor, self.vwap, self.ohl = executor, vwap, ohl
        self.history: list[AnalyzedBar] = []
        self._ohl_fill_day: date | None = None

    def on_closed_candle(
        self, row: AnalyzedBar, *, at: datetime, token: str, spot: Decimal | None,
        quotes: Mapping[str, OptionQuote], previous_close: Decimal | None, opening_spot: Decimal | None,
    ) -> tuple[StrategyDecision, ExecutionResult]:
        now = as_ist(at)
        cfg = self.executor.config
        if cfg.runtime.strategy == "vwap":
            use_ohl, blocked = False, False
        else:
            # The final range candle can arrive after the freeze time, but the entry cutoff is exclusive.
            use_ohl = now.time() < cfg.strategy_b.entry_window_end
            if self._ohl_fill_day != now.date() and ohl_filled_on(self.executor.journal.snapshot().orders, now):
                self._ohl_fill_day = now.date()
            blocked = self._ohl_fill_day == now.date()
        if blocked:
            strategy = "ohl" if use_ohl else "vwap"
            reason = "ohl_trade_limit" if use_ohl else "vwap_suppressed_ohl_traded"
            decision = StrategyDecision(f"{strategy}-{token}-{row.bar.at:%Y%m%d%H%M}", row.bar.at, now,
                                        token, {}, reason, strategy=strategy)
        elif use_ohl:
            decision = self.ohl.evaluate(row, self.history, at=now, token=token, spot=spot, quotes=quotes,
                                         previous_close=previous_close, opening_spot=opening_spot)
        else:
            decision = self.vwap.evaluate(row, self.history, at=now, token=token, spot=spot, quotes=quotes)
        self.history.append(row)
        return decision, submit_strategy_decision(decision, self.executor, spot=spot, quotes=quotes)


def submit_strategy_decision(
    decision: StrategyDecision, executor: PaperExecutor, *, spot: Decimal | None, quotes: Mapping[str, OptionQuote],
) -> ExecutionResult:
    """Persist the gate audit and route eligible signals through the paper risk checks."""
    detail = decision.view()
    now = as_ist(executor.clock())
    signal = decision.signal
    strategy = decision.strategy
    if signal is None:
        signal_id = decision.id
        closed = decision.gates.get("closed_candle")
        if closed is not None and closed.status == "fail":
            signal_id += f":{decision.reason}:{decision.at.isoformat()}"
        executor.journal.record_skip(signal_id, strategy, decision.at, decision.reason, detail)
        return ExecutionResult("skipped", decision.reason)

    def skip(reason: str) -> ExecutionResult:
        executor.journal.record_skip(decision.id, strategy, now, reason, detail)
        return ExecutionResult("skipped", reason)

    cfg = executor.config
    if strategy == "vwap":
        _, cause = entry_window(now, decision.gates["time_window"].detail["expiry_day"], cfg.strategy_a)
        if cause is not None:
            return skip(cause)
    elif now.time() >= cfg.strategy_b.entry_window_end:
        return skip("outside_ohl_window")
    if now < decision.at or now.date() != decision.at.date() or (
        now - decision.candle_at - MINUTE
    ).total_seconds() >= cfg.data.websocket_stale_seconds:
        return skip("stale_candle")
    exits = (parse_timestamp(trade["closed_at"]) for trade in executor.journal.snapshot().trades)
    recent_exit = max((exit_at for exit_at in exits if exit_at.date() == now.date()), default=None)
    if recent_exit is not None and now < recent_exit + timedelta(minutes=cfg.strategy_a.cooldown_minutes):
        return skip("cooldown")

    quote = quotes.get(signal.selection.selected_token)
    if not is_finite_decimal(spot) or spot <= 0 or quote is None:
        return skip("aborted_requote")
    latest = revalidate_selection(signal.selection, quote, at=now, spot=spot, config=cfg.options)
    if latest.outcome != "revalidated":
        detail["requote_reasons"] = list(latest.reasons)
        return skip("aborted_requote")
    candidate = latest.candidates[0]
    entry = quote.asks[0].price + candidate.contract.price_tick * cfg.costs.slippage_ticks
    plan = signal.stop_plan.with_entry_fill(entry)
    detail["execution_stop_plan"] = plan.view()
    return executor.enter(
        decision.id, strategy, signal.selection, quote, spot=spot, premium_stop=plan.premium_stop,
        target_mid=plan.premium_target, target_bid=plan.premium_target - candidate.spread / 2, decision_detail=detail,
    )
