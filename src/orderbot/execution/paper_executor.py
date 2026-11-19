from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from secrets import token_hex
import sqlite3
from threading import RLock
from time import sleep

from orderbot.broker.instruments import Instrument
from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist, now_ist, parse_timestamp
from orderbot.core.money import is_finite_decimal
from orderbot.options.freeze_quantity import FreezeError, split_quantity
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import StrikeDecision, revalidate_selection
from orderbot.risk.risk_manager import EntryPlan, RiskManager, RiskState

from .journal import Journal, JournalConflict, Ledger, Position
from .lifecycle import LifecycleError, OrderIntent, OrderState
from .paper_venue import PaperVenue
from .reconciler import Reconciliation, Reconciler


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str
    reason: str
    order_tags: tuple[str, ...] = ()


class PaperExecutor:
    def __init__(
        self, journal: Journal, venue: PaperVenue, config: Config, *,
        clock: Callable[[], datetime] = now_ist, sleep: Callable[[float], None] = sleep,
        tag_factory: Callable[[], str] = lambda: token_hex(8),
        alert: Callable[[dict], None] | None = None,
    ):
        if config.runtime.mode != "paper" or config.runtime.live_enabled or not isinstance(venue, PaperVenue):
            raise ValueError("Execution requires an in-process paper venue and paper mode.")
        if journal.path.resolve() == venue.path.resolve():
            raise ValueError("The order journal and paper venue need independent databases.")
        self.journal, self.venue, self.config = journal, venue, config
        self.risk = RiskManager(config.risk, config.costs)
        self.clock, self.sleep, self.tag_factory = clock, sleep, tag_factory
        self.alert = alert if alert is not None else lambda event: None
        self.reconciler = Reconciler(journal, venue, self.alert)
        self._last_reconciled: datetime | None = None
        self.last_reconciliation: Reconciliation | None = None
        self._ready = False
        self._lock = RLock()

    def _now(self) -> datetime:
        return as_ist(self.clock())

    def reconcile(self) -> Reconciliation:
        with self._lock:
            now = self._now()
            result = self.reconciler.reconcile(now, recover_unsent=self._last_reconciled is None)
            self._last_reconciled = now
            self.last_reconciliation = result
            self._ready = result.ok
            return result

    def positions(self) -> tuple[Position, ...]:
        return self.journal.snapshot().positions

    def _ensure_reconciled(self, at: datetime) -> bool:
        if (
            not self._ready or self._last_reconciled.date() != at.date()
            or (at - self._last_reconciled).total_seconds() >= self.config.execution.reconcile_interval_seconds
        ):
            return self.reconcile().ok
        return True

    @staticmethod
    def _risk_state(ledger: Ledger, at: datetime) -> RiskState:
        today = at.date()
        entered = {item.intent.decision_id for item in ledger.orders if (
            item.intent.side == "BUY" and item.filled_quantity and item.first_fill_at.date() == today
        )}
        closures = sorted(
            (trade for trade in ledger.trades if parse_timestamp(trade["closed_at"]).date() == today),
            key=lambda trade: (parse_timestamp(trade["closed_at"]), trade["trade_id"]),
        )
        pnl = [Decimal(trade["net_pnl_inr"]) for trade in closures]
        streak = 0
        for net in reversed(pnl):
            if net >= 0:
                break
            streak += 1
        return RiskState(
            today, len(entered), len(ledger.positions),
            sum((item.quantity * item.average_price for item in ledger.positions), Decimal(0)),
            sum(pnl, Decimal(0)), streak, pending_entry=any(not item.state.terminal for item in ledger.orders),
            session_halted=today in ledger.halts,
        )

    def enter(
        self, signal_id: str, strategy: str, selection: StrikeDecision, quote: OptionQuote, *,
        spot: Decimal, premium_stop: Decimal, target_mid: Decimal, target_bid: Decimal,
        decision_detail: dict | None = None,
    ) -> ExecutionResult:
        with self._lock:
            def skip(reason: str):
                self.journal.record_skip(signal_id, strategy, self._now(), reason, decision_detail)
                return ExecutionResult("skipped", reason)

            at = self._now()
            if not self._ensure_reconciled(at):
                return skip("reconciliation_required")
            if selection.outcome != "selected":
                return skip("no_selected_strike")
            current = revalidate_selection(selection, quote, at=at, spot=spot, config=self.config.options)
            if current.outcome != "revalidated":
                return skip("aborted_requote")
            candidate = current.candidates[0]
            ledger = self.journal.snapshot()
            risk_state = self._risk_state(ledger, at)
            plan = EntryPlan(candidate.contract, candidate.mid, quote.asks[0].price, premium_stop, target_mid, target_bid)
            decision = self.risk.assess_entry(plan, risk_state)
            if not decision.allowed:
                if decision.halt_session:
                    self.journal.halt(at, str(decision.reason))
                return skip(str(decision.reason))

            current = revalidate_selection(
                selection, quote, at=at, spot=spot, lots=decision.sizing.lots, config=self.config.options,
            )
            if current.outcome != "revalidated":
                return skip("aborted_requote")
            children = current.candidates[0].freeze.children
            plan = EntryPlan(plan.instrument, plan.entry_mid, plan.entry_ask, plan.premium_stop,
                             plan.target_mid, plan.target_bid, len(children), len(children))
            decision = self.risk.assess_entry(plan, risk_state)
            if not decision.allowed:
                return skip(str(decision.reason))
            tick = plan.instrument.price_tick
            cap = self.config.execution.max_entry_premium // tick * tick
            limit = min(decision.projected_buy, cap)
            if limit <= premium_stop:
                return skip("entry_price_cap")
            intents = self._intents(
                signal_id, strategy, plan.instrument, "BUY", children, limit, at, premium_stop, "entry",
            )
            return self._prepare_and_send(intents, ledger.revision, quote, decision_detail=decision_detail)

    def exit_position(
        self, decision_id: str, contract: Instrument, quote: OptionQuote, *,
        reason: str, quantity: int | None = None, limit_price: Decimal | None = None,
    ) -> ExecutionResult:
        with self._lock:
            at = self._now()
            if not self._ensure_reconciled(at):
                return ExecutionResult("skipped", "reconciliation_required")
            ledger = self.journal.snapshot()
            if not any(position.decision_id == decision_id for position in ledger.positions):
                return ExecutionResult("skipped", "position_not_found")
            pending_buys = [item for item in ledger.orders if (
                item.intent.decision_id == decision_id and item.intent.side == "BUY" and not item.state.terminal
            )]
            if pending_buys:
                if any(item.state not in {OrderState.ACKED, OrderState.PARTIAL} for item in pending_buys):
                    return ExecutionResult("skipped", "unresolved_entry")
                for order in pending_buys:
                    self.journal.sync((self.venue.cancel(order.intent.tag, "protective_exit"),), self._now())
                ledger = self.journal.snapshot()
            at = self._now()
            position = next(item for item in ledger.positions if item.decision_id == decision_id)
            if (contract.token, contract.symbol, contract.lot_size, contract.price_tick) != (
                position.token, position.symbol, position.lot_size, position.price_tick
            ):
                return ExecutionResult("skipped", "exit_contract_mismatch")
            reserved = sum(item.intent.quantity - item.filled_quantity for item in ledger.orders if (
                item.intent.decision_id == decision_id and item.intent.side == "SELL" and not item.state.terminal
            ))
            requested = position.quantity - reserved if quantity is None else quantity
            allowed = self.risk.assess_exit(
                contract.token, requested, held_token=position.token,
                held_quantity=position.quantity, pending_exit_quantity=reserved,
            )
            if not allowed.allowed:
                return ExecutionResult("skipped", str(allowed.reason))
            if not self.venue.fresh_book(quote, contract.token, contract.price_tick, at, side="SELL"):
                return ExecutionResult("skipped", "exit_quote_unavailable")
            limit = (quote.bids[0].price - self.config.costs.slippage_ticks * contract.price_tick
                     if limit_price is None else limit_price)
            if not is_finite_decimal(limit) or limit <= 0 or limit % contract.price_tick:
                return ExecutionResult("skipped", "exit_price_unavailable")
            try:
                children = split_quantity(requested, contract, self.config.options.quantity_freeze_units).children
            except FreezeError:
                return ExecutionResult("skipped", "invalid_exit_quantity")
            intents = self._intents(decision_id, position.strategy, contract, "SELL", children, limit, at, None, reason)
            return self._prepare_and_send(intents, ledger.revision, quote)

    def cancel_working_orders(self, side: str, *, decision_id: str | None = None) -> bool:
        """Cancel only orders whose venue identity and current exposure reconcile."""
        if side not in {"BUY", "SELL"}:
            raise ValueError("Cancellation requires a known side.")
        with self._lock:
            if not self.reconcile().ok:
                return False
            working = [item for item in self.journal.snapshot().orders if (
                not item.state.terminal and item.intent.side == side
                and (decision_id is None or item.intent.decision_id == decision_id)
            )]
            if any(item.state not in {OrderState.ACKED, OrderState.PARTIAL} for item in working):
                return False
            for item in working:
                report = self.venue.cancel(item.intent.tag, "protective_cancel" if side == "BUY" else "exit_reprice")
                self.journal.sync((report,), self._now())
                if not report.state.terminal:
                    return False
            return True

    def poll(self, quotes: Mapping[str, OptionQuote]) -> bool:
        """Advance resting fills, cancel expired remainders, and run due reconciliation."""
        with self._lock:
            at = self._now()
            if not self._ensure_reconciled(at):
                return False
            for report in self.journal.snapshot().orders:
                if report.state not in {OrderState.ACKED, OrderState.PARTIAL}:
                    continue
                intent = report.intent
                quote = quotes.get(intent.token)
                if self._expired(intent, at):
                    current = self.venue.cancel(intent.tag, "unfilled_remainder_timeout")
                elif quote is not None and quote.exchange_at > (report.last_fill_at or intent.at):
                    current = self.venue.match(intent.tag, quote, at)
                else:
                    continue
                self.journal.sync((current,), at)
            return True

    def _expired(self, intent: OrderIntent, at: datetime) -> bool:
        return (at - intent.at).total_seconds() >= self.config.execution.partial_timeout_seconds

    def _intents(self, decision_id, strategy, contract, side, children, limit, at, stop, reason):
        return tuple(
            OrderIntent(self.tag_factory(), decision_id, strategy, contract.token, contract.symbol,
                        side, quantity, contract.lot_size, contract.price_tick, limit, at, stop, reason)
            for quantity in children
        )

    def _prepare_and_send(self, intents, revision, quote, *, decision_detail=None) -> ExecutionResult:
        try:
            self.journal.prepare(intents, revision, decision_detail=decision_detail)
        except JournalConflict:
            return ExecutionResult("skipped", "journal_conflict")
        tags = tuple(order.tag for order in intents)
        for index, intent in enumerate(intents):
            at = self._now()
            if (
                at.date() != intent.at.date() or self._expired(intent, at)
                or not self.venue.fresh_book(quote, intent.token, intent.price_tick, at, side=intent.side)
            ):
                self.journal.sync((), at, cancel_unsent=tags[index:])
                return ExecutionResult("stopped", "quote_expired_before_send", tags)
            try:
                self.journal.claim(intent.tag, at)
            except JournalConflict:
                self.journal.sync((), self._now(), cancel_unsent=tags[index:])
                return ExecutionResult("stopped", "journal_conflict", tags)
            try:
                report = self.venue.submit(intent, quote, at)
            except TimeoutError:
                report = self._lookup_after_timeout(intent)
                if report is None:
                    self.journal.sync((), self._now(), cancel_unsent=tags[index + 1:])
                    return ExecutionResult("halted", "order_timeout_unresolved", tags)
            try:
                self.journal.sync((report,), self._now())
            except LifecycleError:
                self._ready = False
                self._halt("invalid_order_report", intent.tag)
                self.journal.sync((), self._now(), cancel_unsent=tags[index + 1:])
                return ExecutionResult("halted", "invalid_order_report", tags)
            if report.state is OrderState.REJECTED:
                self._halt("order_rejected", intent.tag)
                self.journal.sync((), self._now(), cancel_unsent=tags[index + 1:])
                return ExecutionResult("halted", "order_rejected", tags)
        return ExecutionResult("submitted", "accepted", tags)

    def _lookup_after_timeout(self, intent: OrderIntent):
        self.sleep(self.config.execution.timeout_lookup_delay_seconds)
        try:
            matches = [order for order in self.venue.order_book() if order.intent.tag == intent.tag]
        except (OSError, sqlite3.Error, LifecycleError, KeyError, ValueError):
            matches = []
        if len(matches) == 1 and matches[0].intent == intent:
            return matches[0]
        self._ready = False
        self._halt("order_timeout_unresolved", intent.tag)
        return None

    def _halt(self, reason: str, tag: str):
        now = self._now()
        self.journal.halt(now, reason)
        self.alert({"event": "execution_halted", "at": now.isoformat(), "reason": reason, "tag": tag})
