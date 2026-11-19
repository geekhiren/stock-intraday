from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from threading import RLock

from orderbot.broker.instruments import Instrument
from orderbot.core.ist_time import IST, as_ist
from orderbot.core.money import round_inr
from orderbot.options.freeze_quantity import FreezeError, split_quantity
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.risk.cost_model import round_trip_cost
from orderbot.risk.kill_switch import Health, KillSwitch
from orderbot.strategy.stop_plan import StopPlan

from .exit_rules import ExitFrame, evaluate_exit, square_off_due
from .exit_store import ExitProgress, ExitStore
from .journal import Ledger, Position
from .lifecycle import LifecycleError
from .paper_executor import PaperExecutor


class ExitEngine:
    """Drive an event-ordered paper exit ladder using the same execution journal as entries."""

    def __init__(self, executor: PaperExecutor, contracts: Mapping[str, Instrument], directory: Path):
        self.executor, self.contracts = executor, contracts
        self.config = executor.config
        self.store = ExitStore(executor.journal.path)
        self.kill_switch = KillSwitch(self.config, directory)
        self._lock = RLock()
        self._previous_at: datetime | None = None

    def step(self, frame: ExitFrame, quotes: Mapping[str, OptionQuote], health: Health = Health()) -> bool:
        """Return whether the independently reconciled paper account is confirmed flat."""
        with self._lock:
            return self._step(frame, quotes, health)

    def _step(self, frame: ExitFrame, quotes: Mapping[str, OptionQuote], health: Health) -> bool:
        now = frame.at
        if self._previous_at is not None and now < self._previous_at:
            raise ValueError("Exit events must arrive in timestamp order.")
        self._previous_at = now
        if frame.kind == "tick" and frame.underlying is not None:
            self.kill_switch.observe_underlying(now, frame.underlying)
        ledger = self.executor.journal.snapshot()
        opened = min((item.opened_at for item in ledger.positions), default=None)
        had_position = bool(ledger.positions)
        streak, funds = self.executor.journal.recent_order_health(now)
        ticks = [
            quote.exchange_at for item in ledger.positions if (
                (quote := quotes.get(item.token)) is not None and quote.token == item.token
                and quote.exchange_at <= quote.received_at <= now
            )
        ]
        if health.last_option_tick_at is not None:
            ticks.append(as_ist(health.last_option_tick_at))
        if frame.kind == "tick" and frame.option_ltp is not None and any(
            item.token == frame.option_token for item in ledger.positions
        ):
            ticks.append(now)
        observed = replace(health, rejections=max(health.rejections, streak),
                           insufficient_funds=health.insufficient_funds or funds,
                           last_option_tick_at=max(ticks, default=None))
        trigger = self.kill_switch.evaluate(now, observed, position_opened_at=opened)
        kill = self._latch(now, trigger)
        cfg = self.config.exits
        forced = square_off_due(now, cfg) or any(
            square_off_due(now, cfg, self._expiry_cutoff(position, now)) for position in ledger.positions
        )
        backstop = now.time() >= cfg.backstop_time
        if forced:
            self.executor.journal.halt(now, "forced_square_off")
            self.store.emit(now, "square_off_due", {}, once_key=f"{now.date()}:square_off")

        if observed.position_mismatch:
            self.executor.reconcile()
            if backstop:
                self._backstop_unconfirmed(now)
            return False
        if (kill or forced) and any(item.intent.side == "BUY" and not item.state.terminal for item in ledger.orders):
            if not self.executor.cancel_working_orders("BUY"):
                if backstop:
                    self._backstop_unconfirmed(now)
                return False
        if not self.executor.poll(quotes):
            check = self.executor.last_reconciliation
            reason = "position_mismatch" if check is not None and check.reason == "position_mismatch" else "reconciliation_required"
            self._latch(now, reason)
            if backstop:
                self._backstop_unconfirmed(now)
            return False

        ledger = self.executor.journal.snapshot()
        for position in ledger.positions:
            position_frame = frame if frame.option_token == position.token else replace(frame, option_ltp=None)
            self._manage(position, ledger, position_frame, quotes.get(position.token), kill, backstop)

        latest = self.executor.journal.snapshot()
        flat = not latest.positions and all(item.state.terminal for item in latest.orders)
        if (forced or kill or had_position) and flat:
            check = self.executor.reconcile()
            flat = check.ok and not check.positions
        if backstop and not flat:
            self._backstop_unconfirmed(now)
        elif backstop:
            self.store.emit(now, "backstop_confirmed_flat", {}, once_key=f"{now.date()}:backstop_flat")
        return flat

    def _latch(self, now: datetime, reason: str | None) -> str | None:
        kill, changed = self.store.latch(now, reason)
        if changed:
            self.executor.alert({"event": "kill_switch_fired", "at": now.isoformat(), "reason": kill})
        return kill

    def _expiry_cutoff(self, position: Position, at: datetime) -> datetime | None:
        contract = self.contracts.get(position.token)
        if contract is None or contract.expiry != at.date():
            return None
        close = datetime.combine(contract.expiry, self.config.options.expiry_close, tzinfo=IST)
        return close - timedelta(minutes=self.config.exits.expiry_force_exit_minutes)

    def _manage(
        self, position: Position, ledger: Ledger, frame: ExitFrame, quote: OptionQuote | None,
        kill: str | None, backstop: bool,
    ) -> None:
        now, cfg = frame.at, self.config.exits
        progress = self.store.progress(position.decision_id)
        state = progress
        if frame.kind == "tick" and frame.option_ltp is not None:
            state = replace(state, last_ltp=frame.option_ltp, last_tick_at=now)
        contract = self.contracts.get(position.token)
        if contract is None:
            self.executor.journal.halt(now, "missing_exit_contract")
            self._alert_once(now, "missing_exit_contract", position.decision_id, {})
            return

        expiry_cutoff = self._expiry_cutoff(position, now)
        if square_off_due(now, cfg, expiry_cutoff):
            trigger = "forced_square_off"
        elif kill is not None:
            trigger = "kill_switch"
        elif state.reason is not None:
            trigger = None
        else:
            try:
                detail = self.executor.journal.decision_detail(position.decision_id)
                plan = StopPlan.from_view(detail.get("execution_stop_plan") or detail["stop_plan"])
                plan = plan.with_entry_fill(position.average_price)
                break_even = self._break_even(position, ledger, contract, plan, state)
                trail, trigger = evaluate_exit(plan, position.opened_at, state.trail, frame, cfg, break_even=break_even)
                state = replace(state, trail=trail)
            except (KeyError, TypeError, ValueError, ArithmeticError, LifecycleError):
                self._latch(now, "missing_stop_plan")
                trigger = "kill_switch"
        if state.trail.stop is not None and state.trail.stop != progress.trail.stop:
            self.store.emit(now, "trailing_stop_raised", {
                "decision_id": position.decision_id, "stop": str(state.trail.stop), "peak": str(state.trail.peak),
            })
        if trigger is not None and (
            state.reason is None or trigger == "forced_square_off"
            or (trigger == "kill_switch" and state.reason != "forced_square_off")
        ) and trigger != state.reason:
            if trigger == "forced_square_off":
                state = replace(state, reason=trigger, normal_attempts=0, last_attempt=None)
            else:
                state = replace(state, reason=trigger)
            self.store.emit(now, "exit_triggered", {
                "decision_id": position.decision_id, "reason": trigger, "kill": kill,
            })
        if state != progress:
            self.store.save(position.decision_id, state)
        if state.reason is not None:
            self._ladder(position, contract, state, now, quote, backstop)

    def _break_even(self, position: Position, ledger: Ledger, contract: Instrument, plan: StopPlan,
                    progress: ExitProgress) -> Decimal | None:
        if progress.trail.stop is not None and progress.trail.stop > plan.premium_stop:
            return None
        peak = max(plan.option_entry, progress.trail.peak or plan.option_entry,
                   progress.last_ltp or plan.option_entry)
        if peak - plan.option_entry < self.config.exits.break_even_r * (plan.option_entry - plan.premium_stop):
            return None
        try:
            sell_orders = len(split_quantity(position.quantity, contract, self.config.options.quantity_freeze_units).children)
        except FreezeError:
            return None
        buy_orders = sum(
            item.intent.decision_id == position.decision_id and item.intent.side == "BUY" and item.filled_quantity > 0
            for item in ledger.orders
        )
        charges = round_trip_cost(plan.option_entry, peak, position.quantity, self.config.costs,
                                  buy_orders=buy_orders, sell_orders=sell_orders)["total"]
        return _ceil_tick(plan.option_entry + charges / position.quantity, contract.price_tick)

    def _ladder(self, position: Position, contract: Instrument, state: ExitProgress, now: datetime,
                quote: OptionQuote | None, backstop: bool) -> None:
        cfg, uid = self.config.exits, position.decision_id
        if state.protected_attempted:
            return
        if not backstop and state.last_attempt is not None and (
            now - state.last_attempt
        ).total_seconds() < cfg.reprice_seconds:
            return
        if not backstop and state.normal_attempts >= len(cfg.reprice_extra_ticks):
            self.executor.cancel_working_orders("SELL", decision_id=uid)
            self.executor.journal.halt(now, "exit_unfilled")
            self._alert_once(now, "exit_unfilled", uid, {"remaining_quantity": position.quantity})
            return
        if quote is None or not self.executor.venue.fresh_book(quote, position.token, contract.price_tick, now, side="SELL"):
            return

        if backstop:
            if state.last_tick_at is None or not 0 <= (now - state.last_tick_at).total_seconds() < (
                self.config.options.quote_max_age_seconds
            ):
                return
            limit = _ceil_tick(state.last_ltp * (1 - cfg.paper_protection_fraction), contract.price_tick)
            next_state = replace(state, protected_attempted=True, last_attempt=now)
            route = "protected"
        else:
            ticks = self.config.costs.slippage_ticks + cfg.reprice_extra_ticks[state.normal_attempts]
            limit = quote.bids[0].price - contract.price_tick * ticks
            next_state = replace(state, normal_attempts=state.normal_attempts + 1, last_attempt=now)
            route = "limit"
        if limit <= 0 or not self.executor.cancel_working_orders("SELL", decision_id=uid):
            return
        latest = next((item for item in self.executor.positions() if item.decision_id == uid), None)
        if latest is None:
            return
        self.store.save(uid, next_state)
        self.store.emit(now, "exit_attempt", {
            "decision_id": uid, "reason": state.reason, "route": route,
            "limit": str(round_inr(limit)), "quantity": latest.quantity,
        })
        result = self.executor.exit_position(uid, contract, quote, limit_price=limit, reason=state.reason)
        self.store.emit(now, "exit_attempt_result", {
            "decision_id": uid, "status": result.status, "reason": result.reason,
            "order_tags": list(result.order_tags),
        })

    def _backstop_unconfirmed(self, now: datetime) -> None:
        self.executor.journal.halt(now, "backstop_not_flat")
        self._alert_once(now, "backstop_not_flat", now.date().isoformat(), {})

    def _alert_once(self, now: datetime, event: str, correlation_id: str, fields: dict) -> None:
        fields = {"correlation_id": correlation_id, **fields}
        if self.store.emit(now, event, fields, once_key=f"{event}:{correlation_id}",
                           alert_correlation_id=correlation_id):
            self.executor.alert({"event": event, "at": now.isoformat(), **fields})


def _ceil_tick(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
