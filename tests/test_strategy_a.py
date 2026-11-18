import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from orderbot.broker.ws_client import Level
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import MINUTE, Candle
from orderbot.data.indicators import BarIndicators, PivotLevels
from orderbot.data.replay import AnalyzedBar
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.strategy.iv_store import IVStore, nearest_rank
from orderbot.strategy.replay import load_strategy_fixture, replay_strategy_a
from orderbot.strategy.runner import submit_strategy_decision
from orderbot.strategy.stop_plan import build_stop_plan
from orderbot.strategy.vwap_rsi import StrategyA


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SESSION = FIXTURES / "phase5_session.json"
REFERENCES = json.loads((FIXTURES / "strategy_a_gates.json").read_text(encoding="utf-8"))
CONFIG = load_config(ROOT / "config.yaml")


@pytest.fixture
def session():
    return load_strategy_fixture(SESSION, CONFIG)


def bars(session, *, side="long", index=20, signal_vol=None, rsi=None):
    ref = REFERENCES
    vwap = D(ref["vwap"])
    prior_close = D(ref[f"prior_{side}"])
    signal_close = D(ref[f"close_{side}"])
    recent = D(ref[f"near_{side}"])

    def sample(i, close, volume, rsi):
        bar = Candle(session.session.opens_at + i * MINUTE, close, close, close, close, volume)
        return AnalyzedBar(bar, BarIndicators(vwap, rsi, D(ref["atr"])))

    history = tuple(
        sample(i, recent if i == index - 1 else prior_close, ref["prior_volume"], D("50"))
        for i in range(index)
    )
    return history, sample(index, signal_close, signal_vol or ref["expected_volume_min"],
                           D("50") if rsi is None else rsi)


def book(at, side):
    token = "4102" if side == "long" else "4202"
    return OptionQuote(token, at, at, (Level(D("119.95"), 1300, 4),),
                       (Level(D("120.05"), 1300, 4),), 20000, 10000)


def assess(session, history, current, *, config=CONFIG, at=None, quotes=None, iv=None, pivots=None, token=None):
    now = at if at is not None else current.bar.at + MINUTE
    side = "long" if current.bar.close > D(REFERENCES["vwap"]) else "short"
    snapshot = book(now, side)
    return StrategyA(config, session.master, session.trading_sessions, iv).evaluate(
        current, history, at=now, token=token or session.future_token, spot=D("22150"),
        quotes={snapshot.token: snapshot} if quotes is None else quotes, pivots=pivots,
    )


def test_closed_futures_only_and_no_future_or_missing_history(session):
    history, current = bars(session)
    early = assess(session, history, current, at=current.bar.at + timedelta(seconds=59))
    assert early.reason == "unclosed_candle"
    assert early.gates["closed_candle"].status == "fail"
    assert not early.signal
    assert assess(session, history, replace(current, source="history")).reason == "backfill_candle"
    assert assess(session, history, current, token="99926000").reason == "wrong_signal_instrument"
    assert assess(session, (*history, current), current).reason == "incomplete_session_history"
    assert assess(session, history[1:], current).reason == "incomplete_session_history"


@pytest.mark.parametrize(("side", "low", "high", "right"), [
    ("long", "40", "70", "CE"), ("short", "30", "60", "PE"),
])
def test_mirrored_rsi_edges_and_valid_crossover(session, side, low, high, right):
    for edge in (D(low), D(high)):
        history, current = bars(session, side=side, rsi=edge)
        result = assess(session, history, current)
        assert result.signal.right == right
        assert set(result.gates) == {"closed_candle", "vwap_crossover", "consolidation", "volume_surge",
                                    "rsi", "pivot", "liquidity", "time_window", "iv"}
        assert result.gates["pivot"].status == "disabled"
        assert result.gates["iv"].status == "disabled"
    history, current = bars(session, side=side, rsi=D(low) - D("0.01"))
    assert assess(session, history, current).reason == "rsi"
    history, current = bars(session, side=side, rsi=D(high) + D("0.01"))
    assert assess(session, history, current).reason == "rsi"


def test_vwap_crossover_uses_previous_vwap_and_requires_strict_cross(session):
    history, current = bars(session)
    previous = history[-1]
    same_as_previous = replace(previous, indicators=replace(previous.indicators, vwap=previous.bar.close))
    assert assess(session, (*history[:-1], same_as_previous), current).signal
    already_above = replace(previous, indicators=replace(previous.indicators, vwap=previous.bar.close - D("1")))
    assert assess(session, (*history[:-1], already_above), current).reason == "no_crossover"


