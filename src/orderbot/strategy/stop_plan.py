from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from orderbot.core.config import StrategyAConfig
from orderbot.core.money import is_finite_decimal


Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class StopPlan:
    side: Side
    underlying_entry: Decimal
    underlying_stop: Decimal
    underlying_target: Decimal
    atr: Decimal
    abs_delta: Decimal
    option_entry: Decimal
    max_premium_loss_fraction: Decimal

    @property
    def premium_floor(self) -> Decimal:
        return (1 - self.max_premium_loss_fraction) * self.option_entry

    @property
    def premium_stop(self) -> Decimal:
        distance = abs(self.underlying_entry - self.underlying_stop)
        return max(self.option_entry - self.abs_delta * distance, self.premium_floor)

    @property
    def premium_target(self) -> Decimal:
        distance = abs(self.underlying_target - self.underlying_entry)
        return self.option_entry + self.abs_delta * distance

    def with_entry_fill(self, price: Decimal) -> "StopPlan":
        if not is_finite_decimal(price) or price <= 0:
            raise ValueError("A confirmed premium fill must be positive and finite.")
        return replace(self, option_entry=price)

    def view(self) -> dict[str, str]:
        amounts = (
            "underlying_entry", "underlying_stop", "underlying_target", "atr", "abs_delta", "option_entry",
            "max_premium_loss_fraction", "premium_floor", "premium_stop", "premium_target",
        )
        return {"side": self.side, **{name: str(getattr(self, name)) for name in amounts}}


def build_stop_plan(
    side: Side, futures_entry: Decimal, atr: Decimal, delta: Decimal, option_entry: Decimal,
    config: StrategyAConfig,
) -> StopPlan:
    if side not in {"long", "short"} or not all(
        is_finite_decimal(value) and value > 0 for value in (futures_entry, atr, option_entry)
    ):
        raise ValueError("A stop plan requires direction and positive prices and ATR.")
    if not is_finite_decimal(delta) or not 0 < abs(delta) <= 1:
        raise ValueError("A stop plan requires a valid nonzero option delta.")
    direction = 1 if side == "long" else -1
    stop = futures_entry - direction * config.k_sl * atr
    target = futures_entry + direction * config.k_tgt * atr
    if min(stop, target) <= 0:
        raise ValueError("Underlying stop and target need positive prices.")
    return StopPlan(side, futures_entry, stop, target, atr, abs(delta), option_entry,
                    config.max_premium_loss_fraction)
