from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP
from functools import partial
import json
from pathlib import Path
from sqlite3 import Error as DatabaseError
from uuid import uuid4

from orderbot.broker.angel_client import AngelClient, LoginError, RequestError, read_credentials
from orderbot.broker.history import HistoryError, parse_minutes
from orderbot.broker.instruments import NSE_CASH, NSE_DERIVATIVES, load_master
from orderbot.broker.ws_client import FeedError, FeedNotice, MarketFeed, decode_snap
from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist, now_ist
from orderbot.data.candle_builder import MINUTE, Candle, TradingSession, minute_floor
from orderbot.data.indicators import IndicatorEngine, floor_pivots
from orderbot.data.replay import AnalyzedBar, CandlePipeline, compare_candles
from orderbot.data.tick_archive import ArchiveRecord, TickArchive
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.expiry_resolver import resolve_weekly_expiry
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.reporting.daily_report import build_report

from .iv_store import IVStore
from .opening_range import StrategyB
from .runner import SessionStrategies
from .vwap_rsi import StrategyA


class BrokerCandleConfirmations:
    """Accept consecutive complete minutes when distinct post-close REST reads agree."""

    def __init__(self, start: datetime, delay_seconds: float):
        self.next_at = as_ist(start)
        self.delay = timedelta(seconds=delay_seconds)
        self._prior: dict[datetime, Candle] = {}
        self._last_poll: datetime | None = None

    def accept(self, rows: tuple[Candle, ...], observed_at: datetime) -> tuple[Candle, ...]:
        now = as_ist(observed_at)
        if self._last_poll is not None and now <= self._last_poll:
            raise ValueError("Broker observations must be from distinct polls in timestamp order.")
        self._last_poll = now
        current = {row.at: row for row in rows if self.next_at <= row.at and row.at + MINUTE + self.delay <= now}
        accepted = []
        while self.next_at in current and current[self.next_at] == self._prior.get(self.next_at):
            accepted.append(current.pop(self.next_at))
            self.next_at += MINUTE
        self._prior = current
        return tuple(accepted)


