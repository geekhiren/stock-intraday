from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.execution.exit_store import ExitStore
from orderbot.reporting.alerts import AlertOutbox
from orderbot.strategy.stock_paper import StockPaperTrader, intraday_charges


CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
UNIVERSE = {"1": "ALPHA", "2": "BETA"}
MINUTE = timedelta(minutes=1)


def quote(token, ltp, *, open_, high, low, close, vwap, volume=1_000_000, spread="0.05", depth=1000):
    bid = Decimal(str(ltp)) - Decimal(spread)
    return {"symbolToken": token, "ltp": ltp, "open": open_, "high": high, "low": low, "close": close, "avgPrice": vwap,
            "tradeVolume": volume, "depth": {"buy": [{"price": float(bid), "quantity": depth}],
                                             "sell": [{"price": ltp, "quantity": depth}]}}


def alpha(ltp, **changes):
    return quote("1", ltp, **({"open_": 200, "high": max(201, ltp), "low": 199.95, "close": 199, "vwap": 200.4} | changes))


def test_intraday_charges_match_hand_calculation():
    # 500 shares bought at 200.00 and sold at 201.50: brokerage 40.00, STT 25.19, exchange 6.16, SEBI 0.20,
    # stamp 3.00, GST 8.35.
    assert intraday_charges(Decimal("200"), Decimal("201.5"), 500, CONFIG) == Decimal("82.90")
    # 0.1% of a small order is below the ₹5 minimum on each side.
    assert intraday_charges(Decimal("100"), Decimal("100"), 10, CONFIG) >= Decimal("10")


def test_opening_range_breakout_sizes_fills_and_books_net_to_the_paisa(tmp_path):
    db = tmp_path / "stocks.sqlite3"
    trader = StockPaperTrader(CONFIG, db)
    at = parse_timestamp("2026-11-30T09:30:10+05:30")
    trader.on_scan([alpha(200.5), {"symbolToken": "2", "ltp": 0}], UNIVERSE, at)
    assert trader.status == "Watching opening-range breakouts" and trader.snapshot(at)["open"] == []

    trader.on_scan([alpha(201.2)], UNIVERSE, at + MINUTE)  # clears 201 × 1.0005 with the open at the low and a gap up
    position, = trader.snapshot(at + MINUTE)["open"]
    # ₹500 of risk over a 1.25 stop would buy 400 shares; one of two ₹50,000 position slots caps it at 248. The target is 1.5R.
    assert (position["symbol"], position["strategy"], position["side"], position["quantity"]) == ("ALPHA", "orb", "long", 248)
    assert (position["entry"], position["stop"], position["target"]) == ("201.20", "199.95", "203.08")

    trader.on_scan([alpha(202.0)], UNIVERSE, at + 2 * MINUTE)
    assert trader.status == "Managing the open position"
    trader.on_scan([alpha(203.15)], UNIVERSE, at + 3 * MINUTE)  # bid 203.10 is through the target
    book = StockPaperTrader(CONFIG, db).snapshot(at + 3 * MINUTE)  # a restarted process reads the same journal
    trade, = book["trades"]
    # Gross 471.20 less brokerage 40.00, STT 12.59, exchange 3.08, SEBI 0.10, stamp 1.50, GST 7.77.
    assert (trade["exit"], trade["exit_reason"], trade["net_paise"]) == ("203.10", "target", 40616)
    assert book["open"] == [] and book["net_paise"] == 40616 and book["risk_paise"] == 50000
    assert [event["kind"] for event in AlertOutbox(db).recent(2)] == ["stock_exit", "stock_entry"]

    # The breakout is spent and the opening-range trade uses the session, so later setups do not enter.
    trader.on_scan([alpha(204.0)], UNIVERSE, at + 20 * MINUTE)
    trader.on_scan([alpha(204.0)], UNIVERSE, parse_timestamp("2026-11-30T10:30:00+05:30"))
    assert trader.status.startswith("Done for today") and trader.snapshot(at)["open"] == []


def test_short_breakout_stops_out_and_stop_trading_blocks_entries(tmp_path):
    db = tmp_path / "stocks.sqlite3"
    trader = StockPaperTrader(CONFIG, db)
    at = parse_timestamp("2026-11-30T09:30:10+05:30")
    beta = lambda ltp, **changes: quote("2", ltp, **({"open_": 500, "high": 500.1, "low": min(498, ltp), "close": 503,  # noqa: E731
                                                       "vwap": 499} | changes))
    trader.on_scan([beta(499.0), alpha(200.5, spread="0.5")], UNIVERSE, at)
    # ALPHA breaks out too but its spread is too wide; BETA breaks the low with the open at the high and a gap down.
    trader.on_scan([beta(497.5, low=497.5), alpha(201.2, spread="0.5")], UNIVERSE, at + MINUTE)
    position, = trader.snapshot(at + MINUTE)["open"]
    assert (position["symbol"], position["side"], position["entry"], position["stop"]) == ("BETA", "short", "497.45", "500.43")
    assert position["quantity"] == 100  # the 2.65 stop widens to the 0.6% minimum and the ₹50,000 slot caps the size

    ExitStore(db).latch(at + 2 * MINUTE, "dashboard_stop")
    trader.on_scan([beta(497.0, low=497.0)], UNIVERSE, at + 2 * MINUTE)
    trade, = trader.snapshot(at + 2 * MINUTE)["trades"]
    assert trade["exit_reason"] == "kill_switch" and trade["exit"] == "497.00"
    assert trader.status.startswith("Stopped for today")


