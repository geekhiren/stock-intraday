from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from orderbot.broker.instruments import Instrument, NSE_DERIVATIVES
from orderbot.broker.ws_client import Level, MarketTick
from orderbot.core.config import MarketConfig, OptionsConfig
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal

from .freeze_quantity import FreezeError, FreezePlan, split_quantity
from .greeks import Greeks, GreeksError, implied_greeks


@dataclass(frozen=True, slots=True)
class OptionQuote:
    token: str
    exchange_at: datetime
    received_at: datetime
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    open_interest: int
    day_volume: int
    broker_delta: Decimal | None = None

    def __post_init__(self):
        object.__setattr__(self, "exchange_at", as_ist(self.exchange_at))
        object.__setattr__(self, "received_at", as_ist(self.received_at))

    @classmethod
    def from_tick(cls, tick: MarketTick, received_at: datetime, *, broker_delta: Decimal | None = None):
        if tick.exchange_type != NSE_DERIVATIVES:
            raise ValueError("An option quote requires an NSE derivatives snapshot.")
        return cls(tick.token, tick.exchange_at, received_at, tick.bids, tick.asks,
                   tick.open_interest, tick.volume, broker_delta)


@dataclass(frozen=True, slots=True)
class CandidateAudit:
    strike: Decimal
    contract: Instrument | None
    quote: OptionQuote | None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    quantity: int | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    spread_ticks: Decimal | None = None
    spread_fraction: Decimal | None = None
    bid_depth: int | None = None
    ask_depth: int | None = None
    required_depth: int | None = None
    greeks: Greeks | None = None
    freeze: FreezePlan | None = None


def _valid_levels(levels: tuple[Level, ...], tick: Decimal, *, descending: bool) -> bool:
    if not levels or any(
        not is_finite_decimal(level.price) or level.price <= 0 or level.price % tick
        or type(level.quantity) is not int or level.quantity <= 0
        or type(level.orders) is not int or level.orders < 0
        for level in levels
    ):
        return False
    prices = [level.price for level in levels]
    return prices == sorted(set(prices), reverse=descending)


def evaluate_candidate(
    contract: Instrument, quote: OptionQuote | None, *, at: datetime, spot: Decimal,
    atm: Decimal, lots: int, config: OptionsConfig, market: MarketConfig | None = None,
) -> CandidateAudit:
    strike = contract.strike
    if (
        contract.exchange != "NFO" or contract.right not in {"CE", "PE"}
        or not is_finite_decimal(strike) or strike <= 0
        or not is_finite_decimal(contract.price_tick) or contract.price_tick <= 0
        or type(contract.lot_size) is not int or contract.lot_size <= 0
    ):
        raise ValueError("Liquidity checks require a validated NIFTY option contract.")
    if type(lots) is not int or lots <= 0:
        raise ValueError("Liquidity checks require a positive whole-lot quantity.")
    now = as_ist(at)
    direction = 1 if contract.right == "CE" else -1
    moneyness = direction * (atm - strike) / config.strike_interval
    requested = lots * contract.lot_size
    base = {"strike": strike, "contract": contract, "quote": quote, "quantity": requested}
    if not -config.max_otm_steps <= moneyness <= config.max_itm_steps:
        return CandidateAudit(**base, reasons=("moneyness",))
    if quote is None:
        return CandidateAudit(**base, reasons=("missing_quote",))
    if quote.token != contract.token:
        return CandidateAudit(**base, reasons=("quote_token_mismatch",))
    if quote.exchange_at > quote.received_at or quote.received_at > now:
        return CandidateAudit(**base, reasons=("future_quote",))
    if quote.exchange_at.date() != now.date() or (
        now - quote.exchange_at
    ).total_seconds() >= config.quote_max_age_seconds:
        return CandidateAudit(**base, reasons=("stale_quote",))

    bids, asks = quote.bids[:config.depth_levels], quote.asks[:config.depth_levels]
    tick = contract.price_tick
    if not _valid_levels(bids, tick, descending=True) or not _valid_levels(asks, tick, descending=False):
        return CandidateAudit(**base, reasons=("invalid_book",))
    bid, ask = bids[0].price, asks[0].price
    if bid >= ask:
        return CandidateAudit(**base, reasons=("invalid_book",))

    mid, spread = (ask + bid) / 2, ask - bid
    bid_depth = sum(level.quantity for level in bids)
    ask_depth = sum(level.quantity for level in asks)
    required_depth = config.depth_multiple * requested
    reasons, warnings = [], []
    if not config.min_premium <= mid <= config.max_premium:
        reasons.append("premium_band")
    if spread > mid * config.max_spread_fraction or spread > tick * config.max_spread_ticks:
        reasons.append("wide_spread")
    if min(bid_depth, ask_depth) < required_depth:
        reasons.append("insufficient_depth")
    if type(quote.open_interest) is not int or quote.open_interest < config.min_open_interest:
        reasons.append("low_open_interest")
    if type(quote.day_volume) is not int or quote.day_volume < config.min_day_volume:
        reasons.append("low_day_volume")

    freeze = greeks = None
    # Depth bounds the size of a usable split plan.
    if "insufficient_depth" not in reasons:
        try:
            freeze = split_quantity(requested, contract, config.quantity_freeze_units)
        except FreezeError:
            reasons.append("invalid_freeze")
    try:
        close = market.fo_close(contract.expiry, index_expiry=contract.expiry) if market and contract.expiry else None
        greeks = implied_greeks(spot, contract, mid, now, config, expiry_close=close)
    except GreeksError:
        reasons.append("greeks_unavailable")
    else:
        if not config.min_abs_delta <= abs(greeks.delta) <= config.max_abs_delta:
            reasons.append("delta_band")
        if quote.broker_delta is not None:
            if not is_finite_decimal(quote.broker_delta) or abs(quote.broker_delta) > 1:
                warnings.append("invalid_broker_delta")
            elif abs(quote.broker_delta - greeks.delta) > config.broker_delta_tolerance:
                warnings.append("broker_delta_disagreement")

    return CandidateAudit(
        **base, reasons=tuple(reasons), warnings=tuple(warnings), mid=mid, spread=spread,
        spread_ticks=spread / tick, spread_fraction=spread / mid, bid_depth=bid_depth,
        ask_depth=ask_depth, required_depth=required_depth, greeks=greeks, freeze=freeze,
    )
