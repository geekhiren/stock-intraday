import json
import logging
import os
import struct
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from orderbot.broker.instruments import InstrumentError, load_master
from orderbot.broker.angel_client import Session
from orderbot.broker.ws_client import FeedError, FeedNotice, MarketFeed, decode_snap
from orderbot.core.config import load_config
from orderbot.core.ist_time import IST
from orderbot.data.candle_builder import Candle, CandleBuilder, TradingSession
from orderbot.data import capture
from orderbot.data.indicators import IndicatorEngine, floor_pivots
from orderbot.data.replay import AnalyzedBar, CandlePipeline, compare_candles, replay_archive
from orderbot.data.tick_archive import ArchiveRecord, TickArchive, read_archive


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")


def fixture(name):
    return json.loads((ROOT / "tests" / "fixtures" / (name + ".json")).read_text(encoding="utf-8"))


def packet(token, exchange_ms, trade_ms, sequence, price, volume, *, exchange=2, bids=(), asks=(), oi=0):
    payload = bytearray(379)
    struct.pack_into("<BB25sqqq", payload, 0, 3, exchange, token.encode(), sequence, exchange_ms, price)
    struct.pack_into("<q", payload, 67, volume)
    struct.pack_into("<qq", payload, 123, trade_ms, oi)
    for index, (flag, levels) in enumerate(((1, bids), (0, asks))):
        for rank in range(5):
            price, qty, orders = levels[rank] if rank < len(levels) else (0, 0, 0)
            struct.pack_into("<HqqH", payload, 147 + (index * 5 + rank) * 20, flag, qty, price, orders)
    return bytes(payload)


def record(at, token, sequence, price, volume, delay=0):
    trade_ms = int(at.timestamp() * 1000)
    return ArchiveRecord(
        at + timedelta(milliseconds=delay),
        packet(token, trade_ms + delay, trade_ms, sequence, price, volume),
    )


def cache_fixture_master(directory, day):
    master_dir = directory / CONFIG.data.instrument_cache_dir
    master_dir.mkdir(parents=True)
    (master_dir / f"instruments-{day}.json").write_text(
        json.dumps(fixture("instruments")), encoding="utf-8",
    )


def test_master_resolves_front_future_and_checks_strike_scaling(tmp_path):
    rows = fixture("instruments")
    day = date(2026, 11, 18)
    transport = Mock()
    transport.get.return_value.status_code = 200
    transport.get.return_value.json.return_value = rows
    master = load_master(CONFIG.data, CONFIG.market.underlying, day, tmp_path, transport=transport)
    assert master.front_future(day).token == "61471"
    assert master.front_future(date(2026, 11, 24)).token == "58875"
    assert master.spot.token == "99926000"
    option = master.options[0]
    assert (option.strike, option.lot_size, option.price_tick, option.raw_freeze_quantity) == (
        Decimal("22150"), 65, Decimal("0.05"), 1801
    )
    load_master(CONFIG.data, CONFIG.market.underlying, day, tmp_path, transport=transport)
    assert transport.get.call_count == 1
    rows[2]["strike"] = "22150"
    with pytest.raises(InstrumentError, match="strike"):
        load_master(CONFIG.data, CONFIG.market.underlying, date(2026, 11, 19), tmp_path, transport=transport)
    assert len(list((tmp_path / CONFIG.data.instrument_cache_dir).glob("*.json"))) == 1


def test_snap_fixture_decodes_correct_book_sides_and_trade_time():
    sample = fixture("market_snap")
    raw = packet(
        sample["token"], sample["exchange_epoch_ms"], sample["trade_epoch_ms"], sample["sequence"],
        sample["price_paise"], sample["cumulative_volume"], exchange=sample["exchange_type"],
        bids=sample["bids"], asks=sample["asks"], oi=sample["open_interest"],
    )
    tick = decode_snap(raw)
    assert tick.token == sample["token"]
    assert tick.exchange_at.isoformat() == "2026-11-18T09:15:00.300000+05:30"
    assert tick.trade_at.isoformat() == "2026-11-18T09:15:00.100000+05:30"
    assert tick.price == Decimal("22150")
    assert tick.open_interest == sample["open_interest"]
    assert tick.volume == 120
    assert [(level.price, level.quantity) for level in tick.bids] == [
        (Decimal("22149.90"), 65), (Decimal("22149.85"), 130),
    ]
    assert tick.asks[0].price == Decimal("22150.10")


