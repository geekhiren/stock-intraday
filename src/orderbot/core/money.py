from decimal import ROUND_HALF_UP, Decimal
from typing import TypeGuard


PAISA = Decimal("0.01")


def is_finite_decimal(value: object) -> TypeGuard[Decimal]:
    return isinstance(value, Decimal) and value.is_finite()


def round_inr(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)
