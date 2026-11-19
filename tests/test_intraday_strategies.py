import json
from pathlib import Path

from orderbot.core.config import load_config
from research.intraday_strategies import s6_gap_and_go
from research.intraday_strategies.common import Plan, build_days, simulate, summary

CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
SAFETY = json.loads((Path(__file__).parent / "fixtures" / "paper_safety.json").read_text())


def session(day, closes):
    """One-minute candles from 09:15, each 0.10 wide around its close, with a prior day so RSI and gaps have history."""
    rows = [[f"2026-11-27T15:29:00+05:30", 100, 100, 100, 100, 1000]]
    for index, close in enumerate(closes):
        clock = f"{9 + (15 + index) // 60:02d}:{(15 + index) % 60:02d}"
        rows.append([f"{day}T{clock}:00+05:30", close, close + 0.05, close - 0.05, close, 1000])
    return rows


def test_simulator_sizes_by_risk_fills_the_target_and_prefers_the_stop():
    days = build_days(session("2026-11-30", [100.0] * 5 + [100.5, 101.0, 102.1, 103.0]))
    today = days[-1]
    # Buy at the 09:19 close (100.00 + one tick); stop 99.05 risks 1.00 a share, so Rs 500 buys 499 within a Rs 50,000 slot.
    trade, = simulate([today], lambda day: [Plan(4, 1, 99.05, target=102.05)], CONFIG, risk=500, slot=50000)
    assert (trade["quantity"], trade["entry"], trade["exit"], trade["reason"]) == (499, 100.05, 102.05, "target")
    assert trade["gross"] == 998.0 and trade["gross"] - 70 < trade["net"] < trade["gross"] - 60  # about Rs 65 of charges
    # A candle that touches both the stop and the target counts as a stop, one tick worse.
    wide = build_days(session("2026-11-30", [100.0] * 5) + [["2026-11-30T09:20:00+05:30", 100, 103, 98, 100, 1000]])[-1]
    stopped, = simulate([wide], lambda day: [Plan(4, 1, 99.05, target=102.05)], CONFIG, risk=500, slot=50000)
    assert (stopped["reason"], stopped["exit"]) == ("stop", 99.0) and summary([trade, stopped])["accuracy_pct"] == 50.0


def test_missing_or_duplicate_minutes_cannot_complete_a_candle_or_opening_range():
    _, *rows = session("2026-11-30", [100.0] * 15 + [100.6] * 5)
    missing = SAFETY["missing_opening_minute_index"]
    cases = ([rows[4]], rows[:missing] + rows[missing + 1:], rows[:missing] + [rows[0]] + rows[missing + 1:])
    for batch in cases:
        today, = build_days(batch)
        assert all(bar.clock != "09:19" for bar in today.bars(5))
        assert today.bars(15) == []
        assert s6_gap_and_go._opening(today) is None
    today, = build_days(rows)
    opening = s6_gap_and_go._opening(today)
    assert (opening.clock, opening.volume, opening.end) == ("09:29", 15000, 14)
