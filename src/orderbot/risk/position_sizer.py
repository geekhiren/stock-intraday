from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from orderbot.core.money import is_finite_decimal


@dataclass(frozen=True, slots=True)
class PositionSize:
    lots: int
    lot_size: int
    risk_per_lot: Decimal
    risk_budget: Decimal | None
    reason: Literal["risk_unconfigured", "size_zero"] | None

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


def size_position(
    entry_price: Decimal,
    stop_price: Decimal,
    lot_size: int,
    *,
    capital_inr: Decimal | None,
    risk_per_trade_pct: Decimal | None,
) -> PositionSize:
    """Floor the cash risk budget to whole lots using the selected contract's lot size."""
    if not is_finite_decimal(entry_price) or not is_finite_decimal(stop_price) or not 0 < stop_price < entry_price:
        raise ValueError("Position sizing requires a positive premium stop below entry.")
    if type(lot_size) is not int or lot_size <= 0:
        raise ValueError("Position sizing requires a positive integer contract lot size.")
    if capital_inr is not None and (not is_finite_decimal(capital_inr) or capital_inr <= 0):
        raise ValueError("Capital must be a positive finite Decimal.")
    if risk_per_trade_pct is not None and (
        not is_finite_decimal(risk_per_trade_pct) or not 0 < risk_per_trade_pct <= 100
    ):
        raise ValueError("Per-trade risk must be a finite percentage greater than zero and at most 100.")

    risk_per_lot = (entry_price - stop_price) * lot_size
    if capital_inr is None or risk_per_trade_pct is None:
        return PositionSize(0, lot_size, risk_per_lot, None, "risk_unconfigured")
    budget = capital_inr * risk_per_trade_pct / 100
    lots = int(budget // risk_per_lot)
    return PositionSize(lots, lot_size, risk_per_lot, budget, None if lots else "size_zero")
