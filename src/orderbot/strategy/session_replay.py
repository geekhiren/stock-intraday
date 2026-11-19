from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count, groupby
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.broker.instruments import NSE_CASH, NSE_DERIVATIVES
from orderbot.broker.ws_client import (
    BOOK_START, LEVEL, SNAP_BYTES, SNAP_HEADER, SNAP_QUOTE, SNAP_TRADE_DATA, SNAP_TRADE_DATA_START,
    SNAP_VOLUME, SNAP_VOLUME_START, decode_snap,
)
from orderbot.core.config import Config
from orderbot.core.ist_time import IST, parse_timestamp
from orderbot.core.money import PAISA
from orderbot.data.candle_builder import MINUTE, Candle
from orderbot.data.replay import CandlePipeline
from orderbot.data.tick_archive import ArchiveRecord, TickArchive, read_archive
from orderbot.execution.exit_engine import ExitEngine
from orderbot.execution.exit_rules import ExitFrame
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.reporting.alerts import AlertOutbox
from orderbot.reporting.daily_report import build_report
from orderbot.risk.kill_switch import Health

from .base import StrategyName
from .iv_store import IVStore
from .opening_range import StrategyB
from .replay import load_strategy_fixture
from .runner import SessionStrategies
from .vwap_rsi import StrategyA


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MILLISECOND = timedelta(milliseconds=1)


def _snapshot(at, token, sequence, price, volume, *, exchange=NSE_DERIVATIVES, bid=None, ask=None, units=0, oi=0):
    packet = bytearray(SNAP_BYTES)
    stamp = (at - EPOCH) // MILLISECOND
    SNAP_HEADER.pack_into(packet, 0, SNAP_QUOTE, exchange, token.encode("ascii"), sequence, stamp, int(price / PAISA))
    SNAP_VOLUME.pack_into(packet, SNAP_VOLUME_START, volume)
    SNAP_TRADE_DATA.pack_into(packet, SNAP_TRADE_DATA_START, stamp, oi)
    if bid is not None and ask is not None:
        LEVEL.pack_into(packet, BOOK_START, 1, units, int(bid / PAISA), 1)
        LEVEL.pack_into(packet, BOOK_START + LEVEL.size, 0, units, int(ask / PAISA), 1)
    return ArchiveRecord(at, bytes(packet))


def _records(base, fixture, config):
    session = base.session
    sequence = count()
    volume = 0
    prior = session.opens_at - MINUTE
    overrides = fixture["futures_overrides"] | {"0": fixture["opening_futures"]}
    yield _snapshot(prior, base.future_token, next(sequence), base.candles[0].open, volume)
    for index, candle in enumerate(base.candles):
        if str(index) in overrides:
            opening, high, low, close, quantity = overrides[str(index)]
            candle = Candle(candle.at, Decimal(opening), Decimal(high), Decimal(low), Decimal(close), quantity)
        prices = (candle.open, candle.high, candle.low, candle.close)
        per_tick, remainder = divmod(candle.volume, len(prices))
        if not per_tick:
            raise ValueError("A replay fixture needs positive traded volume for all four OHLC ticks.")
        for offset, price in enumerate(prices):
            volume += per_tick + (remainder if offset == len(prices) - 1 else 0)
            stamp = candle.at + offset * MILLISECOND
            yield _snapshot(stamp, base.future_token, next(sequence), price, volume)
        yield ArchiveRecord(candle.at + MINUTE + timedelta(seconds=config.data.close_grace_seconds), None)

    spot = Decimal(fixture["opening_spot"])
    spot_token = base.master.spot.token
    yield _snapshot(session.opens_at, spot_token, next(sequence), spot, 0, exchange=NSE_CASH)
    contracts = {item.token for item in base.master.options}
    for row in fixture["option_events"]:
        stamp = parse_timestamp(row["at"])
        if not session.opens_at <= stamp < session.closes_at or row["token"] not in contracts:
            raise ValueError("A replay option quote is outside the session or absent from the instrument master.")
        yield _snapshot(stamp, spot_token, next(sequence), spot, 0, exchange=NSE_CASH)
        yield _snapshot(stamp, row["token"], next(sequence), Decimal(row["last"]), fixture["option_volume"],
                        bid=Decimal(row["bid"]), ask=Decimal(row["ask"]), units=fixture["book_quantity"],
                        oi=fixture["option_open_interest"])

    for boundary in (config.strategy_b.entry_window_end, config.exits.square_off_time, config.exits.backstop_time):
        yield ArchiveRecord(datetime.combine(session.day, boundary, tzinfo=IST), None)


