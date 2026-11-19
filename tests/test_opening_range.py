from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from orderbot.broker.ws_client import Level
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import MINUTE
from orderbot.data.indicators import IndicatorEngine
from orderbot.data.replay import AnalyzedBar
from orderbot.execution.journal import Journal
from orderbot.execution.lifecycle import OrderIntent, OrderState
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.strategy.opening_range import StrategyB, build_range_stop_plan
from orderbot.strategy.replay import load_strategy_fixture
from orderbot.strategy.runner import submit_strategy_decision
from orderbot.strategy.session_replay import replay_paper_session


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/phase8_session.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
EXPECTED = DATA["expected"]
CONFIG = load_config(ROOT / "config.yaml")
D = Decimal


@pytest.fixture
def bars():
    base = load_strategy_fixture(FIXTURE.with_name(DATA["base_session"]), CONFIG)
    engine = IndicatorEngine(CONFIG.data.rsi_period, CONFIG.data.atr_period)
    prices = DATA["opening_futures"]
    first = replace(base.candles[0], open=D(prices[0]), high=D(prices[1]), low=D(prices[2]), close=D(prices[3]))
    candles = (first, *base.candles[1:])
    return base, tuple(AnalyzedBar(item, engine.update(item)) for item in candles)


def assess(base, row, history, *, at=None, previous_close=None, token=None):
    strategy = StrategyB(CONFIG, base.master, base.trading_sessions)
    return strategy.evaluate(row, history, at=at or row.bar.at + MINUTE, token=token or base.future_token,
                             previous_close=previous_close if previous_close is not None else D(DATA["previous_close"]),
                             opening_spot=D(DATA["opening_spot"]), spot=D(DATA["opening_spot"]), quotes={})


def test_range_freezes_with_all_closed_minutes_and_excludes_breakout_candle(bars):
    base, rows = bars
    frozen_at = parse_timestamp(EXPECTED["frozen_at"])
    pending = assess(base, rows[13], rows[:13])
    assert pending.reason == "opening_range_building"
    assert pending.gates["opening_range"].detail["tag"] == "short"
    frozen = assess(base, rows[14], rows[:14], at=frozen_at)
    assert frozen.reason == "range_candle"
    candidate = assess(base, rows[20], rows[:20])
    detail = candidate.gates["opening_range"].detail
    assert detail["frozen_at"] == EXPECTED["frozen_at"]
    assert D(detail["high"]) == D(EXPECTED["range_high"])
    assert D(detail["low"]) == D(EXPECTED["range_low"])
    assert detail["spot_open"] == DATA["opening_spot"]
    assert D(candidate.gates["breakout"].detail["level"]) == D(EXPECTED["breakout_level"])
    assert candidate.reason == "skipped_illiquid" and candidate.signal is None
    assert assess(base, rows[20], rows[1:20]).reason == "opening_range_incomplete"
    assert assess(base, rows[20], rows[:20], at=rows[20].bar.at).reason == "unclosed_candle"
    assert assess(base, rows[20], rows[:20], token=base.master.spot.token).reason == "wrong_signal_instrument"


def test_closed_price_gap_one_breakout_and_exclusive_cutoff(bars):
    base, rows = bars
    current = rows[20]
    no_breakout = replace(current, bar=replace(current.bar, close=D("22228.80")))
    assert assess(base, no_breakout, rows[:20]).reason == "no_range_breakout"
    assert assess(base, current, rows[:20], previous_close=D(DATA["fallback_previous_close"])).reason == "gap_disagrees"
    assert assess(base, rows[21], rows[:21]).reason == "ohl_breakout_already_seen"
    cutoff = parse_timestamp("2026-11-30T10:00:00+05:30")
    assert assess(base, rows[44], rows[:44], at=cutoff).reason == "outside_ohl_window"


def test_long_tag_requires_closed_breakout_at_the_first_valid_tick(bars):
    base, rows = bars
    first = replace(rows[0], bar=replace(rows[0].bar, open=D("22240.00"), low=D("22240.00")))
    history = (first, *rows[1:20])
    threshold = D(DATA["unit"]["long"]["entry"])
    previous_close = D("22230.00")
    current = replace(rows[20], bar=replace(rows[20].bar, high=threshold, close=threshold))
    inside = replace(current, bar=replace(current.bar, close=threshold - base.master.front_future(base.session.day).price_tick))
    assert assess(base, inside, history, previous_close=previous_close).reason == "no_range_breakout"
    accepted = assess(base, current, history, previous_close=previous_close)
    assert accepted.gates["breakout"].detail["side"] == "long"
    assert accepted.gates["breakout"].status == "pass"


