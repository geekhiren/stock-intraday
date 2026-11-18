from decimal import Decimal

from orderbot.core.config import CostConfig
from orderbot.core.money import is_finite_decimal, round_inr


def round_trip_cost(
    buy_price: Decimal,
    sell_price: Decimal,
    qty: int,
    rates: CostConfig,
    *,
    buy_orders: int = 1,
    sell_orders: int = 1,
) -> dict[str, Decimal]:
    """Calculate charges from average fill prices and executed order counts."""
    if not all(is_finite_decimal(price) and price > 0 for price in (buy_price, sell_price)):
        raise ValueError("Buy and sell fill prices must be positive finite Decimal values.")
    if type(qty) is not int or qty <= 0:
        raise ValueError("Filled quantity must be a positive integer.")
    if any(type(count) is not int or not 1 <= count <= qty for count in (buy_orders, sell_orders)):
        raise ValueError("Each side needs a positive executed-order count up to the filled quantity.")

    buy_turnover = buy_price * qty
    sell_turnover = sell_price * qty
    turnover = buy_turnover + sell_turnover
    brokerage = (buy_orders + sell_orders) * rates.brokerage_per_order_inr
    exchange = turnover * rates.exchange_including_ipft_rate
    sebi = turnover * rates.sebi_rate
    charges = {
        "brokerage": round_inr(brokerage),
        "stt": round_inr(sell_turnover * rates.stt_sell_rate),
        "exchange_including_ipft": round_inr(exchange),
        "sebi": round_inr(sebi),
        "stamp_duty": round_inr(buy_turnover * rates.stamp_buy_rate),
        "gst": round_inr((brokerage + exchange + sebi) * rates.gst_rate),
    }
    charges["total"] = sum(charges.values())
    return charges


def net_pnl(
    buy_price: Decimal,
    sell_price: Decimal,
    qty: int,
    rates: CostConfig,
    *,
    buy_orders: int = 1,
    sell_orders: int = 1,
) -> Decimal:
    charges = round_trip_cost(buy_price, sell_price, qty, rates, buy_orders=buy_orders, sell_orders=sell_orders)
    return round_inr((sell_price - buy_price) * qty - charges["total"])


def exercise_stt(intrinsic_value: Decimal, qty: int, rates: CostConfig) -> Decimal:
    """Calculate exercise STT on intrinsic value per unit for settlement reporting."""
    if not is_finite_decimal(intrinsic_value) or intrinsic_value < 0:
        raise ValueError("Intrinsic value must be a nonnegative finite Decimal.")
    if type(qty) is not int or qty <= 0:
        raise ValueError("Exercised quantity must be a positive integer.")
    return round_inr(intrinsic_value * qty * rates.stt_exercise_rate)
