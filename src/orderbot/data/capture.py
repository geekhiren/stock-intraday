from __future__ import annotations

import json
import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import chain
from pathlib import Path
from sqlite3 import Error as DatabaseError
from time import sleep

from orderbot.broker.angel_client import AngelClient, RateLimitError, read_credentials
from orderbot.broker.history import HistoryCache, HistoryError, encode_minutes, parse_minutes
from orderbot.broker.instruments import NSE_CASH, NSE_DERIVATIVES, Instrument, load_master
from orderbot.broker.ws_client import FeedError, FeedNotice, MarketFeed
from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import Config
from orderbot.core.feed_status import record_feed_status
from orderbot.core.ist_time import now_ist

from .candle_builder import Candle, TradingSession, minute_floor
from .replay import CandlePipeline, ReplayReport, compare_candles, replay_archive
from .tick_archive import ArchiveRecord, TickArchive, read_archive


@dataclass(frozen=True, slots=True)
class SessionSeed:
    previous: tuple[Candle, ...] = ()
    backfill: tuple[Candle, ...] = ()


def _seed_path(directory: Path, session: TradingSession) -> Path:
    return directory / session.day.isoformat() / "analysis-seed.json"


def _load_seed(directory: Path, session: TradingSession, signal_token: str, lookback: int) -> SessionSeed:
    path = _seed_path(directory, session)
    if not path.exists():
        return SessionSeed()
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata["signal_token"] != signal_token:
            raise ValueError("Seed token disagrees with the instrument master.")
        first_prior = session.opens_at - timedelta(days=lookback)
        return SessionSeed(
            parse_minutes(metadata["previous"], first_prior, session.opens_at),
            parse_minutes(metadata["backfill"], session.opens_at, session.closes_at),
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError("Unreadable or mismatched indicator seed in the tick archive.") from None


def _save_seed(directory: Path, session: TradingSession, token: str, seed: SessionSeed) -> None:
    with atomic_path(_seed_path(directory, session)) as target:
        target.write_text(json.dumps({
            "signal_token": token,
            "previous": encode_minutes(seed.previous),
            "backfill": encode_minutes(seed.backfill),
        }, separators=(",", ":")), encoding="utf-8")


def load_replay(config: Config, directory: Path, day: date) -> ReplayReport:
    session = TradingSession.from_config(day, config.market)
    master = load_master(config.data, config.market.underlying, day, directory, download=False)
    return _replay_session(config, directory, session, master.front_future(day))


def _replay_session(
    config: Config, directory: Path, session: TradingSession, future: Instrument,
) -> ReplayReport:
    tick_dir = directory / config.data.tick_archive_dir
    seed = _load_seed(tick_dir, session, future.token, config.data.previous_session_lookback_days)
    return replay_archive(
        read_archive(tick_dir, session.day), session, future.token, config.data,
        previous=seed.previous, backfill=seed.backfill,
    )


def compare_with_broker(config: Config, directory: Path, day: date) -> tuple[dict, bool]:
    session = TradingSession.from_config(day, config.market)
    if now_ist() < session.closes_at + timedelta(seconds=config.data.close_grace_seconds):
        raise ValueError("Compare against SmartAPI after the requested session closes.")
    future = load_master(config.data, config.market.underlying, day, directory, download=False).front_future(day)
    replay = _replay_session(config, directory, session, future)
    cache = HistoryCache(config.data, directory)
    client = AngelClient.for_workspace(config, directory)
    credentials = auth = None

    def request_reference():
        nonlocal credentials, auth
        if auth is None:
            credentials = read_credentials(config.broker, env_file=directory / ".env")
            auth = client.login(credentials)
        return client.historical_minutes(credentials, auth, future.exchange, future.token,
                                         session.opens_at, session.closes_at)

    expected = cache.get(future.token, session.opens_at, session.closes_at, request_reference)
    if not expected:
        raise HistoryError("SmartAPI returned no reference candles for the requested session.")
    mismatches = compare_candles(replay.rows, expected, future.price_tick)
    passed = replay.complete_capture and not mismatches
    return {
        "session": day.isoformat(),
        "window_open_ist": session.opens_at.isoformat(),
        "window_close_ist": session.closes_at.isoformat(),
        "passed": passed,
        "complete_capture": replay.complete_capture,
        "reference_minutes": len(expected),
        "captured_minutes": sum(row.source == "feed" for row in replay.rows),
        "price_tolerance_inr": str(future.price_tick),
        "mismatches": mismatches,
    }, passed


def capture_session(
    config: Config, directory: Path, logger: logging.Logger, correlation_id: str, option_tokens: list[str],
) -> int:
    now = now_ist()
    session = TradingSession.from_config(now.date(), config.market)
    earliest = session.opens_at - timedelta(minutes=config.data.capture_lead_minutes)
    if not earliest <= now < session.closes_at:
        raise ValueError(f"Start paper capture between {earliest:%H:%M} and {session.closes_at:%H:%M} IST.")
    credentials = read_credentials(config.broker, env_file=directory / ".env")
    master = load_master(config.data, config.market.underlying, session.day, directory)
    future = master.front_future(session.day)
    options = {option.token for option in master.options if option.expiry >= session.day}
    if set(option_tokens) - options:
        raise ValueError("Capture option tokens must be unexpired NIFTY options from the dated master.")

    client = AngelClient.for_workspace(config, directory)
    auth = client.login(credentials)
    cache = HistoryCache(config.data, directory)
    tick_dir = directory / config.data.tick_archive_dir
    headers = {
        "Authorization": "Bearer " + auth.jwt_token,
        "x-api-key": credentials.api_key,
        "x-client-code": credentials.client_code,
        "x-feed-token": auth.feed_token,
    }
    latest_previous = TradingSession.from_config(session.day - timedelta(days=1), config.market)
    lookback = config.data.previous_session_lookback_days
    oldest_previous = TradingSession.from_config(
        session.day - timedelta(days=lookback), config.market,
    )
    context = {"correlation_id": correlation_id}
    db = directory / config.storage.sqlite_path

    def publish_feed(status, at):
        try:
            record_feed_status(db, status, at)
        except (DatabaseError, OSError):
            logger.warning("dashboard_feed_status_unavailable", extra=context)

    def fetch(start, end):
        try:
            return cache.get(future.token, start, end, lambda: client.historical_minutes(
                credentials, auth, future.exchange, future.token, start, end,
            ))
        except (HistoryError, RateLimitError) as exc:
            logger.warning("history_unavailable", extra={**context, "fields": {"reason": str(exc)}})
            return ()

    with TickArchive(tick_dir, session.day, config.data.archive_batch_rows,
                     config.data.archive_flush_seconds) as archive:
        if archive.has_parts:
            seed = _load_seed(tick_dir, session, future.token, lookback)
            existing = read_archive(tick_dir, session.day)
            first = next(existing)
            pipeline = CandlePipeline(session, future.token, config.data, started_at=first.received_at,
                                      previous=seed.previous, backfill=seed.backfill)
            for entry in chain((first,), existing):
                pipeline.step(entry)
        else:
            prior = fetch(oldest_previous.opens_at, latest_previous.closes_at)
            previous = tuple(bar for bar in prior if bar.at.date() == prior[-1].at.date()) if prior else ()
            end = minute_floor(now_ist())
            backfill = fetch(session.opens_at, min(end, session.closes_at)) if end > session.opens_at else ()
            seed = SessionSeed(previous, backfill)
            pipeline = None

        if now_ist() >= session.closes_at:
            raise ValueError("The session closed before capture could start.")
        count = 0
        feed = MarketFeed(config.data)
        events = feed.stream(
            headers, {NSE_CASH: [master.spot.token], NSE_DERIVATIVES: [future.token, *option_tokens]},
            (NSE_DERIVATIVES, future.token), session,
        )
        heartbeat = now_ist()
        publish_feed("connecting", heartbeat)
        try:
            with closing(events):
                for event in events:
                    if isinstance(event, FeedNotice):
                        logger.info("feed_" + event.kind, extra=context)
                        if event.kind == "unavailable":
                            raise FeedError("Required signal feed did not recover within the reconnect window.")
                        status = "connected" if event.kind == "recovered" else event.kind
                        heartbeat = event.at
                        publish_feed(status, heartbeat)
                        if event.kind != "connected":
                            continue
                        if pipeline is None:
                            _save_seed(tick_dir, session, future.token, seed)
                            pipeline = CandlePipeline(
                                session, future.token, config.data, started_at=event.at,
                                previous=seed.previous, backfill=seed.backfill,
                            )
                            event = ArchiveRecord(event.at, None)
                        else:
                            event = ArchiveRecord(event.at, None, discontinuity=True)
                    elif (event.received_at - heartbeat).total_seconds() >= config.reporting.feed_heartbeat_seconds:
                        heartbeat = event.received_at
                        publish_feed("connected", heartbeat)
                    archive.append(event)
                    count += len(pipeline.step(event))
        finally:
            publish_feed("disconnected", max(heartbeat, now_ist()))

        if pipeline is None:
            raise FeedError("The market capture did not connect before the session closed.")
        final_deadline = session.closes_at + timedelta(seconds=config.data.close_grace_seconds)
        remaining = (final_deadline - now_ist()).total_seconds()
        if remaining > 0:
            sleep(remaining)
        terminal = ArchiveRecord(now_ist(), None)
        archive.append(terminal)
        return count + len(pipeline.step(terminal))