@pytest.mark.parametrize("side", ["long", "short"])
def test_range_stop_uses_the_closer_level_then_targets_one_and_a_half_r(side):
    fixture = DATA["unit"]
    case = fixture[side]
    plan = build_range_stop_plan(side, D(case["entry"]), D(case["range_opposite"]), D(case["atr"]),
                                 D(fixture["delta"]), D(fixture["option_entry"]), CONFIG)
    assert plan.underlying_stop == D(case["stop"])
    assert plan.underlying_target == D(case["target"])
    if side == "short":
        assert plan.premium_stop == D(fixture["short_premium_stop"])
        assert plan.premium_target == D(fixture["short_premium_target"])


def test_precedence_is_enforced_at_execution_and_persists_after_a_closed_trade(bars, tmp_path):
    base, rows = bars
    cfg = base.config.model_copy(update={"runtime": base.config.runtime.model_copy(update={"strategy": "ohl"})})
    spot = D(DATA["opening_spot"])
    stamp = parse_timestamp(EXPECTED["ohl_opened_at"])
    entry = DATA["option_events"][0]
    units = DATA["book_quantity"]
    quote = OptionQuote(entry["token"], stamp, stamp, (Level(D(entry["bid"]), units, 1),),
                        (Level(D(entry["ask"]), units, 1),), DATA["option_open_interest"], DATA["option_volume"])
    quotes = {quote.token: quote}
    signal = StrategyB(cfg, base.master, base.trading_sessions).evaluate(
        rows[20], rows[:20], at=stamp, token=base.future_token, previous_close=D(DATA["previous_close"]),
        opening_spot=spot, spot=spot, quotes=quotes,
    )
    clock = [stamp]
    journal_file, venue_file = tmp_path / "journal.sqlite3", tmp_path / "venue.sqlite3"

    def executor(config):
        return PaperExecutor(Journal(journal_file, config.costs), PaperVenue(venue_file, config), config,
                             clock=lambda: clock[0])

    def attempt(exec_, uid, strategy):
        plan = signal.signal.stop_plan
        return exec_.enter(uid, strategy, signal.signal.selection, quote, spot=spot,
                           premium_stop=plan.premium_stop, target_mid=plan.premium_target,
                           target_bid=signal.signal.target_bid).reason

    assert submit_strategy_decision(signal, executor(base.config), spot=spot, quotes=quotes).reason == "ohl_disabled"
    # Use a different decision ID to replay the same market event under the alternative mode.
    signal = replace(signal, id=signal.id + "-ohl-mode")
    exec_ = executor(cfg)
    assert attempt(exec_, "early-a", "vwap") == "vwap_suppressed_ohl_window"
    assert submit_strategy_decision(signal, exec_, spot=spot, quotes=quotes).status == "submitted"
    clock[0] += timedelta(seconds=1)
    exit_quote = replace(quote, exchange_at=clock[0], received_at=clock[0])
    contract = next(contract for contract in base.master.options if contract.token == quote.token)
    assert exec_.exit_position(signal.id, contract, exit_quote, reason="test_exit").status == "submitted"
    assert exec_.positions() == ()
    assert attempt(exec_, "second-b", "ohl") == "ohl_trade_limit"
    clock[0] = parse_timestamp("2026-11-30T10:00:00+05:30")
    restarted = executor(cfg)
    assert restarted.reconcile().ok
    assert attempt(restarted, "late-a", "vwap") == "vwap_suppressed_ohl_traded"


