from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from logging import Logger
from pathlib import Path
import json

from orderbot.core.config import CostConfig
from orderbot.core.ist_time import as_ist, from_storage, parse_timestamp, to_storage
from orderbot.core.money import PAISA, round_inr
from orderbot.core.state_store import open_store
from orderbot.risk.cost_model import round_trip_cost

from .lifecycle import LifecycleError, OrderIntent, OrderReport, OrderState, observed_transitions, validate_transition


class JournalConflict(LifecycleError):
    """The journal changed before a prepared operation acquired its write lock."""


@dataclass(frozen=True, slots=True)
class Position:
    decision_id: str
    token: str
    symbol: str
    strategy: str
    lot_size: int
    price_tick: Decimal
    quantity: int
    average_price: Decimal
    opened_at: datetime
    premium_stop: Decimal

    @property
    def premium_risk_at_stop(self) -> Decimal:
        return (max(Decimal(0), self.average_price - self.premium_stop) * self.quantity).normalize()

    def view(self) -> dict:
        return {
            "decision_id": self.decision_id, "token": self.token, "symbol": self.symbol,
            "quantity": self.quantity, "average_price": str(self.average_price),
            "opened_at": self.opened_at.isoformat(), "premium_stop": str(self.premium_stop),
            "premium_risk_at_stop": str(round_inr(self.premium_risk_at_stop)),
        }


@dataclass(frozen=True, slots=True)
class Ledger:
    revision: int
    orders: tuple[OrderReport, ...]
    positions: tuple[Position, ...]
    trades: tuple[dict, ...]
    halts: dict[date, str]


def lots_by_token(positions: Iterable[Position]) -> dict[str, int]:
    quantities: dict[str, int] = defaultdict(int)
    for position in positions:
        quantities[position.token] += position.quantity
    return dict(quantities)


def project_orders(
    orders: Iterable[OrderReport], costs: CostConfig | None = None, booked: frozenset[str] = frozenset(),
) -> tuple[tuple[Position, ...], list[dict]]:
    grouped = defaultdict(list)
    for report in orders:
        grouped[report.intent.decision_id].append(report)
    positions, closed = [], []
    for group_id, reports in grouped.items():
        buys = [item for item in reports if item.intent.side == "BUY" and item.filled_quantity]
        sells = [item for item in reports if item.intent.side == "SELL" and item.filled_quantity]
        bought = sum(item.filled_quantity for item in buys)
        sold = sum(item.filled_quantity for item in sells)
        if sold > bought:
            raise LifecycleError("Journal would produce a short position.")
        if not bought:
            continue
        first = buys[0].intent
        identity = (first.token, first.symbol, first.lot_size, first.price_tick)
        if any((item.intent.token, item.intent.symbol, item.intent.lot_size, item.intent.price_tick) != identity
               for item in reports):
            raise LifecycleError("Position orders disagree on contract identity.")
        opened = min(item.first_fill_at for item in buys)
        buy_average = sum(item.filled_value for item in buys) / bought
        if sells and max(item.last_fill_at for item in buys) > min(item.first_fill_at for item in sells):
            raise LifecycleError("A position received new entry fills after exits began.")
        if bought > sold:
            positions.append(Position(
                group_id, first.token, first.symbol, first.strategy, first.lot_size, first.price_tick,
                bought - sold, buy_average, opened, first.premium_stop,
            ))
        elif costs is not None and group_id not in booked and all(item.state.terminal for item in reports):
            ended = max(item.last_fill_at for item in sells)
            sell_average = sum(item.filled_value for item in sells) / sold
            charges = round_trip_cost(buy_average, sell_average, sold, costs,
                                      buy_orders=len(buys), sell_orders=len(sells))
            value_moved = sum(item.filled_value for item in sells) - sum(item.filled_value for item in buys)
            net = round_inr(value_moved - charges["total"])
            closed.append({
                "trade_id": group_id, "symbol": first.symbol, "quantity": sold,
                "opened_at": opened.isoformat(), "closed_at": ended.isoformat(),
                "buy_average": str(buy_average), "sell_average": str(sell_average),
                "charges": {name: str(value) for name, value in charges.items()}, "net_pnl_inr": str(net),
                "buy_orders": len(buys), "sell_orders": len(sells),
            })
    return tuple(positions), closed


