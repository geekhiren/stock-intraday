"""Backtest the stock paper strategies by replaying historical one-minute candles through the live trader.

Each closed candle becomes the quote the minute scan would have seen, so entries, sizing, exits and charges
are the same code that paper-trades live.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.core.config import Config
from orderbot.core.ist_time import from_storage, parse_timestamp
from orderbot.core.state_store import open_store

from .stock_paper import StockPaperTrader, baseline, daily_stats, strategy_stats, watch_scores

# ponytail: entries fill at a one-tick spread around each candle close with unlimited depth. Stops and
# targets are checked against each later candle's high and low and fill one tick worse; when a candle touches
# both, the stop wins. Replay recorded ticks (data/tick_archive) for fill-accurate results.
TICK = Decimal("0.05")
DEPTH = 10**9


def backtest(config: Config, candles: dict[str, list], first_day: date) -> dict:
    """`candles` maps symbol -> SmartAPI rows [timestamp, open, high, low, close, volume], oldest first.

    Days before `first_day` only supply the previous close and the 14-session baselines.
    """
    universe = {str(index): symbol for index, symbol in enumerate(sorted(candles))}
    minutes: dict = {}
    for token, symbol in universe.items():
        for stamp, opened, high, low, close, volume in candles[symbol]:
            minutes.setdefault(parse_timestamp(stamp), []).append((token, *(Decimal(str(value)) for value in (opened, high, low, close)), int(volume)))

    stocks = config.stocks
    history = {symbol: daily_stats(rows, config.market.session_open, stocks.sip_range_minutes) for symbol, rows in candles.items()}
    state: dict[str, dict] = {}
    days = set()
    with TemporaryDirectory(prefix="orderbot-backtest-") as temporary:
        db = Path(temporary) / "backtest.sqlite3"
        trader = StockPaperTrader(config, db)
        for at in sorted(minutes):
            quotes, bars = [], {}
            for token, opened, high, low, close, volume in minutes[at]:
                bars[token] = (opened, high, low)
                item = state.setdefault(token, {"day": None, "close": None})
                if item["day"] != at.date():
                    item.update(day=at.date(), previous=item["close"], open=opened, high=high, low=low, volume=0, value=Decimal(0))
                item["high"], item["low"], item["close"] = max(item["high"], high), min(item["low"], low), close
                item["volume"] += volume
                item["value"] += (high + low + close) / 3 * volume
                if item["previous"] is None or not item["volume"]:
                    continue
                quotes.append({
                    "symbolToken": token, "ltp": close, "open": item["open"], "high": item["high"], "low": item["low"],
                    "close": item["previous"], "avgPrice": item["value"] / item["volume"], "tradeVolume": item["volume"],
                    "depth": {"buy": [{"price": close - TICK, "quantity": DEPTH}], "sell": [{"price": close, "quantity": DEPTH}]},
                })
            if at.date() >= first_day and quotes:
                if at.date() not in days:
                    found = {symbol: baseline(stats, at.date().isoformat(), stocks.sip_baseline_days) for symbol, stats in history.items()}
                    trader.set_baselines(at.date(), {symbol: item for symbol, item in found.items() if item}, watch_scores(
                        history, at.date().isoformat(), stocks.sip_baseline_days, stocks.watchlist_size))
                days.add(at.date())
                for trade in trader.held():  # what the live 5-second price watch would have caught inside this minute
                    if trade["token"] not in bars:
                        continue
                    opened, high, low = bars[trade["token"]]
                    direction = 1 if trade["side"] == "long" else -1
                    stop, target = Decimal(trade["stop"]), Decimal(trade["target"]) if trade["target"] else None
                    adverse, favourable = (low, high) if direction > 0 else (high, low)
                    if direction * (adverse - stop) <= 0:
                        price, slip = (min(stop, opened) if direction > 0 else max(stop, opened)), TICK
                    elif target is not None and direction * (favourable - target) >= 0:
                        price, slip = (max(target, opened) if direction > 0 else min(target, opened)), Decimal(0)
                    else:
                        continue
                    exit_price = price - direction * slip  # the bid for a long, the ask for a short
                    bid, ask = (exit_price, exit_price + TICK) if direction > 0 else (exit_price - TICK, exit_price)
                    item = state[trade["token"]]
                    trader.manage([{
                        "symbolToken": trade["token"], "ltp": price, "open": item["open"], "high": item["high"], "low": item["low"],
                        "close": item["previous"], "avgPrice": item["value"] / item["volume"], "tradeVolume": item["volume"],
                        "depth": {"buy": [{"price": bid, "quantity": DEPTH}], "sell": [{"price": ask, "quantity": DEPTH}]},
                    }], universe, at + timedelta(seconds=30))
                trader.on_scan(quotes, universe, at + timedelta(minutes=1))
        with open_store(db) as connection:
            rows = connection.execute("SELECT * FROM stock_trades WHERE exit_at_utc IS NOT NULL ORDER BY trade_id").fetchall()
            stats = strategy_stats(connection)
    return {
        "symbols": sorted(candles), "sessions": len(days),
        "from": min(days).isoformat() if days else None, "to": max(days).isoformat() if days else None,
        "stats": stats,
        "trades": [{
            "symbol": row["symbol"], "strategy": row["strategy"], "side": row["side"], "quantity": row["quantity"],
            "entry_at": from_storage(row["entry_at_utc"]).isoformat(), "entry": row["entry_price"],
            "stop": row["stop"], "exit_at": from_storage(row["exit_at_utc"]).isoformat(), "exit": row["exit_price"],
            "exit_reason": row["exit_reason"], "net_paise": row["net_paise"],
        } for row in rows],
    }
