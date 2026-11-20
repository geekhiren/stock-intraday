from datetime import date, timedelta
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from orderbot.broker.angel_client import RateLimitError, Session
from orderbot.broker.instruments import parse_master
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data.candle_builder import TradingSession
from orderbot.data.movers import MoversScanner, quote_freshness


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")
FIXTURE = json.loads((ROOT / "tests/fixtures/nse_sessions.json").read_text())


def test_dated_session_cutoffs_and_expiring_index_exception():
    for item in FIXTURE["schedules"]:
        day = date.fromisoformat(item["day"])
        cash = TradingSession.from_config(day, CONFIG.market, segment="cash")
        derivatives = TradingSession.from_config(day, CONFIG.market, segment="derivatives")
        index = TradingSession.from_config(day, CONFIG.market, segment="derivatives", index_expiry=day)
        assert cash.closes_at.strftime("%H:%M") == item["cash_close"]
        assert derivatives.closes_at.strftime("%H:%M") == item["fo_close"]
        assert index.closes_at.strftime("%H:%M") == item["index_expiry_close"]
        assert cash.contains(cash.closes_at - timedelta(seconds=1)) and not cash.contains(cash.closes_at)
        if item["auction_end"]:
            assert CONFIG.market.cash_phase(cash.closes_at) == "closing_auction"
            assert CONFIG.market.cash_phase(parse_timestamp(f'{day}T{item["auction_end"]}:00+05:30')) == "closed"


def test_quote_freshness_uses_exchange_time_and_segment():
    times = FIXTURE["quotes"]
    now = parse_timestamp(times["now"])
    cash = TradingSession.from_config(now.date(), CONFIG.market, segment="cash")
    limit = CONFIG.reporting.rest_quote_max_age_seconds
    assert quote_freshness(times["fresh"], now, cash, limit)["state"] == "fresh"
    assert quote_freshness(times["stale"], now, cash, limit)["state"] == "stale"
    assert quote_freshness("01-Jan-1970 05:30:00", now, cash, limit)["state"] == "unknown"
    assert quote_freshness(times["fresh"], parse_timestamp(times["after_cash_close"]), cash, limit)["state"] == "market_closed"


def test_dashboard_probe_uses_fresh_broker_reads_and_compares_local_candles(tmp_path, monkeypatch):
    from orderbot.data import candle_store

    now = parse_timestamp("2026-11-18T09:20:12+05:30")
    client = Mock()
    scanner = MoversScanner(CONFIG, tmp_path, client=client, clock=lambda: now)
    master = parse_master(json.loads((ROOT / "tests/fixtures/instruments.json").read_text()), CONFIG.market.underlying)
    scanner._master, scanner._universe, scanner._day = master, {"1": "RELIANCE"}, now.date()
    scanner._session = Session("fake-jwt", "fake-refresh", "fake-feed")
    monkeypatch.setenv("ORDERBOT_CANDLE_STORE", str(tmp_path / "local"))

    def quote(credentials, session, exchange, tokens, *, check):
        return [{"symbolToken": token, "ltp": 22150, "exchFeedTime": now.isoformat()} for token in tokens]

    def history(credentials, session, exchange, token, start, end, *, check):
        count = CONFIG.paper_trial.market_verification_minutes - (exchange == "NFO")
        return [[(start + timedelta(minutes=index)).isoformat(), 100, 101, 99, 100, 10] for index in range(count)]

    candle_store.save("RELIANCE", [["2026-11-18T09:15:00+05:30", 100, 101, 99, 101, 10]])
    client.quotes.side_effect = quote
    client.historical_minutes.side_effect = history
    result = scanner.verify_market_data()
    assert result["mode"] == "PAPER" and result["risk_configured"] is False
    assert all(quote["state"] == "fresh" for quote in result["quotes"].values())
    assert result["candles"]["RELIANCE"]["local_mismatches"] == ["2026-11-18T09:15:00+05:30"]
    assert result["candles"][master.front_future(now.date()).symbol]["missing"] == ["2026-11-18T09:19:00+05:30"]
    assert client.quotes.call_count == client.historical_minutes.call_count == 2
    client.login.assert_not_called()
    scanner.clock = lambda: parse_timestamp("2026-11-18T17:30:00+05:30")
    with pytest.raises(ValueError, match="regular sessions"):
        scanner.verify_market_data()
    assert client.quotes.call_count == 2


@pytest.mark.parametrize("blocked_phase, quote_count, history_count, candle_count", [
    ("derivatives_quotes", 2, 0, 0),
    ("derivatives_candles", 4, 2, 1),
])
def test_probe_keeps_partial_results_and_does_not_request_during_cooldown(
        tmp_path, monkeypatch, blocked_phase, quote_count, history_count, candle_count):
    now = parse_timestamp("2026-11-18T09:26:12+05:30")
    client = Mock()
    scanner = MoversScanner(CONFIG, tmp_path, client=client, clock=lambda: now)
    scanner._master = parse_master(json.loads((ROOT / "tests/fixtures/instruments.json").read_text()), CONFIG.market.underlying)
    scanner._universe, scanner._day = {"1": "RELIANCE"}, now.date()
    scanner._session = Session("fake-jwt", "fake-refresh", "fake-feed")
    monkeypatch.setenv("ORDERBOT_CANDLE_STORE", str(tmp_path / "local"))
    candle_end = parse_timestamp("2026-11-18T09:20:00+05:30")
    throttle = RateLimitError("Broker cooldown", retry_after_seconds=90)

    def quotes(credentials, session, exchange, tokens, *, check):
        if exchange == "NFO" and blocked_phase == "derivatives_quotes":
            raise throttle
        return [{"symbolToken": token, "ltp": 22150, "exchFeedTime": now.isoformat()} for token in tokens]

    def history(credentials, session, exchange, token, start, end, *, check):
        assert end == candle_end
        if exchange == "NFO":
            raise throttle
        return [[start.isoformat(), 100, 101, 99, 100, 10]]

    client.quotes.side_effect = quotes
    client.historical_minutes.side_effect = history
    report = scanner.verify_market_data(candle_end=candle_end)
    assert len(report["quotes"]) == quote_count
    assert all(quote["state"] == "fresh" for quote in report["quotes"].values())
    assert client.historical_minutes.call_count == history_count
    assert len(report["candles"]) == candle_count
    assert report["cooldown"]["stage"] == blocked_phase
    assert report["cooldown"]["retry_after_seconds"] == 90
    assert report["candle_window"]["end_exclusive"] == candle_end.isoformat()
    assert report["pending_candles"] == (["cash", "derivatives"] if not candle_count else ["derivatives"])
