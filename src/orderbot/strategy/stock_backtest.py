"""Backtest the stock paper strategies by replaying historical one-minute candles through the live trader.

Each closed candle becomes the quote the minute scan would have seen, so entries, sizing, exits and charges
are the same code that paper-trades live.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.broker.history import parse_minutes
from orderbot.core.config import Config
from orderbot.core.ist_time import from_storage, parse_timestamp
from orderbot.core.state_store import open_store
from orderbot.data.candle_builder import MINUTE, Candle
from orderbot.data.stock_history import stock_candle_days

from .stock_paper import StockPaperTrader, baseline, daily_stats, strategy_stats, watch_scores

# ponytail: entries fill at a one-tick spread around each candle close with unlimited depth. Stops and
# targets are checked against each later candle's high and low and fill one tick worse; when a candle touches
# both, the stop wins. Replay recorded ticks (data/tick_archive) for fill-accurate results.
TICK = Decimal("0.05")
DEPTH = 10**9


def _exclude_unverified(db, positions, at, reason, results):
    """Remove unresolved trades from the temporary replay journal before calculating P&L."""
    if not positions:
        return
    with open_store(db) as connection:
        for position in positions:
            trade_id = position["trade_id"]
            opened = connection.execute("SELECT entry_at_utc, strategy FROM stock_trades WHERE trade_id=?", (trade_id,)).fetchone()
            results.append({"symbol": position["symbol"], "strategy": opened["strategy"],
                            "entry_at": from_storage(opened["entry_at_utc"]).isoformat(),
                            "unverified_at": at.isoformat(), "reason": reason})
            connection.execute("DELETE FROM stock_exit_fills WHERE trade_id=?", (trade_id,))
            connection.execute("DELETE FROM stock_trades WHERE trade_id=?", (trade_id,))


def backtest(config: Config, candles: dict[str, list], first_day: date, *, through: datetime | None = None) -> dict:
    """`candles` maps symbol -> SmartAPI rows [timestamp, open, high, low, close, volume], oldest first.

    Days before `first_day` only supply the previous close and the 14-session baselines.
    """
    if config.stocks.capital_inr is None or config.stocks.risk_per_trade_pct is None:
        raise ValueError("Set stock paper capital and risk per trade before running a backtest.")
    universe = {str(index): symbol for index, symbol in enumerate(sorted(candles))}
    minutes: dict[datetime, list[tuple[str, Candle, Decimal]]] = {}
    gaps: dict[datetime, set[str]] = {}
    history, skipped_windows = {}, []
    days = set()
    stocks = config.stocks
    for token, symbol in universe.items():
        raw = candles[symbol]
        bars = parse_minutes(raw, parse_timestamp(raw[0][0]), parse_timestamp(raw[-1][0]) + MINUTE) if raw else ()
        blocks = stock_candle_days(bars, config.market)
        history[symbol] = daily_stats(blocks, stocks.sip_range_minutes)
        previous_close = None
        for block in blocks:
            session = block.session
            prior = previous_close
            previous_close = block.close
            end = min(through, session.closes_at) if through is not None else session.closes_at
            if session.day < first_day or end <= session.opens_at:
                continue
            days.add(session.day)
            missing = block.missing_minutes(end)
            stop = missing[0] if missing else end
            if missing:
                gaps.setdefault(stop, set()).add(token)
                skipped_windows.append({"symbol": symbol, "start": stop.isoformat(), "end_exclusive": end.isoformat(),
                                        "missing_minutes": [at.isoformat() for at in missing]})
            if prior is not None:
                for bar in block.candles:
                    if bar.at >= stop:
                        break  # cumulative volume and VWAP cannot be recovered after a missing bar
                    minutes.setdefault(bar.at, []).append((token, bar, prior))

    state: dict[str, dict] = {}
    planned_days = set()
    blocked_days = set()
    unverified = []
    timeline = sorted(minutes.keys() | gaps.keys())
    with TemporaryDirectory(prefix="orderbot-backtest-") as temporary:
        db = Path(temporary) / "backtest.sqlite3"
        trader = StockPaperTrader(config, db)
        for at in timeline:
            if at in gaps:
                affected = [position for position in trader.held() if position["token"] in gaps[at]]
                if affected:
                    _exclude_unverified(db, affected, at, "missing_candle", unverified)
                    # A stop or target in the gap could have changed the shared risk budget.
                    blocked_days.add(at.date())
            quotes, bars = [], {}
            for token, bar, prior in minutes.get(at, ()):
                opened, high, low, close, volume = bar.open, bar.high, bar.low, bar.close, bar.volume
                bars[token] = (opened, high, low)
                item = state.setdefault(token, {"day": None})
                if item["day"] != at.date():
                    item.update(day=at.date(), previous=prior, open=opened, high=high, low=low, volume=0, value=Decimal(0))
                item["high"], item["low"] = max(item["high"], high), min(item["low"], low)
                item["volume"] += volume
                item["value"] += (high + low + close) / 3 * volume
                if not item["volume"]:
                    continue
                quotes.append({
                    "symbolToken": token, "exchFeedTime": (at + timedelta(minutes=1)).isoformat(),
                    "ltp": close, "open": item["open"], "high": item["high"], "low": item["low"],
                    "close": item["previous"], "avgPrice": item["value"] / item["volume"], "tradeVolume": item["volume"],
                    "depth": {"buy": [{"price": close - TICK, "quantity": DEPTH}], "sell": [{"price": close, "quantity": DEPTH}]},
                })
            if quotes:
                if at.date() not in planned_days:
                    found = {symbol: baseline(stats, at.date().isoformat(), stocks.sip_baseline_days) for symbol, stats in history.items()}
                    trader.set_baselines(at.date(), {symbol: item for symbol, item in found.items() if item}, watch_scores(
                        history, at.date().isoformat(), stocks.sip_baseline_days, stocks.watchlist_size))
                    planned_days.add(at.date())
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
                        "symbolToken": trade["token"], "exchFeedTime": (at + timedelta(seconds=30)).isoformat(),
                        "ltp": price, "open": item["open"], "high": item["high"], "low": item["low"],
                        "close": item["previous"], "avgPrice": item["value"] / item["volume"], "tradeVolume": item["volume"],
                        "depth": {"buy": [{"price": bid, "quantity": DEPTH}], "sell": [{"price": ask, "quantity": DEPTH}]},
                    }], universe, at + timedelta(seconds=30))
                trader.on_scan(quotes, universe, at + MINUTE, allow_entries=at.date() not in blocked_days)
        if timeline:
            _exclude_unverified(db, trader.held(), through or timeline[-1] + MINUTE, "end_of_history", unverified)
        with open_store(db) as connection:
            rows = connection.execute("SELECT * FROM stock_trades WHERE exit_at_utc IS NOT NULL ORDER BY trade_id").fetchall()
            stats = strategy_stats(connection)
    return {
        "symbols": sorted(candles), "sessions": len(days),
        "from": min(days).isoformat() if days else None, "to": max(days).isoformat() if days else None,
        "stats": stats,
        "skipped_windows": sorted(skipped_windows, key=lambda item: (item["start"], item["symbol"])),
        "unverified_trades": unverified,
        "trades": [{
            "symbol": row["symbol"], "strategy": row["strategy"], "side": row["side"], "quantity": row["quantity"],
            "entry_at": from_storage(row["entry_at_utc"]).isoformat(), "entry": row["entry_price"],
            "stop": row["stop"], "exit_at": from_storage(row["exit_at_utc"]).isoformat(), "exit": row["exit_price"],
            "exit_reason": row["exit_reason"], "net_paise": row["net_paise"],
        } for row in rows],
    }