def test_late_start_skips_the_opening_range(tmp_path):
    trader = StockPaperTrader(CONFIG, tmp_path / "stocks.sqlite3")
    at = parse_timestamp("2026-11-30T09:40:00+05:30")
    trader.on_scan([alpha(200.5)], UNIVERSE, at)
    trader.on_scan([alpha(201.2)], UNIVERSE, at + MINUTE)
    assert trader.status.startswith("Opening range missed") and trader.snapshot(at)["open"] == []


def test_manual_trade_stats_and_backtest_reuse_the_live_rules(tmp_path):
    from orderbot.strategy.stock_backtest import backtest

    trader = StockPaperTrader(CONFIG, tmp_path / "stocks.sqlite3")
    at = parse_timestamp("2026-11-30T11:00:00+05:30")
    assert trader.manual_entry("ALPHA", "long", at).startswith("No fresh quote")
    trader.on_scan([alpha(200.0)], UNIVERSE, at)
    assert trader.manual_entry("ALPHA", "long", at) is None
    assert trader.manual_entry("ALPHA", "long", at) == "ALPHA already has an open position."
    position, = trader.snapshot(at)["open"]
    # With no ATR yet the stop is the 0.6% minimum (1.20), a ₹50,000 slot buys 250 shares and the target is 2R.
    assert (position["strategy"], position["quantity"], position["stop"], position["target"]) == ("manual", 250, "198.80", "202.40")
    trader.on_scan([alpha(200.5)], UNIVERSE, at + MINUTE)
    assert trader.manual_exit(position["trade_id"], at + MINUTE) is None
    book = trader.snapshot(at + MINUTE)
    assert book["open"] == [] and book["trades"][0]["exit_reason"] == "manual"
    assert [(line["strategy"], line["trades"], line["wins"], line["win_rate"]) for line in book["stats"]] == [
        ("manual", 1, 1, 100.0), ("all", 1, 1, 100.0)]

    # The same breakout as the live test, replayed from one-minute candles: a prior day for the previous close,
    # a 09:15-09:29 range of 199.95-201 with the open at the low, a break at 09:30 and the target hit at 09:32.
    def candle(day, clock, opened, high, low, close):
        return [f"2026-{day}T{clock}:00+05:30", opened, high, low, close, 10_000]
    rows = [candle("11-27", "15:29", 199, 199, 199, 199)]
    rows += [candle("11-30", f"09:{minute}", 200, 201, 199.95, 200.5) for minute in range(15, 30)]
    rows += [candle("11-30", "09:30", 200.5, 201.2, 200.5, 201.2), candle("11-30", "09:31", 201.2, 202, 201.2, 202),
             candle("11-30", "09:32", 202, 203.2, 202, 203.15)]
    result = backtest(CONFIG, {"ALPHA": rows}, at.date())
    trade, = result["trades"]
    assert (trade["strategy"], trade["side"], trade["quantity"], trade["entry"], trade["exit"], trade["exit_reason"],
            trade["net_paise"]) == ("orb", "long", 248, "201.20", "203.08", "target", 40120)
    assert result["sessions"] == 1 and result["stats"][-1]["win_rate"] == 100.0