def test_cutoff_cancels_a_working_ohl_buy_before_a_requote_can_fill(bars, tmp_path):
    base, _ = bars
    cfg = base.config.model_copy(update={"runtime": base.config.runtime.model_copy(update={"strategy": "ohl"})})
    entry = DATA["option_events"][0]
    contract = next(contract for contract in base.master.options if contract.token == entry["token"])
    cutoff = parse_timestamp("2026-11-30T10:00:00+05:30")
    stamp = cutoff - timedelta(seconds=1)
    quote = OptionQuote(contract.token, stamp, stamp, (Level(D(entry["bid"]), DATA["book_quantity"], 1),),
                        (Level(D(entry["ask"]), DATA["book_quantity"], 1),), 0, 0)
    journal = Journal(tmp_path / "journal.sqlite3", cfg.costs)
    venue = PaperVenue(tmp_path / "venue.sqlite3", cfg)
    limit = D(entry["ask"])
    intent = OrderIntent("workingohl", "pending-b", "ohl", contract.token, contract.symbol, "BUY", contract.lot_size,
                         contract.lot_size, contract.price_tick, limit, stamp, D("100"), "entry")
    journal.prepare((intent,), journal.snapshot().revision)
    journal.claim(intent.tag, stamp)
    working = venue.submit(intent, quote, stamp)
    assert working.state is OrderState.ACKED
    journal.sync((working,), stamp)
    executor = PaperExecutor(journal, venue, cfg, clock=lambda: cutoff)
    new_quote = replace(quote, exchange_at=cutoff, received_at=cutoff, asks=(quote.bids[0],),
                        bids=(replace(quote.bids[0], price=quote.bids[0].price - contract.price_tick),))
    assert executor.poll({contract.token: new_quote})
    cancelled, = journal.snapshot().orders
    assert cancelled.state is OrderState.CANCELLED and cancelled.reason == "ohl_entry_cutoff"
    assert not cancelled.filled_quantity


def test_full_session_runs_real_paper_orders_and_reconciles_after_ohl_trade():
    run = replay_paper_session(FIXTURE, CONFIG, mode="ohl", scenario="ohl_trade", fixture_funding=True)
    assert run["closed_candles"] == EXPECTED["closed_candles"]
    assert run["archived_packets"] > run["closed_candles"]
    assert run["risk_inputs_in_config"] is False and run["fixture_funding"] is True
    assert run["confirmed_flat"] and run["report"]["reconciliation"]["delta_paise"] == 0
    assert run["report"]["reconciliation"]["status"] == "reconciled"
    trade, = run["trades"]
    assert trade["strategy"] == "ohl" and trade["side"] == EXPECTED["trade_side"]
    assert trade["entry_at"] == EXPECTED["ohl_opened_at"] and trade["exit_at"] == EXPECTED["ohl_closed_at"]
    assert trade["exit_reason"] == EXPECTED["ohl_exit_reason"]
    assert trade["net_paise"] == EXPECTED["ohl_net_paise"] and trade["charges_paise"] == EXPECTED["ohl_charges_paise"]
    assert [event["kind"] for event in run["alerts"]] == ["entry", "exit"]
    assert all(event["status"] == "queued" for event in run["alerts"])
    assert any(event["event"] == "backstop_confirmed_flat" for event in run["exit_events"])
    assert run["routing"]["vwap_suppressed_ohl_traded"] > 0


def test_fallback_if_ohl_did_not_trade_and_unfunded_session_fails_closed():
    fallback = replay_paper_session(FIXTURE, CONFIG, mode="ohl", scenario="no_ohl_trade", fixture_funding=True)
    trade, = fallback["trades"]
    assert trade["strategy"] == EXPECTED["fallback_strategy"] and trade["exit_reason"] == EXPECTED["fallback_exit_reason"]
    assert trade["net_paise"] == EXPECTED["fallback_net_paise"] and trade["charges_paise"] == EXPECTED["fallback_charges_paise"]
    assert fallback["confirmed_flat"] and fallback["report"]["reconciliation"]["delta_paise"] == 0
    assert trade["entry_at"][11:16] >= "10:00"
    unfunded = replay_paper_session(FIXTURE, CONFIG, mode="ohl", scenario="ohl_trade", fixture_funding=False)
    assert not unfunded["trades"] and not unfunded["orders"] and not unfunded["alerts"]
    assert unfunded["routing"]["risk_unconfigured"] == 2


def test_vwap_mode_never_evaluates_ohl():
    run = replay_paper_session(FIXTURE, CONFIG, mode="vwap", scenario="ohl_trade", fixture_funding=True)
    assert run["signals"]["ohl"] == 0
    assert all(trade["strategy"] == "vwap" for trade in run["trades"])
