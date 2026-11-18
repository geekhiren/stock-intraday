import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from orderbot.broker.instruments import parse_master
from orderbot.broker.ws_client import Level, MarketTick
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.options.expiry_resolver import resolve_weekly_expiry
from orderbot.options.freeze_quantity import FreezeError, split_quantity
from orderbot.options.greeks import Greeks, GreeksError, implied_greeks
from orderbot.options.liquidity_gate import OptionQuote, evaluate_candidate
from orderbot.options.replay import replay_options
from orderbot.options.strike_selector import revalidate_selection, select_strike


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SESSION_PATH = FIXTURES / "phase3_session.json"
SESSION = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
CONFIG = load_config(ROOT / "config.yaml")
MASTER = parse_master(json.loads((FIXTURES / SESSION["instrument_master"]).read_text(encoding="utf-8")),
                      CONFIG.market.underlying)
SESSIONS = tuple(date.fromisoformat(day) for day in SESSION["trading_sessions"])
REFERENCE = json.loads((FIXTURES / "black76.json").read_text(encoding="utf-8"))
CONTRACTS = {contract.token: contract for contract in MASTER.options}
AT = parse_timestamp(REFERENCE["at"])


def quote(token="4102", *, bid="119.90", ask="120.10", bid_count=195, ask_count=195):
    tick = MarketTick(
        token, 2, AT, AT, 1, D("120"), CONFIG.options.min_day_volume, CONFIG.options.min_open_interest,
        (Level(D(bid), bid_count, 1),), (Level(D(ask), ask_count, 1),),
    )
    return OptionQuote.from_tick(tick, AT)


def assess(snapshot, config=CONFIG.options):
    return evaluate_candidate(
        CONTRACTS["4102"], snapshot, at=AT, spot=D("22150"), atm=D("22150"), lots=1, config=config,
    )


@pytest.mark.parametrize(("day", "selected"), [
    ("2026-11-27", "2026-12-01"),
    ("2026-11-30", "2026-12-08"),
    ("2026-12-01", "2026-12-08"),
])
def test_weekly_rule_skips_expiry_day_and_one_session_before(day, selected):
    decision = resolve_weekly_expiry(MASTER, date.fromisoformat(day), SESSIONS, CONFIG.options)
    assert decision.expiry.isoformat() == selected
    assert decision.reviews[-1].sessions_inclusive >= 3


def test_holiday_shift_comes_from_master_and_trading_sessions():
    scheduled = date(2026, 12, 1)
    shifted = date(2026, 11, 30)
    contracts = tuple(replace(item, expiry=shifted) if item.expiry == scheduled else item for item in MASTER.options)
    calendar = tuple(day for day in SESSIONS if day != scheduled)
    decision = resolve_weekly_expiry(replace(MASTER, options=contracts), SESSIONS[0], calendar, CONFIG.options)
    assert decision.reviews[0].expiry == shifted
    assert decision.reviews[0].sessions_inclusive == 2
    assert decision.expiry == date(2026, 12, 8)


def test_monthlies_and_unclassified_or_uncovered_dates_fail_closed():
    day = date(2026, 11, 30)
    only_monthly = replace(MASTER, options=(CONTRACTS["4302"],))
    outcome = resolve_weekly_expiry(only_monthly, day, SESSIONS, CONFIG.options)
    assert outcome.expiry is None
    assert outcome.reviews[0].reason == "monthly_series"
    assert resolve_weekly_expiry(MASTER, day, SESSIONS[:3], CONFIG.options).reason == "calendar_coverage_missing"
    partial = replace(MASTER, futures=MASTER.futures[:1])
    assert resolve_weekly_expiry(partial, day, SESSIONS, CONFIG.options).reason == "monthly_series_unverified"


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda case: case["right"])
def test_black76_matches_independent_brent_root(case):
    contract = next(item for item in MASTER.options if item.right == case["right"] and item.strike == D(case["strike"])
                    and item.expiry == date(2026, 12, 8))
    result = implied_greeks(D(REFERENCE["spot"]), contract, D(case["mid"]), parse_timestamp(REFERENCE["at"]),
                            CONFIG.options)
    assert float(result.iv) == pytest.approx(case["iv"], abs=1e-7)
    assert float(result.delta) == pytest.approx(case["delta"], abs=1e-7)
    with pytest.raises(GreeksError):
        implied_greeks(D(REFERENCE["spot"]), contract, D("30000"), parse_timestamp(REFERENCE["at"]), CONFIG.options)


def test_freeze_chunks_are_whole_lots_and_never_hit_raw_exclusive_ceiling():
    contract = CONTRACTS["4102"]
    plan = split_quantity(1820, contract, CONFIG.options.quantity_freeze_units)
    assert plan.children == (1755, 65)
    assert plan.max_order_quantity == 1755
    small = replace(contract, lot_size=75)
    assert split_quantity(1800, small, 1800).children == (1800,)
    assert split_quantity(1800, replace(small, raw_freeze_quantity=1800), 1800).children == (1725, 75)
    with pytest.raises(FreezeError):
        split_quantity(1800, contract, 1800)
    with pytest.raises(FreezeError):
        split_quantity(65, replace(contract, raw_freeze_quantity=None), 1800)


