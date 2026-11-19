import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import MINUTE, Candle
from orderbot.data.indicators import BarIndicators
from orderbot.data.replay import AnalyzedBar
from orderbot.execution.exit_engine import ExitEngine
from orderbot.execution.exit_rules import ExitFrame, TrailState, evaluate_exit
from orderbot.execution.exit_replay import _open_drill, replay_exits
from orderbot.execution.lifecycle import OrderState
from orderbot.execution.replay import load_execution_fixture
from orderbot.risk.kill_switch import Health, KillSwitch
from orderbot.strategy.stop_plan import build_stop_plan


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "phase6_exits.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
CONFIG = load_config(ROOT / "config.yaml")
OPENED = parse_timestamp("2026-11-30T09:45:00+05:30")
PLAN = build_stop_plan("long", D("22150"), D("50"), D("0.52"), D("120.10"), CONFIG.strategy_a)


def candle(at, close="22155", rsi="50", vwap="22150"):
    price = D(close)
    return AnalyzedBar(Candle(at, price, price, price, price, 100), BarIndicators(D(vwap), D(rsi), D("50")))


def test_exact_exit_priority_and_stop_before_targets():
    frame = ExitFrame(OPENED + MINUTE, "tick", underlying=D("22050"), option_ltp=D("225"))
    state, reason = evaluate_exit(PLAN, OPENED, TrailState(), frame, CONFIG.exits)
    assert reason == "underlying_stop"
    frame = replace(frame, underlying=D("22400"), option_ltp=D("78"))
    assert evaluate_exit(PLAN, OPENED, state, frame, CONFIG.exits, kill="sentinel")[1] == "kill_switch"
    assert evaluate_exit(PLAN, OPENED, state, frame, CONFIG.exits)[1] == "premium_floor"
    square_off = replace(frame, at=parse_timestamp(DATA["square_off"]["at"]))
    assert evaluate_exit(PLAN, OPENED, state, square_off, CONFIG.exits, kill="sentinel")[1] == "forced_square_off"
    mapped = build_stop_plan("long", D("22150"), D("20"), D("0.52"), D("120.10"), CONFIG.strategy_a)
    assert evaluate_exit(mapped, OPENED, TrailState(), replace(frame, option_ltp=D("95")), CONFIG.exits)[1] == (
        "premium_stop"
    )


@pytest.mark.parametrize(("kind", "underlying", "premium", "elapsed", "expected"), [
    ("tick", "22350", "225", 1, "premium_target"),
    ("tick", "22350", "140", 1, "underlying_target"),
    ("timer", None, None, 20, "stall"),
    ("timer", None, None, 120, "max_hold"),
])
def test_price_and_timer_triggers(kind, underlying, premium, elapsed, expected):
    mark = D("145") if expected == "max_hold" else None
    frame = ExitFrame(OPENED + elapsed * MINUTE, kind, D(underlying) if underlying else None,
                      D(premium) if premium else None)
    assert evaluate_exit(PLAN, OPENED, TrailState(peak=mark), frame, CONFIG.exits)[1] == expected


def test_short_direction_and_only_closed_candle_triggers_rsi_and_vwap():
    short = build_stop_plan("short", D("22150"), D("50"), D("-0.52"), D("120.10"), CONFIG.strategy_a)
    now = OPENED + 2 * MINUTE
    assert evaluate_exit(short, OPENED, TrailState(), ExitFrame(now, "tick", underlying=D("22250")),
                         CONFIG.exits)[1] == "underlying_stop"
    row = candle(now - MINUTE, close="22160", rsi="29")
    assert evaluate_exit(short, OPENED, TrailState(), ExitFrame(now, "candle", candle=row),
                         CONFIG.exits)[1] == "rsi_exhaustion"
    future = candle(now, close="22160", rsi="29")
    assert evaluate_exit(short, OPENED, TrailState(), ExitFrame(now, "candle", candle=future),
                         CONFIG.exits)[1] is None
    row = candle(now - MINUTE, close="22160", rsi="30")
    assert evaluate_exit(short, OPENED, TrailState(), ExitFrame(now, "candle", candle=row),
                         CONFIG.exits)[1] == "vwap_break"