def test_snap_quote_accepts_unavailable_index_depth_without_inventing_quotes():
    sample = fixture("market_snap")
    raw = bytearray(packet("99926000", sample["exchange_epoch_ms"], 0, sample["sequence"],
                           sample["price_paise"], 0, exchange=1))
    level = struct.Struct("<HqqH")
    for index in range(10):
        level.pack_into(raw, 147 + index * level.size, *sample["unavailable_depth_level"])
    tick = decode_snap(bytes(raw))
    assert tick.price == Decimal("22150")
    assert tick.bids == tick.asks == ()


def test_snap_quote_normalizes_last_trade_epoch_seconds():
    sample = fixture("market_snap")
    raw = packet(sample["token"], sample["exchange_epoch_ms"], sample["trade_epoch_seconds"],
                 sample["sequence"], sample["price_paise"], sample["cumulative_volume"])
    assert decode_snap(raw).trade_at.isoformat() == "2026-11-18T09:15:00+05:30"
    future_trade = packet(sample["token"], sample["exchange_epoch_ms"], sample["trade_epoch_seconds"] + 1,
                          sample["sequence"], sample["price_paise"], sample["cumulative_volume"])
    with pytest.raises(FeedError, match="timestamps"):
        decode_snap(future_trade)


def test_builder_waits_for_grace_orders_late_snapshots_and_ignores_duplicates():
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    opening = session.opens_at
    builder = CandleBuilder(
        session, CONFIG.data.close_grace_seconds, "61471", started_at=opening - timedelta(seconds=1),
    )
    events = [
        record(opening - timedelta(seconds=1), "61471", 0, 2215000, 15),
        record(opening + timedelta(seconds=1), "61471", 1, 2215000, 17),
        record(opening + timedelta(seconds=40), "61471", 3, 2214980, 23),
        record(opening + timedelta(seconds=20), "61471", 2, 2215030, 20, delay=21000),
        record(opening + timedelta(seconds=40), "61471", 4, 2214980, 23, delay=2000),
    ]
    for event in events:
        assert builder.step(event.received_at, decode_snap(event.raw)) == ()
    end = opening + timedelta(minutes=1)
    assert builder.step(end) == ()
    deadline = end + timedelta(seconds=CONFIG.data.close_grace_seconds)
    assert builder.step(deadline) == (
        Candle(opening, Decimal("22150"), Decimal("22150.30"), Decimal("22149.80"), Decimal("22149.80"), 8),
    )
    late = record(end - timedelta(seconds=1), "61471", 5, 2215040, 25, delay=10000)
    assert builder.step(late.received_at, decode_snap(late.raw)) == ()
    assert builder.late_dropped == 1


def test_mid_session_first_partial_minute_only_seeds_volume():
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    opening = session.opens_at
    builder = CandleBuilder(
        session, CONFIG.data.close_grace_seconds, "61471", started_at=opening + timedelta(minutes=30, seconds=10)
    )
    partial = record(opening + timedelta(minutes=30, seconds=20), "61471", 1, 2215000, 500)
    full = record(opening + timedelta(minutes=31, seconds=10), "61471", 2, 2215010, 505)
    assert not builder.step(partial.received_at, decode_snap(partial.raw))
    assert not builder.step(full.received_at, decode_snap(full.raw))
    bars = builder.step(opening + timedelta(minutes=32, seconds=2))
    assert len(bars) == 1
    assert bars[0].volume == 5


def test_open_without_a_volume_baseline_leaves_first_minute_unverified():
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    opening = session.opens_at
    builder = CandleBuilder(session, CONFIG.data.close_grace_seconds, "61471")
    first = record(opening + timedelta(seconds=3), "61471", 1, 2215000, 650)
    second = record(opening + timedelta(minutes=1, seconds=3), "61471", 2, 2215010, 656)
    assert not builder.step(first.received_at, decode_snap(first.raw))
    assert not builder.step(second.received_at, decode_snap(second.raw))
    assert builder.step(opening + timedelta(minutes=2, seconds=2))[0].volume == 6