def test_liquidity_accepts_equal_thresholds_and_checks_both_sides():
    at_limits = assess(quote())
    assert not at_limits.reasons
    assert at_limits.spread_ticks == 4
    assert at_limits.bid_depth == at_limits.ask_depth == at_limits.required_depth
    assert assess(quote(ask_count=194)).reasons == ("insufficient_depth",)
    tighter_pct = CONFIG.options.model_copy(update={"max_spread_fraction": D("0.001")})
    assert assess(quote(), tighter_pct).reasons == ("wide_spread",)


def test_missing_crossed_off_tick_and_future_books_fail_closed():
    assert assess(None).reasons == ("missing_quote",)
    assert assess(quote(bid="120.20")).reasons == ("invalid_book",)
    assert assess(quote(bid="119.91")).reasons == ("invalid_book",)
    assert assess(quote("4101")).reasons == ("quote_token_mismatch",)
    future = replace(quote(), received_at=AT + timedelta(milliseconds=1))
    assert assess(future).reasons == ("future_quote",)
    assert "premium_band" in assess(quote(bid="400.05", ask="400.15")).reasons
    assert "delta_band" in assess(
        quote(), CONFIG.options.model_copy(update={"min_abs_delta": D("0.56")}),
    ).reasons


def test_equal_delta_prefers_tighter_book_even_at_higher_premium(monkeypatch):
    monkeypatch.setattr(
        "orderbot.options.liquidity_gate.implied_greeks",
        lambda *_: Greeks(D("0.1"), D("0.52"), D("22175")),
    )
    snapshots = {"4101": quote("4101", bid="200.00", ask="200.05"), "4102": quote()}
    choice = select_strike(
        MASTER, SESSIONS, snapshots, at=AT, spot=D("22150"), right="CE", lots=1, config=CONFIG.options,
    )
    assert choice.selected_token == "4101"
    assert [item.contract.token for item in choice.candidates if not item.reasons] == ["4101", "4102"]


def test_requote_checks_final_quantity_against_current_depth_and_original_contract():
    snapshot = quote()
    choice = select_strike(
        MASTER, SESSIONS, {snapshot.token: snapshot}, at=AT, spot=D("22150"),
        right="CE", lots=1, config=CONFIG.options,
    )
    later = AT + timedelta(seconds=1)
    fresh = replace(snapshot, exchange_at=later, received_at=later)
    greater_quantity = revalidate_selection(
        choice, fresh, at=later, spot=D("22150"), config=CONFIG.options, lots=2,
    )
    assert greater_quantity.outcome == "aborted_requote"
    assert greater_quantity.reasons == ("insufficient_depth",)
    assert greater_quantity.selected_token == snapshot.token
    old = replace(snapshot, exchange_at=AT - timedelta(seconds=1))
    assert revalidate_selection(
        choice, old, at=later, spot=D("22150"), config=CONFIG.options,
    ).reasons == ("out_of_order_requote",)


def test_replay_audits_every_strike_and_requote():
    report = replay_options(SESSION_PATH, CONFIG)
    assert len(report) == len(SESSION["expected"])
    for row, expected in zip(report, SESSION["expected"]):
        assert row["id"] == expected["id"]
        assert row["outcome"] == expected["outcome"]
        assert row["selected_token"] == expected.get("token")
        assert row["risk_configured"] is False
        if "children" in expected:
            winner = next(item for item in row["candidates"] if item["token"] == row["selected_token"])
            assert winner["freeze"]["children"] == list(expected["children"])
            assert winner["quote"]["bids"] and winner["quote"]["asks"]
        if "reasons" in expected:
            assert row["reasons"] == expected["reasons"]
    initial = {row["token"]: row for row in report[0]["candidates"]}
    assert len(initial) == 5
    assert initial["4101"]["reasons"] == ["wide_spread"]
    assert initial["4102"]["warnings"] == ["broker_delta_disagreement"]
    skipped = {row["token"]: row for row in report[3]["candidates"]}
    assert skipped["4101"]["reasons"] == ["stale_quote"]
    assert set(skipped["4102"]["reasons"]) == {"insufficient_depth", "low_open_interest", "low_day_volume"}
    assert report[2]["revalidates"] == "call-choice"


def test_options_replay_cli_is_byte_identical():
    command = [sys.executable, str(ROOT / "run.py"), "--replay-options", str(SESSION_PATH)]
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    output = subprocess.check_output(command, env=environment)
    assert output == subprocess.check_output(command, env=environment)
    assert json.loads(output) == replay_options(SESSION_PATH, CONFIG)