def test_simultaneous_stall_timer_precedes_closed_candle_rsi():
    at = OPENED + CONFIG.exits.stall_minutes * MINUTE
    frame = ExitFrame(at, "candle", candle=candle(at - MINUTE, rsi="71"), timer_due=True)
    assert evaluate_exit(PLAN, OPENED, TrailState(), frame, CONFIG.exits)[1] == "stall"


def test_trailing_arms_break_even_then_ratchets_only_on_closed_candles():
    data = DATA["trailing"]
    assert PLAN.option_entry - PLAN.premium_stop == D(data["risk_per_unit"])
    at = OPENED + MINUTE
    state, reason = evaluate_exit(PLAN, OPENED, TrailState(), ExitFrame(at, "tick", option_ltp=D(data["one_r_mark"])),
                                  CONFIG.exits, break_even=D(data["break_even_stop"]))
    assert reason is None and state.stop == D(data["break_even_stop"])
    state, reason = evaluate_exit(PLAN, OPENED, state, ExitFrame(at + MINUTE, "tick", option_ltp=D(data["two_r_mark"])),
                                  CONFIG.exits, break_even=D(data["break_even_stop"]))
    assert reason is None and state.stop == D(data["break_even_stop"])
    row = candle(at)
    state, reason = evaluate_exit(PLAN, OPENED, state, ExitFrame(at + MINUTE, "candle", candle=row), CONFIG.exits)
    assert reason is None and state.stop == D(data["trailing_stop"])
    state, reason = evaluate_exit(PLAN, OPENED, state, ExitFrame(at + 2 * MINUTE, "tick", option_ltp=D("162.60")),
                                  CONFIG.exits)
    assert reason == "trailing_stop" and state.stop == D(data["trailing_stop"])


def test_kill_switch_boundaries_and_expired_volatility_samples(tmp_path):
    switch = KillSwitch(CONFIG, tmp_path)
    at = OPENED
    assert switch.evaluate(at, Health(clock_drift_seconds=D("2"), rejections=2), position_opened_at=None) is None
    assert switch.evaluate(at + timedelta(seconds=29), Health(ws_disconnected_at=at), position_opened_at=None) is None
    assert switch.evaluate(at + timedelta(seconds=30), Health(ws_disconnected_at=at), position_opened_at=None) == "websocket_down"
    switch.observe_underlying(at, D("100"))
    switch.observe_underlying(at + timedelta(seconds=300), D("101.5"))
    assert switch.evaluate(at + timedelta(seconds=300), Health(), position_opened_at=None) is None
    switch.observe_underlying(at + timedelta(seconds=301), D("101.51"))
    assert switch.evaluate(at + timedelta(seconds=301), Health(), position_opened_at=None) is None


def test_exit_engine_restores_trailing_state_and_ignores_unrelated_option_marks(tmp_path):
    fixture = load_execution_fixture(FIXTURE.with_name(DATA["entry_fixture"]), CONFIG)
    drill = _open_drill(tmp_path, fixture, CONFIG, DATA["plan"])
    token = fixture.contract.token
    uid = drill.funded.positions()[0].decision_id
    one, two = D(DATA["trailing"]["one_r_mark"]), D(DATA["trailing"]["two_r_mark"])
    guide = DATA["trailing"]
    at = OPENED + MINUTE
    drill.clock[0] = at
    quote = drill.book(DATA["exit_book"])
    assert not drill.engine.step(ExitFrame(at, "tick", option_ltp=D("1"), option_token="other"),
                                 {token: quote}, Health(last_option_tick_at=at))
    assert drill.engine.store.progress(uid).trail.stop is None
    assert not drill.step(at, quote=quote, option_ltp=one)
    assert drill.engine.store.progress(uid).trail.stop == D(guide["break_even_stop"])

    drill.engine = ExitEngine(drill.engine.executor, {token: fixture.contract}, tmp_path)
    at += MINUTE
    drill.clock[0] = at
    quote = drill.book(DATA["exit_book"])
    assert not drill.step(at, quote=quote, option_ltp=two)
    assert drill.engine.store.progress(uid).trail.stop == D(guide["break_even_stop"])
    assert not drill.engine.step(ExitFrame(at, "candle", candle=candle(at - MINUTE)),
                                 {token: quote}, Health(last_option_tick_at=at))
    assert drill.engine.store.progress(uid).trail.stop == D(guide["trailing_stop"])
    at += timedelta(seconds=1)
    drill.clock[0] = at
    assert drill.step(at, quote=drill.book(DATA["exit_book"]), option_ltp=D(guide["trailing_stop"]))
    assert any(row.get("reason") == "trailing_stop" for row in drill.engine.store.events())


