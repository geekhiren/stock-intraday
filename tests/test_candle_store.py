from datetime import date

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
