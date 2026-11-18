from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import erfc, exp, isfinite, log, sqrt

from orderbot.broker.instruments import Instrument
from orderbot.core.config import OptionsConfig
from orderbot.core.ist_time import IST, as_ist
from orderbot.core.money import is_finite_decimal


SECONDS_IN_ACT_365F_YEAR = 365 * 24 * 60 * 60


class GreeksError(ValueError):
    """An option quote could not be inverted with the configured Black-76 model."""


@dataclass(frozen=True, slots=True)
class Greeks:
    iv: Decimal
    delta: Decimal
    forward: Decimal


def _cdf(value: float) -> float:
    return erfc(-value / sqrt(2)) / 2


def implied_greeks(
    spot: Decimal, contract: Instrument, mid: Decimal, at: datetime, config: OptionsConfig,
) -> Greeks:
    if (
        contract.right not in {"CE", "PE"} or contract.expiry is None
        or not all(is_finite_decimal(value) and value > 0 for value in (spot, contract.strike, mid))
    ):
        raise GreeksError("Black-76 needs a dated call or put and positive spot, strike and premium.")
    expiry = datetime.combine(contract.expiry, config.expiry_close, tzinfo=IST)
    years = (expiry - as_ist(at)).total_seconds() / SECONDS_IN_ACT_365F_YEAR
    if years <= 0:
        raise GreeksError("There is no time left to invert the option premium.")

    rate, dividend = float(config.annual_rate), float(config.dividend_yield)
    try:
        forward, discount = float(spot) * exp((rate - dividend) * years), exp(-rate * years)
        strike, target = float(contract.strike), float(mid)
    except OverflowError:
        raise GreeksError("Black-76 inputs exceed the numeric range.") from None
    if not all(isfinite(value) and value > 0 for value in (forward, discount, strike, target)):
        raise GreeksError("Black-76 inputs exceed the numeric range.")

    sign = 1 if contract.right == "CE" else -1
    intrinsic = discount * max(0, sign * (forward - strike))
    ceiling = discount * (forward if sign > 0 else strike)
    if not intrinsic < target < ceiling:
        raise GreeksError("Premium is outside the Black-76 no-arbitrage bounds.")
    log_moneyness = log(forward) - log(strike)
    root_time = sqrt(years)

    def price_and_delta(volatility: float) -> tuple[float, float]:
        std_dev = volatility * root_time
        d1 = log_moneyness / std_dev + std_dev / 2
        d2 = d1 - std_dev
        probability = _cdf(sign * d1)
        price = discount * sign * (forward * probability - strike * _cdf(sign * d2))
        return price, discount * sign * probability

    low, high = 0.0, float(config.maximum_iv)
    tolerance = float(config.iv_price_tolerance)
    if not isfinite(high) or high * root_time <= 0:
        raise GreeksError("Configured maximum volatility is outside the numeric range.")
    if price_and_delta(high)[0] < target:
        raise GreeksError("Implied volatility exceeds the configured search range.")
    for _ in range(config.iv_max_iterations):
        guess = (low + high) / 2
        if guess == 0:
            break
        value, delta = price_and_delta(guess)
        if abs(value - target) <= tolerance:
            return Greeks(Decimal(str(guess)), Decimal(str(delta)), Decimal(str(forward)))
        if value < target:
            low = guess
        else:
            high = guess
    raise GreeksError("Black-76 implied volatility did not converge.")