def compare_trial_candles(capture, reference, begin, end, *, lot_size, price_tick):
    local = tuple(row for row in capture if begin <= row.bar.at < end)
    broker = tuple(replace(bar, volume=bar.volume * lot_size) for bar in reference if begin <= bar.at < end)
    lookup = {bar.at: bar for bar in broker}
    matched_volume = sum(row.bar.at in lookup and row.bar.volume == lookup[row.bar.at].volume for row in local)
    gaps = [stamp.isoformat() for index in range((end - begin) // MINUTE)
            if (stamp := begin + index * MINUTE) not in lookup]
    mismatches = compare_candles(local, broker, price_tick)
    return {
        "expected_minutes": (end - begin) // MINUTE, "captured_minutes": len(local), "broker_minutes": len(broker),
        "reference_minutes_missing": gaps, "price_tolerance_inr": str(price_tick),
        "broker_volume_lot_multiplier": lot_size, "matched_volume_minutes": matched_volume,
        "mismatches": mismatches, "passed": bool(broker) and not gaps and not mismatches,
    }


def _option_window(config, master, sessions, day, price):
    expiry = resolve_weekly_expiry(master, day, sessions, config.options)
    if expiry.expiry is None:
        raise ValueError(f"The trial could not verify a listed weekly expiry ({expiry.reason}).")
    interval = config.options.strike_interval
    center = (price / interval).to_integral_value(rounding=ROUND_HALF_UP) * interval
    radius = interval * config.paper_trial.quote_window_wings
    contracts = {item.token: item for item in master.options if item.expiry == expiry.expiry and
                 abs(item.strike - center) <= radius}
    if not contracts:
        raise ValueError("The listed weekly has no contracts in the trial quote window.")
    return expiry.expiry, contracts


def _warmup(config, fetch, session, sessions):
    previous_day = sessions[sessions.index(session.day) - 1]
    previous_session = TradingSession.from_config(previous_day, config.market)
    previous = fetch(previous_session.opens_at, previous_session.closes_at)
    end = max(session.opens_at, minute_floor(now_ist() - timedelta(seconds=config.paper_trial.finalize_delay_seconds)) - MINUTE)
    current = fetch(session.opens_at, end) if end > session.opens_at else ()
    if not previous or previous[-1].at + MINUTE != previous_session.closes_at:
        raise HistoryError("The previous broker session close could not be verified.")
    missing = (end - session.opens_at) // MINUTE - len(current)
    if missing:
        raise HistoryError(f"Current-session candle seed has {missing} missing minutes.")
    return previous, current, end


def _observe(config, session, future, spot_contract, options, headers, fetch, root, executor, strategies,
             indicators, previous, warm_end, report):
    cfg = config.paper_trial
    confirmations = BrokerCandleConfirmations(warm_end, cfg.finalize_delay_seconds)
    subscriptions = {NSE_CASH: [spot_contract.token], NSE_DERIVATIVES: [future.token, *options]}
    pivots = floor_pivots(max(bar.high for bar in previous), min(bar.low for bar in previous), previous[-1].close)
    quotes = {}
    counts, two_sided = Counter(), Counter()
    routes = Counter()
    spot_tick = opening_spot = connection = observe_from = observe_until = pipeline = pending = None
    decisions, accepted, shadow = [], [], []
    next_poll = session.opens_at
    status = "connecting"
    notices, failures = [], []
    report.update(feed_notices=notices, history_errors=failures, decisions=decisions)
    wait_for_final = timedelta(seconds=cfg.broker_candle_max_age_seconds)

    with TickArchive(root / "ticks", session.day, config.data.archive_batch_rows, config.data.archive_flush_seconds) as archive:
        with ThreadPoolExecutor(max_workers=1) as worker, closing(
            MarketFeed(config.data).stream(headers, subscriptions, (NSE_DERIVATIVES, future.token), session)
        ) as events:
            for event in events:
                if isinstance(event, FeedNotice):
                    notices.append({"state": event.kind, "at_ist": event.at.isoformat()})
                    status = "connected" if event.kind == "recovered" else event.kind
                    if event.kind == "unavailable":
                        break
                    if event.kind in {"stale", "reconnecting", "connected"}:
                        quotes.clear()
                        spot_tick = None
                    if event.kind == "connected":
                        if connection is None:
                            connection = event.at
                            observe_from = minute_floor(connection) + MINUTE
                            last_confirmable = minute_floor(session.closes_at - wait_for_final)
                            observe_until = min(observe_from + cfg.duration_minutes * MINUTE, last_confirmable)
                            if observe_until <= observe_from:
                                raise FeedError("The session is too close to ending for a confirmed candle trial.")
                            pipeline = CandlePipeline(session, future.token, config.data, started_at=event.at)
                            report.update(window_start_ist=observe_from.isoformat(), window_end_ist=observe_until.isoformat())
                            print(f"Trial window {observe_from:%H:%M} to {observe_until:%H:%M} IST", flush=True)
                            event = ArchiveRecord(event.at, None)
                        else:
                            event = ArchiveRecord(event.at, None, discontinuity=True)
                    else:
                        continue
                archive.append(event)
                shadow.extend(pipeline.step(event))
                if event.raw is not None:
                    tick = decode_snap(event.raw)
                    counts[tick.token] += 1
                    if tick.exchange_type == NSE_CASH and tick.token == spot_contract.token:
                        spot_tick = tick
                        if session.opens_at <= tick.exchange_at < session.opens_at + MINUTE and opening_spot is None:
                            opening_spot = tick.price
                    elif tick.exchange_type == NSE_DERIVATIVES and tick.token in options:
                        quote = OptionQuote.from_tick(tick, event.received_at)
                        quotes[tick.token] = quote
                        if quote.bids and quote.asks and quote.bids[0].price < quote.asks[0].price:
                            two_sided[tick.token] += 1

                now = now_ist()
                if pending is not None and pending[1].done():
                    requested_at, request = pending
                    pending = None
                    try:
                        rows = request.result()
                    except (RequestError, HistoryError) as exc:
                        failures.append(str(exc))
                        rows = ()
                    for candle in confirmations.accept(rows, requested_at):
                        if candle.at < observe_from:
                            strategies.history.append(AnalyzedBar(candle, indicators.update(candle), "history"))
                            continue
                        accepted.append(candle)
                        row = AnalyzedBar(candle, indicators.update(candle), "broker")
                        live_spot = spot_tick.price if status == "connected" and spot_tick is not None and (
                            0 <= (now - spot_tick.exchange_at).total_seconds() < config.options.quote_max_age_seconds
                        ) else None
                        book = quotes if status == "connected" else {}
                        fresh_books = sum(executor.venue.fresh_book(quote, token, options[token].price_tick, now)
                                          for token, quote in book.items())
                        decision, result = strategies.on_closed_candle(
                            row, at=now, token=future.token, spot=live_spot, quotes=book,
                            previous_close=previous[-1].close, opening_spot=opening_spot, pivots=pivots,
                        )
                        routes[result.reason] += 1
                        decisions.append(decision.view() | {"execution_reason": result.reason, "fresh_option_books": fresh_books})
                        print(f"{candle.at:%H:%M} IST broker candle · {result.reason} · {fresh_books} fresh option books", flush=True)

                if confirmations.next_at >= observe_until or now >= observe_until + wait_for_final:
                    break
                cutoff = min(observe_until, minute_floor(now - confirmations.delay))
                if pending is None and now >= next_poll and confirmations.next_at < cutoff:
                    pending = (now, worker.submit(fetch, confirmations.next_at, cutoff))
                    next_poll = now + timedelta(seconds=cfg.history_poll_seconds)

    report.update(tick_counts={spot_contract.symbol: counts[spot_contract.token], future.symbol: counts[future.token],
                               **{item.symbol: counts[item.token] for item in options.values()}},
                  option_two_sided_ticks={options[token].symbol: count for token, count in two_sided.items()},
                  outcome_counts=dict(routes), finalized_minutes=len(accepted),
                  quote_discontinuities=pipeline.discontinuities if pipeline is not None else None,
                  late_ticks=pipeline.late_dropped if pipeline is not None else None)
    if observe_from is None or observe_from >= observe_until:
        raise FeedError("No complete market minute was available in the trial window.")
    available_until = min(observe_until, minute_floor(now_ist() - confirmations.delay))
    if available_until <= observe_from:
        raise FeedError("The completed sample contains no broker comparison window.")
    reference = fetch(observe_from, available_until)
    report["comparison"] = compare_trial_candles(shadow, reference, observe_from, available_until,
                                                 lot_size=future.lot_size, price_tick=future.price_tick)
    accepted_by_at = {row.at: row for row in accepted}
    report["broker_revisions_after_signal"] = [row.at.isoformat() for row in reference
                                               if row.at in accepted_by_at and row != accepted_by_at[row.at]]
    report["unconfirmed_broker_minutes"] = [stamp.isoformat() for index in range((observe_until - observe_from) // MINUTE)
                                            if (stamp := observe_from + index * MINUTE) not in accepted_by_at]


def run_real_paper_trial(config: Config, directory: Path, calendar: Path) -> dict:
    if config.runtime.mode != "paper" or config.runtime.live_enabled or (
        config.risk.capital_inr is not None or config.risk.risk_per_trade_pct is not None
    ):
        raise ValueError("The real-data trial requires paper mode and both risk inputs unset.")
    try:
        document = json.loads(calendar.read_text(encoding="utf-8-sig"))
        sessions = tuple(date.fromisoformat(day) for day in document["trading_sessions"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("The trial calendar needs a trading_sessions array of ISO dates.") from None
    now = now_ist()
    if sessions != tuple(sorted(set(sessions))) or now.date() not in sessions or sessions.index(now.date()) == 0:
        raise ValueError("The trial needs an ordered exchange calendar including today and the prior session.")
    session = TradingSession.from_config(now.date(), config.market)
    if not session.opens_at <= now < session.closes_at:
        raise ValueError("The trial needs an open market session.")
    root = directory / config.paper_trial.output_directory / f"{now:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    root.mkdir(parents=True)
    path = root / "report.json"
    report = {"mode": "PAPER", "risk_configured": False, "strategy": config.runtime.strategy,
              "started_at_ist": now.isoformat(), "calendar_source": document.get("source"), "report_path": str(path),
              "signal_source": "two matching post-close SmartAPI historical reads", "fill_quotes": "SmartAPI WebSocket"}
    journal = Journal(root / "journal.sqlite3", config.costs)
    executor = PaperExecutor(journal, PaperVenue(root / "venue.sqlite3", config), config)
    try:
        master = load_master(config.data, config.market.underlying, session.day, directory)
        future = master.front_future(session.day)
        report["future"] = future.symbol
        client = AngelClient(config.broker)
        credentials = read_credentials(config.broker, env_file=directory / ".env")
        auth = client.login(credentials)
        headers = {"Authorization": "Bearer " + auth.jwt_token, "x-api-key": credentials.api_key,
                   "x-client-code": credentials.client_code, "x-feed-token": auth.feed_token}
        request = partial(client.historical_minutes, credentials, auth, future.exchange, future.token)

        def fetch(begin, end):
            return parse_minutes(request(begin, end), begin, end)

        previous, current, warm_end = _warmup(config, fetch, session, sessions)
        expiry, options = _option_window(config, master, sessions, session.day, (current or previous)[-1].close)
        report.update(weekly_expiry=expiry.isoformat(), subscribed_options=len(options), broker_seed_minutes=len(current))
        indicators = IndicatorEngine(config.data.rsi_period, config.data.atr_period)
        for row in previous:
            indicators.update(row)
        strategies = SessionStrategies(executor, StrategyA(config, master, sessions, IVStore(journal.path)),
                                        StrategyB(config, master, sessions))
        strategies.history.extend(AnalyzedBar(row, indicators.update(row), "history") for row in current)
        _observe(config, session, future, master.spot, options, headers, fetch, root, executor, strategies,
                 indicators, previous, warm_end, report)
    except (LoginError, RequestError, FeedError, ValueError, OSError, DatabaseError) as exc:
        report["error"] = str(exc)
    finally:
        ledger = journal.snapshot()
        report.update(finished_at_ist=now_ist().isoformat(), paper_orders=len(ledger.orders),
                      open_positions=len(ledger.positions), daily_report=build_report(journal.path, session.day))
        with atomic_path(path) as temporary:
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
