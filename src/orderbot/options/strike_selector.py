from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from orderbot.broker.instruments import Instrument, InstrumentMaster
from orderbot.core.config import OptionsConfig
from orderbot.core.ist_time import as_ist
from orderbot.core.money import is_finite_decimal

from .expiry_resolver import ExpiryDecision, resolve_weekly_expiry
from .liquidity_gate import CandidateAudit, OptionQuote, evaluate_candidate


@dataclass(frozen=True, slots=True)
class StrikeDecision:
    at: datetime
    right: str
    spot: Decimal
    atm: Decimal
    lots: int
    expiry: ExpiryDecision
    outcome: str
    selected_token: str | None
    candidates: tuple[CandidateAudit, ...] = ()
    reasons: tuple[str, ...] = ()


def _atm_strike(spot: Decimal, config: OptionsConfig) -> Decimal:
    if not is_finite_decimal(spot) or spot <= 0:
        raise ValueError("Strike selection needs positive finite NIFTY spot.")
    steps = (spot / config.strike_interval).to_integral_value(rounding=ROUND_HALF_UP)
    return steps * config.strike_interval


def select_strike(
    master: InstrumentMaster, sessions: tuple[date, ...], quotes: Mapping[str, OptionQuote],
    *, at: datetime, spot: Decimal, right: str, lots: int, config: OptionsConfig,
) -> StrikeDecision:
    now = as_ist(at)
    if right not in {"CE", "PE"} or type(lots) is not int or lots <= 0:
        raise ValueError("Selection requires CE or PE and a positive whole-lot quantity.")
    atm = _atm_strike(spot, config)
    expiry = resolve_weekly_expiry(master, now.date(), sessions, config)
    if expiry.expiry is None:
        return StrikeDecision(now, right, spot, atm, lots, expiry, "skipped_no_expiry",
                              None, reasons=(expiry.reason,))

    listed: dict[Decimal, list[Instrument]] = {}
    for contract in master.options:
        if contract.expiry == expiry.expiry and contract.right == right:
            listed.setdefault(contract.strike, []).append(contract)
    candidates = []
    for step in range(-config.candidate_wings, config.candidate_wings + 1):
        strike = atm + step * config.strike_interval
        contracts = listed.get(strike, [])
        if not contracts:
            candidates.append(CandidateAudit(strike, None, None, reasons=("not_listed",)))
        elif len(contracts) > 1:
            candidates.extend(
                CandidateAudit(strike, contract, quotes.get(contract.token), reasons=("ambiguous_listing",))
                for contract in sorted(contracts, key=lambda item: item.token)
            )
        else:
            contract = contracts[0]
            candidates.append(evaluate_candidate(
                contract, quotes.get(contract.token), at=now, spot=spot, atm=atm, lots=lots, config=config,
            ))

    eligible = [item for item in candidates if not item.reasons]
    if not eligible:
        return StrikeDecision(now, right, spot, atm, lots, expiry, "skipped_illiquid",
                              None, tuple(candidates), ("no_liquid_strike",))
    best = min(eligible, key=lambda item: (
        abs(abs(item.greeks.delta) - config.target_abs_delta), item.spread, item.strike, item.contract.token,
    ))
    return StrikeDecision(now, right, spot, atm, lots, expiry, "selected",
                          best.contract.token, tuple(candidates))


def revalidate_selection(
    selected: StrikeDecision, quote: OptionQuote | None, *, at: datetime, spot: Decimal,
    config: OptionsConfig, lots: int | None = None,
) -> StrikeDecision:
    if selected.outcome != "selected":
        raise ValueError("A requote needs an earlier successful selection.")
    original = next(item for item in selected.candidates if (
        item.contract is not None and item.contract.token == selected.selected_token
    ))
    now, atm = as_ist(at), _atm_strike(spot, config)
    intended_lots = selected.lots if lots is None else lots
    candidate = evaluate_candidate(
        original.contract, quote, at=now, spot=spot, atm=atm, lots=intended_lots, config=config,
    )
    reasons = list(candidate.reasons)
    if now < selected.at or now.date() != selected.at.date():
        reasons.append("invalid_selection_time")
    if quote is not None and (
        quote.received_at < original.quote.received_at or quote.exchange_at < original.quote.exchange_at
    ):
        reasons.append("out_of_order_requote")
    if candidate.spread is not None and candidate.spread > original.spread:
        reasons.append("spread_widened")
    if candidate.mid is not None and abs(candidate.mid - original.mid) > (
        original.mid * config.max_requote_mid_fraction
    ):
        reasons.append("mid_moved")
    outcome = "aborted_requote" if reasons else "revalidated"
    return StrikeDecision(
        now, selected.right, spot, atm, intended_lots, selected.expiry, outcome, selected.selected_token,
        (candidate,), tuple(reasons),
    )
