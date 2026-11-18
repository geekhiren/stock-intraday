from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

from orderbot.core.ist_time import as_ist, parse_timestamp
from orderbot.core.money import is_finite_decimal
from orderbot.data.candle_builder import MINUTE
from orderbot.execution.paper_executor import ExecutionResult, PaperExecutor
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import revalidate_selection

from .vwap_rsi import StrategyDecision, entry_window


def submit_strategy_decision(
    decision: StrategyDecision, executor: PaperExecutor, *, spot: Decimal | None, quotes: Mapping[str, OptionQuote],
) -> ExecutionResult:
    """Persist the gate audit and route eligible signals through the paper risk checks."""
    detail = decision.view()
    now = as_ist(executor.clock())
    signal = decision.signal
    if signal is None:
        signal_id = decision.id
        if decision.gates["closed_candle"].status == "fail":
            signal_id += f":{decision.reason}:{decision.at.isoformat()}"
        executor.journal.record_skip(signal_id, "vwap", decision.at, decision.reason, detail)
        return ExecutionResult("skipped", decision.reason)

    def skip(reason: str) -> ExecutionResult:
        executor.journal.record_skip(decision.id, "vwap", now, reason, detail)
        return ExecutionResult("skipped", reason)

    cfg = executor.config
    _, cause = entry_window(now, decision.gates["time_window"].detail["expiry_day"], cfg.strategy_a)
    if cause is not None:
        return skip(cause)
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
        decision.id, "vwap", signal.selection, quote, spot=spot, premium_stop=plan.premium_stop,
        target_mid=plan.premium_target, target_bid=plan.premium_target - candidate.spread / 2, decision_detail=detail,
    )