def test_stocks_in_play_ranks_by_opening_volume_and_holds_to_square_off(tmp_path):
    from orderbot.strategy.stock_paper import baseline, daily_stats

    # Two sessions of two candles: true ranges 4 and 6 (the second gaps above the first close), opening volumes 100 and 300.
    rows = [["2026-11-26T09:15:00+05:30", 100, 102, 99, 101, 100], ["2026-11-26T09:20:00+05:30", 101, 103, 100, 102, 50],
            ["2026-11-27T09:15:00+05:30", 104, 108, 104, 107, 300], ["2026-11-27T09:21:00+05:30", 107, 107, 105, 106, 70]]
    stats = daily_stats(rows, CONFIG.market.session_open, 5)
    assert stats == [("2026-11-26", 4.0, 100, 102.0, 150), ("2026-11-27", 6.0, 300, 106.0, 370)]
    assert baseline(stats, "2026-11-30", 2) == (Decimal("5.0"), Decimal("200.0")) and baseline(stats, "2026-11-27", 2) is None

    trader = StockPaperTrader(CONFIG, tmp_path / "stocks.sqlite3")
    at = parse_timestamp("2026-11-30T09:20:10+05:30")
    trader.set_baselines(at.date(), {"ALPHA": (Decimal("4"), Decimal("100000")), "BETA": (Decimal("9"), Decimal("900000"))})
    play = lambda ltp, **changes: quote("1", ltp, **({"open_": 200, "high": 201.2, "low": 199.8, "close": 199, "vwap": 200.5,  # noqa: E731
                                                      "volume": 300_000} | changes))
    quiet = quote("2", 501, open_=500, high=502, low=499, close=498, vwap=500.5, volume=450_000)  # half its usual volume
    trader.on_scan([play(201.0), quiet], UNIVERSE, at)
    assert trader.status == "Watching 1 stock in play"
    trader.on_scan([play(201.5, high=201.5), quiet], UNIVERSE, at + MINUTE)  # above the 5-minute high in the direction of the first move
    position, = trader.snapshot(at + MINUTE)["open"]
    # Stop is half the 14-day ATR (2.00) below the fill and there is no target.
    assert (position["symbol"], position["strategy"], position["side"], position["quantity"]) == ("ALPHA", "sip", "long", 248)
    assert (position["entry"], position["stop"], position["target"]) == ("201.50", "199.50", None)
    trader.on_scan([play(209.0, high=209.0), quiet], UNIVERSE, at + 2 * MINUTE)
    assert trader.snapshot(at)["open"] and trader.status == "Managing the open position"
    trader.on_scan([play(206.0, high=209.0)], UNIVERSE, parse_timestamp("2026-11-30T15:10:00+05:30"))
    trade, = trader.snapshot(at)["trades"]
    assert (trade["exit_reason"], trade["exit"]) == ("square_off", "205.95") and trade["net_paise"] > 0


def test_fast_watch_exits_between_scans_and_never_enters(tmp_path):
    trader = StockPaperTrader(CONFIG, tmp_path / "stocks.sqlite3")
    at = parse_timestamp("2026-11-30T11:00:00+05:30")
    trader.manage([alpha(201.2)], UNIVERSE, at)
    assert trader.held() == []
    trader.on_scan([alpha(200.0)], UNIVERSE, at)
    assert trader.manual_entry("ALPHA", "long", at) is None
    assert [(trade["symbol"], trade["stop"]) for trade in trader.held()] == [("ALPHA", "198.80")]
    trader.manage([alpha(199.5)], UNIVERSE, at + timedelta(seconds=5))  # above the stop
    assert len(trader.held()) == 1
    trader.manage([alpha(198.8)], UNIVERSE, at + timedelta(seconds=10))  # bid 198.75 is through the stop
    trade, = trader.snapshot(at)["trades"]
    assert trader.held() == [] and (trade["exit_reason"], trade["exit"]) == ("stop", "198.75")


def test_watchlist_scores_indicator_exits_and_daily_report(tmp_path):
    from orderbot.reporting.daily_report import build_report, render_markdown
    from orderbot.strategy.stock_paper import watch_scores

    # (day, true range, opening volume, close, volume): BUSY moves more, traded 3x its usual volume and rose over five sessions.
    quiet = [(f"2026-11-{day:02d}", 1.0, 10, 100.0, 1000) for day in range(10, 17)]
    busy = [(f"2026-11-{day:02d}", 4.0, 10, 100.0 + 2 * index, 1000) for index, day in enumerate(range(10, 16))]
    busy.append(("2026-11-16", 4.0, 10, 112.0, 9000))
    ranked = watch_scores({"QUIET": quiet, "BUSY": busy}, "2026-11-17", 7, 1)
    assert [row["symbol"] for row in ranked] == ["BUSY"] and ranked[0]["score"] == 100.0
    assert (ranked[0]["relative_volume"], ranked[0]["change_5d"]) == (4.2, 9.8)

    db = tmp_path / "stocks.sqlite3"
    config = CONFIG.model_copy(update={"stocks": CONFIG.stocks.model_copy(update={"exit_on_vwap_cross": True})})
    trader = StockPaperTrader(config, db)
    at = parse_timestamp("2026-11-30T11:00:00+05:30")
    trader.on_scan([alpha(200.6)], UNIVERSE, at)  # VWAP is 200.4
    assert trader.manual_entry("ALPHA", "long", at) is None
    trader.manage([alpha(200.2)], UNIVERSE, at + timedelta(seconds=5))  # the fast watch ignores indicator exits
    assert len(trader.held()) == 1
    trader.on_scan([alpha(200.2)], UNIVERSE, at + MINUTE)  # back below VWAP on the minute scan
    trade, = trader.snapshot(at + MINUTE)["trades"]
    assert trade["exit_reason"] == "vwap_cross"

    report = build_report(db, at.date())
    assert [(row["symbol"], row["exit_reason"]) for row in report["stock_trades"]] == [("ALPHA", "vwap_cross")]
    assert report["stock_net_paise"] == trade["net_paise"] < 0
    assert "## Stock intraday paper trades" in render_markdown(report) and "0 won of 1" in render_markdown(report)