def test_indicators_against_reference_values_and_session_anchor():
    spec = fixture("indicators")
    start = datetime(2026, 11, 18, 9, 15, tzinfo=IST)
    engine = IndicatorEngine(CONFIG.data.rsi_period, CONFIG.data.atr_period)
    computed = []
    for index, (opening, high, low, close, volume) in enumerate(spec["rows"]):
        bar = Candle(start + timedelta(minutes=index), *map(Decimal, (opening, high, low, close)), volume)
        computed.append(engine.update(bar))
    reference = spec["reference"]
    assert format(computed[9].vwap, ".8f") == reference["vwap_at_10"]
    assert computed[12].atr is None
    assert format(computed[13].atr, ".8f") == reference["atr_at_14"]
    assert format(computed[14].atr, ".8f") == reference["atr_at_15"]
    assert computed[13].rsi is None
    assert format(computed[14].rsi, ".4f") == reference["rsi_at_15"]
    next_day = start + timedelta(days=1)
    assert engine.update(Candle(next_day, Decimal(20), Decimal(22), Decimal(19), Decimal(19), 1)).vwap == 20
    assert floor_pivots(*map(Decimal, spec["previous_ohlc"])) == tuple(map(Decimal, spec["expected_pivots"]))


def test_full_synthetic_session_replays_identically(tmp_path):
    spec = fixture("replay_session")
    session = TradingSession.from_config(date.fromisoformat(spec["session"]), CONFIG.market)
    baseline = int(Decimal(spec["first_open"]) * 100)
    sequence = 0
    volume = spec["opening_volume_baseline"]
    expected = []
    through = session.closes_at + timedelta(seconds=CONFIG.data.close_grace_seconds)
    preopen = record(session.opens_at - timedelta(seconds=1), spec["signal_token"], 0, baseline, volume)
    pipeline = CandlePipeline(session, spec["signal_token"], CONFIG.data, started_at=preopen.received_at)
    actual = []
    archive_dir = tmp_path / CONFIG.data.tick_archive_dir
    with TickArchive(archive_dir, session.day, CONFIG.data.archive_batch_rows) as archive:
        archive.append(preopen)
        actual.extend(pipeline.step(preopen))
        for minute in range(spec["expected_minutes"]):
            at = session.opens_at + timedelta(minutes=minute)
            prices = [baseline + minute * spec["minute_step_paise"] + offset for offset in spec["offsets_paise"]]
            expected.append(Candle(at, *(Decimal(n) / 100 for n in (prices[0], max(prices), min(prices), prices[-1])), sum(spec["trade_quantities"])))
            for second, (price, quantity) in enumerate(zip(prices, spec["trade_quantities"]), start=1):
                sequence += 1
                volume += quantity
                entry = record(at + timedelta(seconds=second), spec["signal_token"], sequence, price, volume)
                archive.append(entry)
                actual.extend(pipeline.step(entry))
        terminal = ArchiveRecord(through, None)
        archive.append(terminal)
        actual.extend(pipeline.step(terminal))
    assert sum(row.bar.volume for row in actual) == spec["expected_volume"]
    assert actual[-1].bar.close == Decimal(spec["expected_last_close"])
    rows = list(read_archive(archive_dir, session.day))
    assert len(rows) == sequence + 2
    assert rows[-1] == terminal
    one = replay_archive(iter(rows), session, spec["signal_token"], CONFIG.data)
    two = replay_archive(read_archive(archive_dir, session.day), session, spec["signal_token"], CONFIG.data)
    assert one.to_json() == two.to_json()
    assert one.complete_capture
    assert not compare_candles(one.rows, expected, Decimal("0.10"))
    assert one.rows == tuple(actual)

    config_path = tmp_path / "config.yaml"
    config_path.write_bytes((ROOT / "config.yaml").read_bytes())
    cache_fixture_master(tmp_path, session.day)
    command = [sys.executable, str(ROOT / "run.py"), "--config", str(config_path), "--replay", str(session.day)]
    replayed = subprocess.run(
        command, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert replayed.stdout.strip() == one.to_json()


def test_disconnection_discards_incomplete_minutes():
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    at = session.opens_at
    records = [
        ArchiveRecord(at, None),
        record(at + timedelta(seconds=5), "61471", 1, 2215000, 20),
        ArchiveRecord(at + timedelta(seconds=30), None, discontinuity=True),
        record(at + timedelta(seconds=40), "61471", 2, 2215000, 25),
        record(at + timedelta(minutes=1, seconds=10), "61471", 3, 2215010, 32),
        ArchiveRecord(at + timedelta(minutes=2, seconds=2), None),
    ]
    replay = replay_archive(records, session, "61471", CONFIG.data)
    assert [row.bar.volume for row in replay.rows] == [7]
    assert replay.discontinuities == 1
    assert not replay.complete_capture


def test_comparison_allows_one_price_tick_and_requires_exact_volume():
    at = datetime(2026, 11, 18, 9, 15, tzinfo=IST)
    reference = Candle(at, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 50)
    tick = Decimal("0.10")
    actual = replace(reference, open=reference.open + tick, close=reference.close + 2 * tick, volume=49)
    indicators = IndicatorEngine(14, 14).update(actual)
    differences = compare_candles((AnalyzedBar(actual, indicators),), (reference,), tick)
    assert differences == ({
        "at": at.isoformat(),
        "close": {"capture": "100.20", "reference": "100"},
        "volume": {"capture": "49", "reference": "50"},
    },)


def test_market_feed_resubscribes_and_reports_stale():
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    token = "61471"
    times = [session.opens_at]
    streams = []

    class Socket:
        def __enter__(self):
            self.sent = []
            streams.append(self.sent)
            return self

        def __exit__(self, *_):
            pass

        def send(self, message):
            self.sent.append(message)

        def recv(self, timeout):
            times[0] += timedelta(seconds=timeout)
            raise TimeoutError

    def sleep(seconds):
        times[0] += timedelta(seconds=seconds)

    def now():
        return times[0]

    def monotonic():
        return (now() - session.opens_at).total_seconds()

    feed = MarketFeed(CONFIG.data, connect=Mock(side_effect=lambda *a, **kw: Socket()),
                      clock=now, monotonic=monotonic, sleep=sleep)
    events = list(feed.stream({}, {2: [token]}, (2, token), session))
    notices = [event.kind for event in events if isinstance(event, FeedNotice)]
    assert "stale" in notices
    assert notices[-1] == "unavailable"
    assert len(streams) > 1
    assert all(json.loads(sent[0])["params"]["tokenList"] == [{"exchangeType": 2, "tokens": [token]}] for sent in streams)


def test_capture_preserves_history_seed_for_mid_session_replay(tmp_path, monkeypatch):
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    cache_fixture_master(tmp_path, session.day)
    clock = [session.opens_at + timedelta(minutes=2, seconds=3)]
    monkeypatch.setattr(capture, "now_ist", lambda: clock[0])
    credentials = {
        CONFIG.broker.api_key_env: "test-key",
        CONFIG.broker.client_code_env: "test-client",
        CONFIG.broker.mpin_env: "0123",
        CONFIG.broker.totp_secret_env: "secret",
        CONFIG.broker.local_ip_env: "192.0.2.1",
        CONFIG.broker.public_ip_env: "203.0.113.10",
        CONFIG.broker.mac_address_env: "02:00:00:00:00:01",
    }
    for name in credentials:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("\n".join(f"{name}={value}" for name, value in credentials.items()), encoding="utf-8")
    broker = Mock()
    broker.login.return_value = Session("test-jwt", "test-refresh", "test-feed")
    broker.historical_minutes.side_effect = [[], fixture("history")["data"]]
    monkeypatch.setattr(capture.AngelClient, "for_workspace", lambda *_: broker)

    class FixtureFeed:
        def stream(self, headers, subscriptions, required, captured_session):
            assert headers["Authorization"] == "Bearer test-jwt"
            assert headers["x-api-key"] == "test-key"
            assert subscriptions == {1: ["99926000"], 2: ["61471", "42544"]}
            assert required == (2, "61471")
            assert captured_session == session
            yield FeedNotice("connected", clock[0])
            yield record(session.opens_at + timedelta(minutes=2, seconds=4), "61471", 1, 2215020, 45)
            yield record(session.opens_at + timedelta(minutes=3, seconds=5), "61471", 2, 2215030, 52)
            clock[0] = session.closes_at + timedelta(seconds=CONFIG.data.close_grace_seconds)

    monkeypatch.setattr(capture, "MarketFeed", lambda _: FixtureFeed())
    assert capture.capture_session(CONFIG, tmp_path, logging.getLogger("capture-test"), "test", ["42544"]) == 1
    report = capture.load_replay(CONFIG, tmp_path, session.day)
    assert [row.source for row in report.rows] == ["history", "history", "feed"]
    assert [row.bar.volume for row in report.rows] == [20, 20, 7]
    assert report.rows[-1].bar.at == session.opens_at + timedelta(minutes=3)
    assert not report.complete_capture
    assert broker.login.call_count == 1
