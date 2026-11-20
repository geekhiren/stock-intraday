"""Use imported NSE stock candles first and fetch only uncovered regular-session windows."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from orderbot.broker.history import HistoryCache, HistoryError, parse_minutes
from orderbot.core.config import Config, MarketConfig
from orderbot.core.ist_time import as_ist

from . import candle_store
from .candle_builder import MINUTE, Candle, TradingSession


@dataclass(frozen=True, slots=True)
class StockCandleDay:
    session: TradingSession
    candles: tuple[Candle, ...]

    def missing_minutes(self, through: datetime | None = None) -> tuple[datetime, ...]:
        start, end = self.session.opens_at, self.session.closes_at
        if through is not None:
            end = min(end, as_ist(through))
        seen = {bar.at for bar in self.candles}
        return tuple(at for step in range((end - start) // MINUTE)
                     if (at := start + step * MINUTE) not in seen)

    @property
    def close(self) -> Decimal | None:
        last = self.candles[-1]
        return last.close if last.at + MINUTE == self.session.closes_at else None


def stock_candle_days(candles: Iterable[Candle], market: MarketConfig) -> tuple[StockCandleDay, ...]:
    """Group observed regular-session candles. Holidays and absent rows are left out."""
    groups: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        groups[as_ist(candle.at).date()].append(candle)
    result = []
    for day, observed in sorted(groups.items()):
        if day.weekday() >= 5:
            continue
        session = TradingSession.from_config(day, market, segment="cash")
        bars = tuple(sorted((bar for bar in observed if session.contains(bar.at)), key=lambda bar: bar.at))
        if bars:
            result.append(StockCandleDay(session, bars))
    return tuple(result)


class StockHistory:
    def __init__(self, config: Config, cache: HistoryCache):
        self.market = config.market
        self.span_days = config.data.stock_history_span_days
        self.cache = cache

    def get(self, symbol, token, start, end, fetch=None):
        start, end = as_ist(start), as_ist(end)
        if end <= start:
            return ()
        # The stock universe is the CAS-eligible cash segment. Exclude auction and post-close bars.
        windows = {}
        for offset in range((end.date() - start.date()).days + 1):
            day = start.date() + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            session = TradingSession.from_config(day, self.market, segment="cash")
            since, until = max(start, session.opens_at), min(end, session.closes_at)
            if since < until:
                windows[day] = since, until

        def regular(rows):
            return (bar for bar in rows if (window := windows.get(bar.at.date())) and window[0] <= bar.at < window[1])

        try:
            imported = candle_store.load(symbol, start.date(), end.date())
            local = {bar.at: bar for bar in regular(parse_minutes(imported, start, end))}
        except (OSError, ValueError, TypeError, IndexError):
            raise HistoryError(f"Local candle data is unreadable or malformed for {symbol}.") from None

        # Old exact-range SmartAPI cache entries are still usable alongside the scraped store.
        if token and (end - start).days <= self.span_days and len(windows):
            cached = self.cache.read_cached(token, start, end)
            local.update((bar.at, bar) for bar in regular(cached or ()))
        counts = Counter(at.date() for at in local)
        pending = []
        for day, (since, until) in windows.items():
            if counts[day] == (until - since) // MINUTE:
                continue
            if pending and (since - pending[-1][1]) <= timedelta(days=(3 if day.weekday() == 0 else 1)):
                previous, previous_end = pending[-1]
                # Only group missing adjacent sessions. Keep each REST request within the broker limit.
                covered_between = any(previous_end.date() < other < day for other in windows)
                if not covered_between and until - previous <= timedelta(days=self.span_days):
                    pending[-1] = previous, until
                    continue
            pending.append((since, until))
        if token:
            for since, until in pending:
                if fetch is None:
                    more = self.cache.read_cached(token, since, until) or ()
                else:
                    more = self.cache.get(token, since, until, lambda: fetch(since, until))
                local.update((bar.at, bar) for bar in regular(more))
        return tuple(local[at] for at in sorted(local))
