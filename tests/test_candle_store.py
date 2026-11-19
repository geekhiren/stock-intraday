from datetime import date
from pathlib import Path

from orderbot.broker.history import HistoryCache
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp

from orderbot.data import candle_store


def row(stamp, close, volume=10):
    return [stamp, close, close, close, close, volume]


def test_store_merges_by_minute_splits_by_month_and_loads_a_date_range(tmp_path):
    first = [row("2026-10-30T15:29:00+05:30", 100), row("2026-11-02T09:15:00+05:30", 101), row("2026-11-02T09:16:00+05:30", 102)]
    assert candle_store.save("M&M", first, tmp_path) == 3
    # A second download overlaps one minute (corrected price) and adds a later one.
    candle_store.save("M&M", [row("2026-11-02T09:16:00+05:30", 102.5), row("2026-11-03T09:15:00+05:30", 103)], tmp_path)
    assert sorted(path.name for path in (tmp_path / "M_and_M").iterdir()) == ["2026-10.json.gz", "2026-11.json.gz"]
    assert candle_store.symbols(tmp_path) == ["M&M"]
    assert candle_store.coverage("M&M", tmp_path) == ("2026-10-30", "2026-11-03", 4)
    loaded = candle_store.load("M&M", date(2026, 11, 2), date(2026, 11, 2), tmp_path)
    assert [(item[0][11:16], item[4]) for item in loaded] == [("09:15", 101), ("09:16", 102.5)]
    assert candle_store.load("OTHER", date(2026, 11, 2), date(2026, 11, 2), tmp_path) == []


def test_stock_history_uses_local_first_and_caches_only_uncovered_windows(tmp_path, monkeypatch):
    from orderbot.data.stock_history import StockHistory

    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    monkeypatch.setenv("ORDERBOT_CANDLE_STORE", str(tmp_path / "scraped"))
    start = parse_timestamp("2026-11-19T15:14:00+05:30")
    end = parse_timestamp("2026-11-20T09:17:00+05:30")
    candle_store.save("RELIANCE", [row(start.isoformat(), 1375), row("2026-11-19T15:20:00+05:30", 9999)])
    history = StockHistory(config, HistoryCache(config.data, tmp_path))
    called = []

    def fetch(since, until):
        called.append((since.strftime("%d %H:%M"), until.strftime("%d %H:%M")))
        return [row("2026-11-20T09:15:00+05:30", 1376), row("2026-11-20T09:16:00+05:30", 1377)]

    result = history.get("RELIANCE", "1", start, end, fetch)
    assert called == [("20 09:15", "20 09:17")]
    assert [bar.close for bar in result] == [1375, 1376, 1377]
    assert history.get("RELIANCE", "1", start, end, None) == result
    assert len(called) == 1
