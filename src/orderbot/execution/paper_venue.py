from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist, to_storage
from orderbot.core.money import PAISA, is_finite_decimal
from orderbot.core.state_store import open_store
from orderbot.data.candle_builder import TradingSession
from orderbot.options.liquidity_gate import OptionQuote

from .lifecycle import LifecycleError, OrderIntent, OrderReport, OrderState


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    orders: tuple[OrderReport, ...]
    positions: dict[str, int]


class PaperVenue:
    """Local simulation with a durable book independent of the execution journal."""

    def __init__(self, path: Path, config: Config):
        if config.runtime.mode != "paper" or config.runtime.live_enabled:
            raise ValueError("The simulated venue requires paper mode.")
        self.path, self.config = path, config

    def snapshot(self) -> BookSnapshot:
        with open_store(self.path) as connection:
            connection.execute("BEGIN")
            orders = self._orders(connection)
            return BookSnapshot(orders, self._positions(orders))

    def order_book(self) -> tuple[OrderReport, ...]:
        with open_store(self.path) as connection:
            return self._orders(connection)

    def submit(self, intent: OrderIntent, quote: OptionQuote, at: datetime) -> OrderReport:
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            orders = self._orders(connection)
            if any(order.intent.tag == intent.tag for order in orders):
                raise LifecycleError("Paper venue received an already submitted tag.")
            error = None
            if intent.side == "SELL":
                held = self._positions(orders).get(intent.token, 0)
                reserved = sum(item.intent.quantity - item.filled_quantity for item in orders if (
                    item.intent.token == intent.token and item.intent.side == "SELL" and not item.state.terminal
                ))
                if intent.quantity > held - reserved:
                    error = "would_open_short_position"
            if error is None and self._should_reject(intent.tag):
                error = "paper_injected_rejection"
            if error is not None:
                report = OrderReport(intent, OrderState.REJECTED, reason=error)
            else:
                report = self._match(connection, OrderReport(intent, OrderState.ACKED), quote, at)
            self._save(connection, report)
            return report

    def match(self, tag: str, quote: OptionQuote, at: datetime) -> OrderReport:
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self._lookup(connection, tag)
            new = old if old.state.terminal else self._match(connection, old, quote, at)
            if new != old:
                self._save(connection, new)
            return new

    def cancel(self, tag: str, reason: str) -> OrderReport:
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = self._lookup(connection, tag)
            if old.state.terminal:
                return old
            current = replace(old, state=OrderState.CANCELLED, reason=reason)
            self._save(connection, current)
            return current

    def _should_reject(self, tag: str) -> bool:
        digest = sha256(f"{self.config.execution.paper_rejection_seed}:{tag}".encode()).digest()
        draw = Decimal(int.from_bytes(digest, "big")) / (1 << (8 * len(digest)))
        return draw < self.config.execution.paper_rejection_pct / 100

    def _match(self, connection, report: OrderReport, quote: OptionQuote, at: datetime) -> OrderReport:
        now, intent = as_ist(at), report.intent
        if not self.fresh_book(quote, intent.token, intent.price_tick, now, side=intent.side):
            return report
        buy = intent.side == "BUY"
        level = quote.asks[0] if buy else quote.bids[0]
        slippage = intent.price_tick * self.config.costs.slippage_ticks
        price = level.price + slippage if buy else level.price - slippage
        if price <= 0 or (buy and price > intent.limit) or (not buy and price < intent.limit):
            return report

        # Every child resting against the same displayed touch shares its visible quantity.
        key = (intent.token, intent.side, to_storage(quote.exchange_at), int(level.price / PAISA))
        consumed = connection.execute(
            "SELECT units FROM paper_depth_usage WHERE token=? AND side=? AND at_utc=? AND touch_paise=?", key,
        ).fetchone()
        remaining = max(0, level.quantity - (consumed[0] if consumed else 0))
        filled = min(intent.quantity - report.filled_quantity, remaining // intent.lot_size * intent.lot_size)
        if not filled:
            return report
        connection.execute("""
            INSERT INTO paper_depth_usage VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(token, side, at_utc, touch_paise) DO UPDATE SET units = units + excluded.units
        """, (*key, filled))
        cumulative = report.filled_quantity + filled
        measured = not report.filled_quantity or report.touch_value is not None
        return OrderReport(
            intent, OrderState.FILLED if cumulative == intent.quantity else OrderState.PARTIAL,
            cumulative, report.filled_value + price * filled, report.first_fill_at or now, now,
            touch_value=(report.touch_value or Decimal(0)) + level.price * filled if measured else None,
            modeled_slippage=(report.modeled_slippage or Decimal(0)) + slippage * filled if measured else None,
        )

    def fresh_book(self, quote: OptionQuote, token: str, tick: Decimal, at: datetime, *, side: str = "BUY") -> bool:
        at = as_ist(at)
        session = TradingSession.from_config(at.date(), self.config.market, segment="derivatives")
        if (
            not session.opens_at <= at < session.closes_at
            or quote.token != token or not quote.bids or (side == "BUY" and not quote.asks)
            or not quote.exchange_at <= quote.received_at <= at
            or quote.exchange_at.date() != at.date()
            or (at - quote.exchange_at).total_seconds() >= self.config.options.quote_max_age_seconds
        ):
            return False
        levels = quote.bids[:1] + quote.asks[:1]
        return all(
            is_finite_decimal(level.price) and level.price > 0 and level.price % tick == 0
            and type(level.quantity) is int and level.quantity > 0 for level in levels
        ) and (not quote.asks or quote.bids[0].price < quote.asks[0].price)

    @staticmethod
    def _positions(orders) -> dict[str, int]:
        result = defaultdict(int)
        for order in orders:
            direction = 1 if order.intent.side == "BUY" else -1
            result[order.intent.token] += direction * order.filled_quantity
        return {token: units for token, units in result.items() if units}

    @classmethod
    def _orders(cls, connection) -> tuple[OrderReport, ...]:
        return tuple(cls._decode(row) for row in connection.execute("SELECT * FROM paper_orders ORDER BY rowid"))

    @classmethod
    def _lookup(cls, connection, tag: str) -> OrderReport:
        row = connection.execute("SELECT * FROM paper_orders WHERE tag = ?", (tag,)).fetchone()
        if row is None:
            raise LifecycleError("Paper venue has no order for the tag.")
        return cls._decode(row)

    @staticmethod
    def _decode(row) -> OrderReport:
        intent = OrderIntent.from_dict(json.loads(row["intent_json"]))
        if intent.tag != row["tag"]:
            raise LifecycleError("Paper venue tag disagrees with its stored intent.")
        report = json.loads(row["report_json"])
        return OrderReport.from_detail(intent, report["state"], report["filled_quantity"], report)

    @staticmethod
    def _save(connection, report: OrderReport) -> None:
        values = report.detail() | {"state": report.state.value, "filled_quantity": report.filled_quantity}
        connection.execute("""
            INSERT INTO paper_orders VALUES (?, ?, ?) ON CONFLICT(tag) DO UPDATE SET report_json=excluded.report_json
        """, (report.intent.tag, json.dumps(report.intent.to_dict()), json.dumps(values)))
