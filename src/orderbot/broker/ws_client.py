from __future__ import annotations

import json
import logging
import struct
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic, sleep
from typing import Literal
from uuid import uuid4

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from orderbot.core.config import DataConfig
from orderbot.core.ist_time import IST, as_ist, now_ist
from orderbot.data.candle_builder import TradingSession
from orderbot.data.tick_archive import ArchiveRecord

from .instruments import NSE_CASH, NSE_DERIVATIVES

SNAP_QUOTE = 3
SNAP_BYTES = 379
BOOK_START = 147
LEVEL = struct.Struct("<HqqH")
LOGGER = logging.Logger("orderbot.smartapi.websocket")
LOGGER.disabled = True


class FeedError(ValueError):
    """SmartAPI market feed or packet failed validation."""


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    quantity: int
    orders: int


@dataclass(frozen=True, slots=True)
class MarketTick:
    token: str
    exchange_type: int
    exchange_at: datetime
    trade_at: datetime | None
    sequence: int
    price: Decimal
    volume: int
    open_interest: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True, slots=True)
class FeedNotice:
    kind: Literal["connected", "stale", "reconnecting", "recovered", "unavailable"]
    at: datetime


def _epoch_ist(milliseconds: int) -> datetime:
    seconds, fraction = divmod(milliseconds, 1000)
    return datetime.fromtimestamp(seconds, tz=IST) + timedelta(milliseconds=fraction)


def decode_snap(raw: bytes) -> MarketTick:
    if len(raw) != SNAP_BYTES or raw[0] != SNAP_QUOTE:
        raise FeedError("Unexpected SmartAPI Snap Quote format.")
    _, exchange, token_bytes, sequence, exchange_ms, price = struct.unpack_from("<BB25sqqq", raw)
    volume = struct.unpack_from("<q", raw, 67)[0]
    trade_ms, open_interest = struct.unpack_from("<qq", raw, 123)
    token = token_bytes.split(b"\0", 1)[0]
    if not token.isdigit() or exchange not in {NSE_CASH, NSE_DERIVATIVES}:
        raise FeedError("Unexpected Snap Quote token or exchange.")
    if min(price, volume, open_interest, trade_ms) < 0 or exchange_ms <= 0 or trade_ms > exchange_ms:
        raise FeedError("Snap Quote has invalid price, volume or timestamps.")
    sides: dict[int, list[Level]] = {1: [], 0: []}
    for level_number in range(10):
        flag, quantity, price_paise, orders = LEVEL.unpack_from(raw, BOOK_START + level_number * LEVEL.size)
        if price_paise == quantity == 0:
            continue
        if flag not in sides or price_paise <= 0 or quantity <= 0 or len(sides[flag]) >= 5:
            raise FeedError("Unexpected Snap Quote depth layout.")
        sides[flag].append(Level(Decimal(price_paise) / 100, quantity, orders))
    try:
        return MarketTick(
            token.decode("ascii"), exchange, _epoch_ist(exchange_ms), _epoch_ist(trade_ms) if trade_ms else None,
            sequence, Decimal(price) / 100, volume, open_interest,
            tuple(sorted(sides[1], key=lambda level: -level.price)),
            tuple(sorted(sides[0], key=lambda level: level.price)),
        )
    except (OverflowError, OSError, ValueError):
        raise FeedError("Snap Quote timestamp is outside the supported range.") from None


class MarketFeed:
    def __init__(
        self, config: DataConfig, *, connect=connect, clock=now_ist, monotonic=monotonic, sleep=sleep,
    ):
        self.config = config
        self.connect = connect
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep

    def stream(
        self, headers: Mapping[str, str], subscriptions: Mapping[int, Sequence[str]],
        required: tuple[int, str], session: TradingSession,
    ) -> Generator[ArchiveRecord | FeedNotice, None, None]:
        config = self.config
        token_groups = [
            {"exchangeType": exchange, "tokens": sorted(set(tokens))}
            for exchange, tokens in sorted(subscriptions.items()) if tokens
        ]
        if sum(len(group["tokens"]) for group in token_groups) > config.websocket_subscription_limit:
            raise FeedError("Snap Quote subscription limit exceeded.")
        if required[1] not in subscriptions.get(required[0], ()):
            raise FeedError("The required signal token must be subscribed.")
        subscription = {
            "correlationID": uuid4().hex[:10],
            "action": 1,
            "params": {"mode": SNAP_QUOTE, "tokenList": token_groups},
        }
        outage_since = None
        backoff = config.websocket_backoff_seconds
        while (now := as_ist(self.clock())) < session.closes_at:
            mono = self.monotonic()
            remaining = (
                config.websocket_reconnect_seconds - (mono - outage_since)
                if outage_since is not None else config.websocket_reconnect_seconds
            )
            if remaining <= 0:
                yield FeedNotice("unavailable", now)
                return
            try:
                with self.connect(
                    config.websocket_url, additional_headers=headers, logger=LOGGER, ping_interval=None,
                    open_timeout=min(config.websocket_connect_timeout_seconds, remaining),
                    close_timeout=config.websocket_poll_seconds,
                ) as ws:
                    ws.send(json.dumps(subscription))
                    yield FeedNotice("connected", as_ist(self.clock()))
                    heartbeat_due = self.monotonic() + config.websocket_heartbeat_seconds
                    watch_since = None
                    while (now := as_ist(self.clock())) < session.closes_at:
                        mono = self.monotonic()
                        if now >= session.opens_at and watch_since is None:
                            watch_since = mono
                        if outage_since is not None and mono - outage_since >= config.websocket_reconnect_seconds:
                            yield FeedNotice("unavailable", now)
                            return
                        if watch_since is not None and mono - watch_since >= config.websocket_stale_seconds:
                            if outage_since is None:
                                outage_since = watch_since
                            yield FeedNotice("stale", now)
                            break
                        if mono >= heartbeat_due:
                            ws.send("ping")
                            heartbeat_due = mono + config.websocket_heartbeat_seconds
                        try:
                            raw = ws.recv(timeout=config.websocket_poll_seconds)
                        except TimeoutError:
                            yield ArchiveRecord(as_ist(self.clock()), None)
                            continue
                        stamp = as_ist(self.clock())
                        if isinstance(raw, bytes):
                            yield ArchiveRecord(stamp, raw)
                            tick = decode_snap(raw)
                            if (tick.exchange_type, tick.token) == required and abs(
                                (stamp - tick.exchange_at).total_seconds()
                            ) <= config.websocket_stale_seconds:
                                watch_since = self.monotonic() if stamp >= session.opens_at else None
                                if outage_since is not None:
                                    yield FeedNotice("recovered", stamp)
                                outage_since = None
                                backoff = config.websocket_backoff_seconds
                        elif raw != "pong":
                            raise FeedError("SmartAPI returned an unexpected subscription response.")
            except InvalidStatus as exc:
                if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise FeedError(f"SmartAPI WebSocket rejected the connection (HTTP {exc.response.status_code}).") from None
            except (OSError, WebSocketException):
                pass
            if as_ist(self.clock()) >= session.closes_at:
                break
            mono = self.monotonic()
            if outage_since is None:
                outage_since = mono
            yield FeedNotice("reconnecting", as_ist(self.clock()))
            remaining = config.websocket_reconnect_seconds - (mono - outage_since)
            remaining_session = (session.closes_at - as_ist(self.clock())).total_seconds()
            if min(remaining, remaining_session) > 0:
                self.sleep(min(backoff, remaining, remaining_session))
            backoff = min(backoff * 2, config.websocket_backoff_max_seconds)
