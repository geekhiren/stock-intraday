from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

from orderbot.broker.history import parse_minutes
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import MINUTE
from orderbot.data.replay import AnalyzedBar
from orderbot.strategy.real_trial import BrokerCandleConfirmations, compare_trial_candles


HERE = Path(__file__).resolve().parent
CONFIG = load_config(HERE.parent / "config.yaml")
FIXTURE = json.loads((HERE / "fixtures/real_trial_candle.json").read_text(encoding="utf-8"))
OPEN = parse_timestamp(FIXTURE["reference"][0])
CANDLE, = parse_minutes([FIXTURE["reference"]], OPEN, OPEN + MINUTE)


def test_broker_candles_need_two_matching_reads_after_close_and_cannot_jump_a_gap():
    tracker = BrokerCandleConfirmations(OPEN, CONFIG.paper_trial.finalize_delay_seconds)
    eligible = OPEN + MINUTE + timedelta(seconds=CONFIG.paper_trial.finalize_delay_seconds)
    assert tracker.accept((CANDLE,), eligible - timedelta(seconds=1)) == ()
    after = OPEN + 2 * MINUTE + timedelta(seconds=CONFIG.paper_trial.finalize_delay_seconds)
    next_candle = replace(CANDLE, at=OPEN + MINUTE)
    assert tracker.accept((next_candle,), after) == ()
    assert tracker.accept((CANDLE, next_candle), after + timedelta(seconds=1)) == ()
    assert tracker.accept((CANDLE, next_candle), after + timedelta(seconds=2)) == (CANDLE, next_candle)
    assert tracker.accept((CANDLE, next_candle), after + timedelta(seconds=3)) == ()


def test_broker_revision_requires_another_confirmation_and_missing_read_resets_streak():
    tracker = BrokerCandleConfirmations(OPEN, CONFIG.paper_trial.finalize_delay_seconds)
    eligible = OPEN + MINUTE + timedelta(seconds=CONFIG.paper_trial.finalize_delay_seconds)
    revised = replace(CANDLE, volume=CANDLE.volume + 1)
    assert tracker.accept((CANDLE,), eligible) == ()
    assert tracker.accept((revised,), eligible + timedelta(seconds=1)) == ()
    assert tracker.accept((), eligible + timedelta(seconds=2)) == ()
    assert tracker.accept((revised,), eligible + timedelta(seconds=3)) == ()
    assert tracker.accept((revised,), eligible + timedelta(seconds=4)) == (revised,)


def test_quote_comparison_normalizes_volume_and_keeps_price_differences():
    price = Decimal(FIXTURE["stream_high_close"])
    live = replace(CANDLE, high=price, close=price, volume=FIXTURE["stream_volume_units"])
    result = compare_trial_candles([AnalyzedBar(live, None)], [CANDLE], OPEN, OPEN + MINUTE,
                                   lot_size=FIXTURE["contract_lot_size"], price_tick=Decimal(FIXTURE["price_tick"]))
    difference, = result["mismatches"]
    mismatch = {"capture": str(price), "reference": str(CANDLE.high)}
    assert difference == {"at": OPEN.isoformat(), "high": mismatch, "close": mismatch}
    assert result["matched_volume_minutes"] == 1