def test_kill_cancels_working_entry_before_matching_fresh_book(tmp_path):
    fixture = load_execution_fixture(FIXTURE.with_name(DATA["entry_fixture"]), CONFIG)
    cfg = fixture.config.model_copy(update={"risk": fixture.config.risk.model_copy(
        update={"capital_inr": fixture.config.risk.capital_inr * 2},
    )})
    drill = _open_drill(tmp_path, replace(fixture, config=cfg), CONFIG, DATA["plan"])
    initial, = drill.funded.journal.snapshot().orders
    assert initial.state is OrderState.PARTIAL
    drill.clock[0] += timedelta(seconds=1)
    assert drill.step(drill.clock[0], quote=drill.book(DATA["exit_book"]), health=Health(dashboard_stop=True))
    entry, exit_order = drill.funded.journal.snapshot().orders
    assert entry.state is OrderState.CANCELLED
    assert entry.filled_quantity == exit_order.filled_quantity == initial.filled_quantity


def test_three_journaled_rejections_fire_the_switch(tmp_path):
    fixture = load_execution_fixture(FIXTURE.with_name(DATA["entry_fixture"]), CONFIG)
    drill = _open_drill(tmp_path, fixture, CONFIG, DATA["plan"])
    venue_config = drill.venue.config
    rejecting = venue_config.execution.model_copy(update={"paper_rejection_pct": D("100")})
    drill.venue.config = venue_config.model_copy(update={"execution": rejecting})
    uid = drill.funded.positions()[0].decision_id
    for _ in range(CONFIG.kill_switch.rejection_limit):
        drill.clock[0] += timedelta(seconds=1)
        quote = drill.book(DATA["exit_book"])
        assert drill.funded.exit_position(uid, fixture.contract, quote, reason="fixture_rejection").reason == (
            "order_rejected"
        )
    drill.venue.config = venue_config
    drill.clock[0] += timedelta(seconds=1)
    assert drill.step(drill.clock[0], quote=drill.book(DATA["exit_book"]))
    assert any(row.get("reason") == "order_rejections" for row in drill.engine.store.events())


def test_square_off_at_1510_can_reduce_at_a_one_sided_bid(tmp_path):
    fixture = load_execution_fixture(FIXTURE.with_name(DATA["entry_fixture"]), CONFIG)
    drill = _open_drill(tmp_path, fixture, CONFIG, DATA["plan"])
    at = parse_timestamp(DATA["square_off"]["at"])
    drill.clock[0] = at
    quote = replace(drill.book(DATA["exit_book"]), asks=())
    assert drill.step(at, quote=quote)
    attempt, = [item for item in drill.engine.store.events() if item["event"] == "exit_attempt"]
    assert attempt["route"] == "limit"
    assert attempt["reason"] == "forced_square_off"
    assert attempt["limit"] == DATA["square_off"]["expected_limit_prices"][0]


def test_fixture_replay_drills_every_switch_and_protected_backstop():
    result = replay_exits(FIXTURE, CONFIG)
    expected = [row["reason"] for row in DATA["switches"]]
    assert [row["reason"] for row in result["switches"]] == expected
    assert result["risk_inputs_in_config"] is False
    for row in result["switches"]:
        assert row["halt_latched"]
        assert row["entries_blocked"]
        assert row["closed"]
    mismatch = next(row for row in result["switches"] if row["reason"] == "position_mismatch")
    assert not mismatch["sold_before_confirmation"]
    square = result["square_off"]
    assert [item["limit"] for item in square["attempts"][:3]] == DATA["square_off"]["expected_limit_prices"]
    assert square["attempts"][-1]["route"] == "protected"
    assert square["attempts"][-1]["limit"] == DATA["square_off"]["protection_limit"]
    assert square["flat_after_backstop"]
    assert [row["state"] for row in square["sell_orders"]] == ["CANCELLED", "CANCELLED", "CANCELLED", "FILLED"]
    assert result["backstop_no_depth"]["flat"] is False
    assert result["backstop_no_depth"]["alerts"] == ["backstop_not_flat"]
