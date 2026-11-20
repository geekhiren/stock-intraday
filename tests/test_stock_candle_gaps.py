from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from orderbot.broker.history import parse_minutes
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import MINUTE, TradingSession
from orderbot.data.stock_history import stock_candle_days
from orderbot.strategy.stock_backtest import backtest
from orderbot.strategy.stock_paper import StockPaperTrader, daily_stats
from research.intraday_strategies import s6_gap_and_go
from research.intraday_strategies.common import Plan, build_days, simulate


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")
FIXTURES = ROOT / "tests/fixtures"
GAPS = json.loads((FIXTURES / "stock_candle_gaps.json").read_text())
SAFETY = json.loads((FIXTURES / "paper_safety.json").read_text())
DAY = date.fromisoformat(GAPS["session"])
SESSION = TradingSession.from_config(DAY, CONFIG.market, segment="cash")
STOCKS = CONFIG.stocks.model_copy(update={"capital_inr": Decimal(SAFETY["stock_capital"]),
                                          "risk_per_trade_pct": Decimal(SAFETY["stock_risk_pct"])})
FUNDED_FIXTURE = CONFIG.model_copy(update={"stocks": STOCKS})


def full_day(session):
    rows = []
    for offset in range((session.closes_at - session.opens_at) // MINUTE):
        at = session.opens_at + offset * MINUTE
        prices = (200, 201, 199.95, 200.5) if offset < CONFIG.strategy_b.range_minutes else (201.2,) * 4
        rows.append([at.isoformat(), *prices, 10_000])
    return rows


@pytest.mark.parametrize("symbol", GAPS["missing_minutes_ist"])
def test_gap_windows_exclude_daily_baseline_and_research_buckets(symbol):
    missing = GAPS["missing_minutes_ist"][symbol]
    complete = full_day(SESSION)
    rows = [row for row in complete if row[0][11:16] not in missing]
    frames = stock_candle_days(parse_minutes(rows, SESSION.opens_at, SESSION.closes_at), CONFIG.market)
    frame, = frames
    assert [at.strftime("%H:%M") for at in frame.missing_minutes()] == missing
    assert daily_stats(frames, CONFIG.stocks.sip_range_minutes) == []
    assert frame.close == Decimal("201.2")  # the last observed regular close remains valid

    intact, = build_days(complete, market=CONFIG.market)
    day, = build_days(rows, market=CONFIG.market)
    assert len(day.minutes) == len(complete) - len(missing)
    for width, key in ((5, "skipped_five_minute_bar_ends_ist"), (15, "skipped_fifteen_minute_bar_ends_ist")):
        removed = {bar.clock for bar in intact.bars(width)} - {bar.clock for bar in day.bars(width)}
        assert sorted(removed) == GAPS[key][symbol]

    before = parse_timestamp(f"{DAY}T{missing[0]}:00+05:30") - MINUTE
    index = next(i for i, bar in enumerate(day.minutes) if bar.clock == before.strftime("%H:%M"))
    assert simulate([day], lambda _: [Plan(index, 1, 190)], CONFIG, risk=500, slot=50_000) == []
    assert simulate([intact], lambda _: [Plan(index, 1, 190)], CONFIG, risk=500, slot=50_000)[0]["reason"] == "time"


def test_stock_backtest_reports_affected_windows_and_excludes_unverifiable_pnl():
    prior = TradingSession.from_config(DAY - timedelta(days=1), CONFIG.market, segment="cash")
    close = [(prior.closes_at - MINUTE).isoformat(), 199, 199, 199, 199, 50_000]
    complete = full_day(SESSION)
    data = {symbol: [close, *[row for row in complete if row[0][11:16] not in gaps]]
            for symbol, gaps in GAPS["missing_minutes_ist"].items()}
    result = backtest(FUNDED_FIXTURE, data, DAY)
    assert result["trades"] == result["stats"] == []
    assert {item["symbol"]: [stamp[11:16] for stamp in item["missing_minutes"]] for item in result["skipped_windows"]} == GAPS["missing_minutes_ist"]
    assert {item["symbol"]: item["unverified_at"][11:16] for item in result["unverified_trades"]} == {
        "FORCEMOT": "12:21", "PGEL": "10:52"}
    control = backtest(FUNDED_FIXTURE, {"VOLTAS": [close, *complete]}, DAY)
    assert [row["exit_reason"] for row in control["trades"]] == ["square_off"]


@pytest.mark.parametrize("symbol", GAPS["missing_minutes_ist"])
def test_live_vwap_warmup_does_not_bridge_a_missing_stock_sample(tmp_path, symbol):
    trader = StockPaperTrader(CONFIG, tmp_path / "paper.sqlite3")
    start = parse_timestamp(f"{DAY}T{GAPS['missing_minutes_ist'][symbol][0]}:00+05:30")
    universe = {"1": symbol, "2": "CONTROL"}

    def quote(token, at):
        return {"symbolToken": token, "exchFeedTime": at.isoformat(), "ltp": 200, "open": 200, "high": 201,
                "low": 199, "close": 198, "avgPrice": 200, "tradeVolume": 10_000,
                "depth": {"buy": [{"price": 199.95, "quantity": 1000}], "sell": [{"price": 200, "quantity": 1000}]}}

    previous, current = start - MINUTE, start + MINUTE
    trader.on_scan([quote("1", previous), quote("2", previous)], universe, previous)
    trader.on_scan([quote("2", start)], universe, start)
    trader.on_scan([quote("1", current), quote("2", current)], universe, current)
    assert len(trader._series["1"].samples) == 1
    assert len(trader._series["2"].samples) == 3


def test_pgel_gap_and_go_cannot_confirm_the_first_break_after_an_incomplete_bar():
    historical = []
    day = DAY - timedelta(days=1)
    while len(historical) < s6_gap_and_go.HISTORY:
        if day.weekday() < 5:
            historical.append(TradingSession.from_config(day, CONFIG.market, segment="cash"))
        day -= timedelta(days=1)
    rows = [[row[0], 100, 100, 100, 100, 1000]
            for session in reversed(historical) for row in full_day(session)]
    today = []
    for stamp, *_ in full_day(SESSION):
        clock = stamp[11:16]
        price = 103 if clock >= "10:55" else 102
        today.append([stamp, price, price, price, price, 2000])
    intact = build_days(rows + today, market=CONFIG.market)[-1]
    plan, = s6_gap_and_go.plans(intact)
    assert intact.minutes[plan.at].clock == "10:59"
    partial = [row for row in today if row[0][11:16] not in GAPS["missing_minutes_ist"]["PGEL"]]
    assert s6_gap_and_go.plans(build_days(rows + partial, market=CONFIG.market)[-1]) == []