def test_consolidation_and_volume_use_previous_windows_only(session):
    history, current = bars(session)
    accepted = assess(session, history, current)
    assert accepted.signal and accepted.gates["consolidation"].detail["near_vwap"] == 1
    assert D(accepted.gates["volume_surge"].detail["prior_mean"]) == D(REFERENCES["prior_volume"])
    penultimate = history[-2]
    tight = replace(penultimate, indicators=replace(penultimate.indicators, vwap=penultimate.bar.close))
    assert assess(session, (*history[:-2], tight, history[-1]), current).reason == "consolidating"
    history, current = bars(session, signal_vol=149)
    assert assess(session, history, current).reason == "no_volume"


def test_pivot_enabled_uses_directional_band(session):
    config = CONFIG.model_copy(update={"strategy_a": CONFIG.strategy_a.model_copy(update={"require_pivot": True})})
    levels = PivotLevels(D("100"), D("110"), D("120"), D("90"), D("80"))
    history, current = bars(session)
    assert assess(session, history, current, config=config, pivots=levels).signal
    assert assess(session, history, current, config=config).reason == "pivot_unavailable"
    wide = replace(current.bar, open=D("104"), high=D("104"), low=D("104"), close=D("104"))
    assert assess(session, history, replace(current, bar=wide), config=config, pivots=levels).reason == "pivot"
    history, current = bars(session, side="short")
    assert assess(session, history, current, config=config, pivots=levels).signal


def test_time_window_uses_decision_clock_and_market_expiry_cutoff(session):
    history, current = bars(session, index=13)
    assert assess(session, history, current).reason == "outside_entry_window"
    history, current = bars(session, index=329)
    assert assess(session, history, current).reason == "outside_entry_window"
    day = date(2026, 12, 1)
    moved = replace(session, session=type(session.session).from_config(day, CONFIG.market))
    history, current = bars(moved, index=164)
    result = assess(moved, history, current)
    assert result.reason == "expiry_day_cutoff"
    assert result.gates["time_window"].status == "fail"


def test_illiquid_signal_is_a_skip(session):
    history, current = bars(session)
    result = assess(session, history, current, quotes={})
    assert result.reason == "skipped_illiquid"
    assert result.gates["liquidity"].status == "fail"
    assert result.signal is None


def test_iv_gate_uses_prior_sessions_excludes_today_and_requires_current_atm(session, tmp_path):
    store = IVStore(tmp_path / "iv.sqlite3")
    history, current = bars(session)
    baseline = assess(session, history, current)
    atm_iv = baseline.signal.selection.candidates[2].greeks.iv
    for index in range(20):
        observed_at = current.bar.at - timedelta(days=index + 1)
        store.record("CE", observed_at, atm_iv)
    store.record("CE", current.bar.at, D("0.9"))
    store.record("CE", current.bar.at + timedelta(days=1), D("0.9"))
    result = assess(session, history, current, iv=store)
    assert result.reason == "iv_at_or_above_threshold"
    assert result.gates["iv"].detail["prior_sessions"] == 20
    now = current.bar.at + MINUTE
    itm_only = replace(book(now, "long"), token="4101", bids=(Level(D("199.95"), 1300, 4),),
                       asks=(Level(D("200.05"), 1300, 4),))
    assert assess(session, history, current, quotes={"4101": itm_only}, iv=store).reason == "atm_iv_unavailable"
    assert nearest_rank(tuple(D(i) for i in range(1, 21)), D("80")) == D("16")


def test_stop_plan_mirrors_underlying_and_maps_long_premium_for_both_sides():
    values = REFERENCES["reference_stop"]
    for side, label in (("long", "bull"), ("short", "bear")):
        plan = build_stop_plan(side, D(values["futures_entry"]), D(values["atr"]), D(values["delta"]),
                               D(values["option_entry"]), CONFIG.strategy_a)
        assert plan.underlying_stop == D(values[f"{label}_stop"])
        assert plan.underlying_target == D(values[f"{label}_target"])
        assert plan.premium_stop == D(values["premium_stop"])
        assert plan.premium_target == D(values["premium_target"])
        assert plan.premium_floor == D(values["premium_floor"])
        assert plan.with_entry_fill(D("121")).premium_stop == D("100.20")
    bounded = build_stop_plan("long", D("22150"), D("100"), D("0.52"), D("120"), CONFIG.strategy_a)
    assert bounded.premium_stop == bounded.premium_floor == D("78")