class Journal:
    def __init__(self, path: Path, costs: CostConfig, audit: Logger | None = None):
        self.path, self.costs, self.audit = path, costs, audit

    def snapshot(self) -> Ledger:
        with open_store(self.path) as connection:
            connection.execute("BEGIN")
            return self._read(connection)

    def _read(self, connection) -> Ledger:
        rows = connection.execute("""
            SELECT orders.*, events.event_id, events.state, events.filled_quantity,
                   events.detail_json AS report_json
            FROM orders LEFT JOIN order_events AS events ON events.event_id = (
                SELECT MAX(event_id) FROM order_events WHERE order_tag = orders.order_tag
            )
            ORDER BY orders.rowid
        """).fetchall()
        orders = []
        try:
            for row in rows:
                intent = OrderIntent.from_dict(json.loads(row["detail_json"]) | {
                    "tag": row["order_tag"], "decision_id": row["decision_id"], "token": row["instrument_token"],
                    "side": row["side"], "quantity": row["requested_quantity"],
                    "at": from_storage(row["intent_at_utc"]).isoformat(),
                })
                orders.append(OrderReport.from_detail(
                    intent, row["state"], row["filled_quantity"], json.loads(row["report_json"]),
                ))
            positions, _ = project_orders(orders)
            trades = tuple(json.loads(row[0]) for row in connection.execute(
                "SELECT detail_json FROM trades ORDER BY rowid",
            ))
            halts = {date.fromisoformat(row[0]): row[1] for row in connection.execute(
                "SELECT session_day, reason FROM execution_halts",
            )}
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise LifecycleError("Cannot reconstruct execution state from the SQLite journal.") from exc
        return Ledger(max((row["event_id"] for row in rows), default=0), tuple(orders), positions, trades, halts)

    def record_skip(self, signal_id: str, strategy: str, at: datetime, reason: str, detail: dict | None = None) -> None:
        with open_store(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                (signal_id, to_storage(at), strategy, "skipped", reason, json.dumps(detail or {})),
            )

    def prepare(self, intents: tuple[OrderIntent, ...], revision: int, *, decision_detail: dict | None = None) -> None:
        if not intents:
            raise LifecycleError("No orders to journal.")
        first = intents[0]
        if any((order.decision_id, order.side, order.strategy) != (first.decision_id, first.side, first.strategy)
               for order in intents):
            raise LifecycleError("Each batch must belong to one decision and side.")
        events = tuple(OrderReport(intent, OrderState.INTENT) for intent in intents)
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ledger = self._read(connection)
            if ledger.revision != revision:
                raise JournalConflict("Ledger changed before orders could be reserved.")
            if first.side == "BUY":
                if first.at.date() in ledger.halts:
                    raise JournalConflict("Current session has a durable execution halt.")
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                    (first.decision_id, to_storage(first.at), first.strategy, "orders_prepared", first.reason,
                     json.dumps(decision_detail or {})),
                ).rowcount
                if not inserted:
                    raise JournalConflict("Decision ID has already been recorded.")
            for intent, event in zip(intents, events):
                connection.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (intent.tag, intent.decision_id, to_storage(intent.at), intent.token,
                     intent.side, intent.quantity, json.dumps(intent.detail())),
                )
                self._write_event(connection, event, intent.at)
        for event in events:
            self._audit(event, event.intent.at)

    def claim(self, tag: str, at: datetime) -> OrderReport:
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ledger = self._read(connection)
            previous = next(item for item in ledger.orders if item.intent.tag == tag)
            if previous.state is not OrderState.INTENT or (
                previous.intent.side == "BUY" and as_ist(at).date() in ledger.halts
            ):
                raise JournalConflict("Only an unclaimed, permitted intent can be sent.")
            event = OrderReport(previous.intent, OrderState.SENT)
            validate_transition(previous, event)
            self._write_event(connection, event, at)
        self._audit(event, at)
        return event

    def sync(
        self, reports: Iterable[OrderReport], at: datetime, *, revision: int | None = None,
        cancel_unsent: Iterable[str] = (),
    ) -> None:
        events = []
        now = as_ist(at)
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            ledger = self._read(connection)
            if revision is not None and ledger.revision != revision:
                raise JournalConflict("Ledger changed before reconciliation could be committed.")
            current = {item.intent.tag: item for item in ledger.orders}
            for item in reports:
                previous = current.get(item.intent.tag)
                if previous is None:
                    raise LifecycleError("Unrecognized venue order.")
                if item.last_fill_at is not None and item.last_fill_at > now:
                    raise LifecycleError("Venue report contains a future fill timestamp.")
                events.extend(observed_transitions(previous, item))
                current[item.intent.tag] = item
            for tag in cancel_unsent:
                previous = current[tag]
                if previous.state is not OrderState.INTENT:
                    continue
                cancelled = OrderReport(previous.intent, OrderState.CANCELLED, reason="unsent_intent")
                validate_transition(previous, cancelled)
                events.append(cancelled)
                current[tag] = cancelled
            booked = frozenset(row[0] for row in connection.execute("SELECT trade_id FROM trades"))
            _, trades = project_orders(current.values(), self.costs, booked)
            for event in events:
                self._write_event(connection, event, at)
            for trade in trades:
                connection.execute(
                    "INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (trade["trade_id"], trade["trade_id"], to_storage(parse_timestamp(trade["opened_at"])),
                     to_storage(parse_timestamp(trade["closed_at"])), trade["quantity"],
                     int(Decimal(trade["net_pnl_inr"]) / PAISA), json.dumps(trade)),
                )
        for event in events:
            self._audit(event, at)

    def halt(self, at: datetime, reason: str) -> bool:
        with open_store(self.path) as connection:
            return bool(connection.execute(
                "INSERT OR IGNORE INTO execution_halts VALUES (?, ?, ?)",
                (as_ist(at).date().isoformat(), to_storage(at), reason),
            ).rowcount)

    def history(self, tag: str) -> list[dict]:
        with open_store(self.path) as connection:
            return [
                {"state": row["state"], "at": from_storage(row["at_utc"]).isoformat(),
                 "filled_quantity": row["filled_quantity"], **json.loads(row["detail_json"])}
                for row in connection.execute("SELECT * FROM order_events WHERE order_tag = ? ORDER BY event_id", (tag,))
            ]

    @staticmethod
    def _write_event(connection, event: OrderReport, at: datetime):
        connection.execute(
            "INSERT INTO order_events(order_tag, at_utc, state, filled_quantity, detail_json) VALUES (?, ?, ?, ?, ?)",
            (event.intent.tag, to_storage(at), event.state.value, event.filled_quantity, json.dumps(event.detail())),
        )

    def _audit(self, event: OrderReport, at: datetime) -> None:
        if self.audit is not None:
            self.audit.info("order_transition", extra={"correlation_id": event.intent.decision_id, "fields": {
                "order_tag": event.intent.tag, "strategy": event.intent.strategy, "state": event.state.value,
                "event_at_ist": as_ist(at).isoformat(), "filled_quantity": event.filled_quantity, **event.detail(),
            }})