def _drive_session(base, config, day_records, executor, exit_engine, strategies, clock, *, previous_close):
    pipeline = CandlePipeline(base.session, base.future_token, config.data, started_at=day_records[0].received_at)
    quotes = {}
    opening_spot = last_spot = None
    exposed = False
    halted = False
    health = Health()
    boundaries = (config.strategy_b.entry_window_end, config.exits.square_off_time, config.exits.backstop_time)
    routes = Counter()
    signals = Counter({"ohl": 0, "vwap": 0})
    range_detail = None
    for stamp, group in groupby(day_records, key=lambda item: item.received_at):
        clock[0] = stamp
        closed, ticks = [], []
        for record in group:
            closed.extend(pipeline.step(record))
            if record.raw is not None:
                ticks.append(decode_snap(record.raw))

        for tick in ticks:
            if tick.exchange_type == NSE_CASH and tick.token == base.master.spot.token:
                last_spot = tick
                if opening_spot is None and base.session.opens_at <= tick.exchange_at < base.session.opens_at + MINUTE:
                    opening_spot = tick.price
            elif tick.token == base.future_token and tick.exchange_at >= base.session.opens_at:
                if exposed:
                    exposed = not exit_engine.step(ExitFrame(stamp, "tick", underlying=tick.price), quotes)
                else:
                    exit_engine.kill_switch.observe_underlying(stamp, tick.price)
                    if not halted and exit_engine.kill_switch.evaluate(stamp, health, position_opened_at=None):
                        exposed = not exit_engine.step(ExitFrame(stamp, "timer"), quotes, health)
                        halted = True
            elif tick.token in exit_engine.contracts:
                quotes[tick.token] = OptionQuote.from_tick(tick, stamp)
                if exposed:
                    ltp = tick.price if tick.price > 0 else None
                    frame = ExitFrame(stamp, "tick", option_token=tick.token, option_ltp=ltp)
                    exposed = not exit_engine.step(frame, quotes, health)

        boundary = any(stamp.time() == time for time in boundaries)
        if boundary:
            exposed = not exit_engine.step(ExitFrame(stamp, "timer"), quotes, health)
        for row in closed:
            if exposed:
                exposed = not exit_engine.step(ExitFrame(stamp, "candle", candle=row, timer_due=True), quotes, health)
            else:
                executor.poll(quotes)
            spot = last_spot.price if last_spot and (
                0 <= (stamp - last_spot.exchange_at).total_seconds() < config.options.quote_max_age_seconds
            ) else None
            decision, result = strategies.on_closed_candle(
                row, at=stamp, token=base.future_token, spot=spot, quotes=quotes,
                previous_close=previous_close, opening_spot=opening_spot,
            )
            routes[result.reason] += 1
            if result.order_tags:
                exposed = True
            if decision.signal is not None:
                signals[decision.strategy] += 1
            gate = decision.gates.get("opening_range")
            if gate is not None and gate.detail.get("frozen_at"):
                range_detail = gate.detail
    return routes, signals, range_detail


def replay_paper_session(path: Path, config: Config, *, mode: StrategyName, scenario: str, fixture_funding: bool) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    base = load_strategy_fixture(path.with_name(fixture["base_session"]), config)
    if scenario not in {"ohl_trade", "no_ohl_trade"} or mode not in {"ohl", "vwap"}:
        raise ValueError("Unknown paper session fixture scenario or strategy mode.")
    close_key = "previous_close" if scenario == "ohl_trade" else "fallback_previous_close"
    previous_close = Decimal(fixture[close_key])
    unfunded = config.risk.model_copy(update={"capital_inr": None, "risk_per_trade_pct": None})
    cfg = base.config.model_copy(update={
        "runtime": config.runtime.model_copy(update={"strategy": mode}),
        "risk": base.config.risk if fixture_funding else unfunded,
        "kill_switch": config.kill_switch.model_copy(update={"sentinel_path": Path(config.kill_switch.sentinel_path.name)}),
    })
    clock, ids = [base.session.opens_at], count()
    with TemporaryDirectory(prefix="orderbot-phase8-") as temporary:
        directory = Path(temporary)
        archive = directory / cfg.data.tick_archive_dir.name
        with TickArchive(archive, base.session.day, cfg.data.archive_batch_rows) as writer:
            for record in sorted(_records(base, fixture, cfg), key=lambda item: item.received_at):
                writer.append(record)
        records = tuple(read_archive(archive, base.session.day))
        journal = Journal(directory / "journal.sqlite3", cfg.costs)
        venue = PaperVenue(directory / "venue.sqlite3", cfg)
        executor = PaperExecutor(journal, venue, cfg, clock=lambda: clock[0], tag_factory=lambda: f"run{next(ids):06}")
        engine = ExitEngine(executor, {item.token: item for item in base.master.options}, directory)
        iv = IVStore(journal.path)
        for day, value in base.prior_iv:
            for right in ("CE", "PE"):
                iv.record(right, datetime.combine(day, cfg.market.session_close, tzinfo=IST), value)
        strategies = SessionStrategies(executor, StrategyA(cfg, base.master, base.trading_sessions, iv),
                                        StrategyB(cfg, base.master, base.trading_sessions))
        routes, signals, opening_range = _drive_session(base, cfg, records, executor, engine, strategies, clock,
                                                       previous_close=previous_close)
        check = executor.reconcile()
        ledger = journal.snapshot()
        report = build_report(journal.path, base.session.day)
        orders = [order.intent.to_dict() | {"state": order.state.value, **order.detail()} for order in ledger.orders]
        trades = []
        for trade in report["trades"]:
            decision = journal.decision_detail(trade["id"])
            strategy = next(order.intent.strategy for order in ledger.orders if order.intent.decision_id == trade["id"])
            trades.append(trade | {"strategy": strategy, "side": decision["side"], "right": decision["right"],
                                   "stop_plan": decision["execution_stop_plan"]})
        alerts = list(reversed(AlertOutbox(journal.path).recent(config.reporting.alert_history_limit)))
        return {
            "mode": "paper", "source": "synthetic_snap_quote_archive", "session": base.session.day.isoformat(),
            "strategy_mode": mode, "scenario": scenario, "fixture_funding": fixture_funding,
            "risk_inputs_in_config": config.risk.capital_inr is not None and config.risk.risk_per_trade_pct is not None,
            "archived_packets": sum(record.raw is not None for record in records),
            "closed_candles": len(strategies.history), "opening_range": opening_range,
            "routing": dict(routes), "signals": dict(signals), "orders": orders, "trades": trades, "report": report,
            "alerts": alerts, "exit_events": engine.store.events(),
            "confirmed_flat": check.ok and not ledger.positions and all(order.state.terminal for order in ledger.orders),
        }