def paper_executor(session, tmp_path, clock):
    return PaperExecutor(
        Journal(tmp_path / "journal.sqlite3", session.config.costs),
        PaperVenue(tmp_path / "venue.sqlite3", session.config), session.config,
        clock=lambda: clock[0], tag_factory=iter(("fixture000", "fixture001", "fixture002")).__next__,
    )


def test_execution_rechecks_latest_quote_and_cutoff(session, tmp_path):
    history, current = bars(session)
    signal = assess(session, history, current)
    clock = [signal.at + timedelta(seconds=1)]
    executor = paper_executor(session, tmp_path, clock)
    quote = book(clock[0], "long")
    wider = replace(quote, bids=(Level(D("119.90"), 1300, 4),), asks=(Level(D("120.10"), 1300, 4),))
    assert submit_strategy_decision(
        signal, executor, spot=D("22150"), quotes={wider.token: wider},
    ).reason == "aborted_requote"
    assert executor.journal.snapshot().orders == ()

    history, current = bars(session, index=328)
    late = assess(session, history, current)
    assert late.signal
    clock[0] = late.at + MINUTE
    assert submit_strategy_decision(late, executor, spot=D("22150"), quotes={}).reason == "outside_entry_window"
    assert executor.journal.snapshot().orders == ()


def test_unclosed_input_does_not_reserve_the_closed_signal_id(session, tmp_path):
    history, current = bars(session)
    clock = [current.bar.at + timedelta(seconds=59)]
    executor = paper_executor(session, tmp_path, clock)
    early = assess(session, history, current, at=clock[0])
    assert submit_strategy_decision(early, executor, spot=None, quotes={}).reason == "unclosed_candle"
    clock[0] += timedelta(seconds=1)
    signal = assess(session, history, current)
    quote = book(clock[0], "long")
    assert submit_strategy_decision(
        signal, executor, spot=D("22150"), quotes={quote.token: quote},
    ).status == "submitted"


def test_cooldown_uses_last_completed_exit(session, tmp_path):
    history, current = bars(session)
    signal = assess(session, history, current)
    clock = [signal.at]
    executor = paper_executor(session, tmp_path, clock)
    quote = book(clock[0], "long")
    assert submit_strategy_decision(signal, executor, spot=D("22150"), quotes={quote.token: quote}).status == "submitted"
    clock[0] += MINUTE
    quote = book(clock[0], "long")
    contract = next(option for option in session.master.options if option.token == quote.token)
    assert executor.exit_position(signal.id, contract, quote, reason="fixture_exit").status == "submitted"
    history, current = bars(session, side="short", index=25)
    next_signal = assess(session, history, current)
    assert next_signal.signal
    clock[0] = next_signal.at
    quote = book(clock[0], "short")
    assert submit_strategy_decision(
        next_signal, executor, spot=D("22150"), quotes={quote.token: quote},
    ).reason == "cooldown"
    assert executor.positions() == ()


def test_full_session_replay_uses_real_strategy_and_paper_fills():
    expected = json.loads(SESSION.read_text(encoding="utf-8"))["expected"]
    report = replay_strategy_a(SESSION, CONFIG)
    assert report["closed_candles"] == expected["closed_candles"]
    assert report["risk_inputs_in_config"] is False
    assert len(report["trades"]) == 2
    for trade, want in zip(report["trades"], expected["signals"]):
        assert trade["right"] == want["right"]
        assert trade["signal_candle_at"][11:16] == want["minute"]
        assert trade["opened_at"][11:19] == want["closed_at"]
        assert trade["net_pnl_inr"] == want["net_pnl_inr"]
        assert trade["exit_reason"] == "fixture_exit"
    assert report["net_pnl_inr"] == expected["net_pnl_inr"]


def test_cli_replay_produces_identical_bytes():
    command = [sys.executable, str(ROOT / "run.py"), "--replay-strategy-a", str(SESSION)]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    first = subprocess.check_output(command, env=env)
    second = subprocess.check_output(command, env=env)
    assert first == second
    assert len(json.loads(first)["trades"]) == 2
