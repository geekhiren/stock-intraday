from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from re import fullmatch
from typing import Literal

from orderbot.core.ist_time import as_ist, parse_timestamp
from orderbot.core.money import is_finite_decimal


class LifecycleError(ValueError):
    """An order intent or event conflicts with the durable lifecycle."""


class OrderState(StrEnum):
    INTENT = "INTENT"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.FILLED, self.REJECTED, self.CANCELLED}


TRANSITIONS = {
    OrderState.INTENT: {OrderState.SENT, OrderState.CANCELLED},
    OrderState.SENT: {OrderState.ACKED, OrderState.REJECTED},
    OrderState.ACKED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED},
    OrderState.PARTIAL: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED},
}


@dataclass(frozen=True, slots=True)
class OrderIntent:
    tag: str
    decision_id: str
    strategy: Literal["vwap", "ohl"]
    token: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    lot_size: int
    price_tick: Decimal
    limit: Decimal
    at: datetime
    premium_stop: Decimal | None
    reason: str

    def __post_init__(self):
        object.__setattr__(self, "at", as_ist(self.at))
        if not fullmatch(r"[A-Za-z0-9]{1,19}", self.tag) or not self.decision_id or not self.token or not self.symbol:
            raise LifecycleError("Missing order identity or invalid order tag.")
        if self.strategy not in {"vwap", "ohl"} or self.side not in {"BUY", "SELL"}:
            raise LifecycleError("Order needs a known strategy and side.")
        if (
            type(self.lot_size) is not int or self.lot_size <= 0 or type(self.quantity) is not int
            or self.quantity <= 0 or self.quantity % self.lot_size
        ):
            raise LifecycleError("Order quantity must be positive and divisible by the contract lot size.")
        if (
            not is_finite_decimal(self.price_tick) or self.price_tick <= 0
            or not is_finite_decimal(self.limit) or self.limit <= 0 or self.limit % self.price_tick
        ):
            raise LifecycleError("A positive on-tick limit price is required.")
        if self.side == "BUY" and (
            not is_finite_decimal(self.premium_stop) or not 0 < self.premium_stop < self.limit
        ):
            raise LifecycleError("An entry order requires a positive premium stop below its limit.")

    def detail(self) -> dict:
        return {
            "strategy": self.strategy, "symbol": self.symbol, "lot_size": self.lot_size,
            "price_tick": str(self.price_tick), "limit": str(self.limit),
            "premium_stop": str(self.premium_stop) if self.premium_stop is not None else None,
            "reason": self.reason,
        }

    def to_dict(self) -> dict:
        return self.detail() | {
            "tag": self.tag, "decision_id": self.decision_id, "token": self.token,
            "side": self.side, "quantity": self.quantity, "at": self.at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OrderIntent":
        stop = data["premium_stop"]
        return cls(**(data | {
            "at": parse_timestamp(data["at"]), "price_tick": Decimal(data["price_tick"]),
            "limit": Decimal(data["limit"]), "premium_stop": Decimal(stop) if stop is not None else None,
        }))


@dataclass(frozen=True, slots=True)
class OrderReport:
    intent: OrderIntent
    state: OrderState
    filled_quantity: int = 0
    filled_value: Decimal = Decimal(0)
    first_fill_at: datetime | None = None
    last_fill_at: datetime | None = None
    reason: str | None = None
    touch_value: Decimal | None = None
    modeled_slippage: Decimal | None = None

    def __post_init__(self):
        quantity = self.filled_quantity
        if (
            type(quantity) is not int or not 0 <= quantity <= self.intent.quantity
            or quantity % self.intent.lot_size or not is_finite_decimal(self.filled_value)
        ):
            raise LifecycleError("Invalid cumulative order fill.")
        if quantity:
            if self.filled_value <= 0 or self.first_fill_at is None or self.last_fill_at is None:
                raise LifecycleError("A cumulative fill requires value and both fill timestamps.")
            first, last = as_ist(self.first_fill_at), as_ist(self.last_fill_at)
            if not self.intent.at <= first <= last:
                raise LifecycleError("Order fill timestamps are inconsistent.")
            object.__setattr__(self, "first_fill_at", first)
            object.__setattr__(self, "last_fill_at", last)
        elif self.filled_value or self.first_fill_at is not None or self.last_fill_at is not None:
            raise LifecycleError("An unfilled order cannot have fill timestamps or value.")
        if self.touch_value is not None or self.modeled_slippage is not None:
            if (
                not quantity or not is_finite_decimal(self.touch_value) or self.touch_value <= 0
                or not is_finite_decimal(self.modeled_slippage) or self.modeled_slippage < 0
            ):
                raise LifecycleError("Invalid cumulative slippage evidence.")
        if (
            (self.state is OrderState.FILLED) != (quantity == self.intent.quantity)
            or (self.state is OrderState.PARTIAL and not quantity)
            or (quantity and self.state not in {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED})
        ):
            raise LifecycleError("Order state and filled quantity are inconsistent.")

    @property
    def average_price(self) -> Decimal | None:
        return self.filled_value / self.filled_quantity if self.filled_quantity else None

    def detail(self) -> dict:
        detail = {
            "filled_value": str(self.filled_value),
            "first_fill_at": self.first_fill_at.isoformat() if self.first_fill_at is not None else None,
            "last_fill_at": self.last_fill_at.isoformat() if self.last_fill_at is not None else None,
            "reason": self.reason,
        }
        if self.touch_value is not None:
            detail.update(touch_value=str(self.touch_value), modeled_slippage=str(self.modeled_slippage))
        return detail

    @classmethod
    def from_detail(cls, intent, state, quantity, detail):
        first, last = detail["first_fill_at"], detail["last_fill_at"]
        return cls(
            intent, OrderState(state), quantity, Decimal(detail["filled_value"]),
            parse_timestamp(first) if first is not None else None,
            parse_timestamp(last) if last is not None else None, detail["reason"],
            Decimal(detail["touch_value"]) if "touch_value" in detail else None,
            Decimal(detail["modeled_slippage"]) if "modeled_slippage" in detail else None,
        )


def validate_transition(previous: OrderReport, current: OrderReport) -> None:
    if previous.intent != current.intent or current.state not in TRANSITIONS.get(previous.state, ()):
        raise LifecycleError(f"Invalid transition {previous.state} to {current.state}.")
    new_quantity = current.filled_quantity - previous.filled_quantity
    new_value = current.filled_value - previous.filled_value
    if new_quantity < 0 or (new_quantity == 0 and new_value):
        raise LifecycleError("Cumulative order fills regressed or changed without a new fill.")
    if previous.first_fill_at is not None and (
        previous.first_fill_at != current.first_fill_at or current.last_fill_at < previous.last_fill_at
        or (new_quantity == 0 and current.last_fill_at != previous.last_fill_at)
    ):
        raise LifecycleError("Previously observed fill timestamps changed.")
    if new_quantity:
        limit_value = new_quantity * current.intent.limit
        beyond_limit = new_value > limit_value if current.intent.side == "BUY" else new_value < limit_value
        if new_value <= 0 or beyond_limit:
            raise LifecycleError("New fill contradicts the order limit.")
    old_audit = (previous.touch_value, previous.modeled_slippage)
    new_audit = (current.touch_value, current.modeled_slippage)
    if new_quantity == 0 and old_audit != new_audit:
        raise LifecycleError("Slippage evidence changed without a new fill.")
    if previous.filled_quantity and new_quantity:
        if (previous.touch_value is None) != (current.touch_value is None) or (
            previous.touch_value is not None and (
                current.touch_value <= previous.touch_value or current.modeled_slippage < previous.modeled_slippage
            )
        ):
            raise LifecycleError("Cumulative slippage evidence regressed or changed coverage.")


def observed_transitions(previous: OrderReport, reported: OrderReport) -> tuple[OrderReport, ...]:
    if previous == reported:
        return ()
    if reported.state in {OrderState.INTENT, OrderState.SENT} or previous.intent != reported.intent:
        raise LifecycleError("Venue report has an invalid status or a different order identity.")
    if (
        previous.state is OrderState.SENT
        and reported.state not in {OrderState.ACKED, OrderState.REJECTED}
    ):
        acknowledged = OrderReport(previous.intent, OrderState.ACKED)
        validate_transition(previous, acknowledged)
        validate_transition(acknowledged, reported)
        return acknowledged, reported
    validate_transition(previous, reported)
    return (reported,)
