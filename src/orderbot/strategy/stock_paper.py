"""Paper intraday trading of F&O stocks from the minute quote scan. No broker orders are sent.

Two entries mirror the NIFTY strategies: an opening-range breakout until the Strategy B cutoff, then a
VWAP crossover until the Strategy A cutoff if the breakout did not trade. One position at a time, no leverage.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist, from_storage, to_storage
from orderbot.core.money import PAISA, round_inr
from orderbot.core.state_store import open_store
from orderbot.data.indicators import WilderAverage
from orderbot.reporting.alerts import queue_alert

# ponytail: one-minute polling. Stops and targets are checked once a scan and fill at that scan's bid/ask,
# and the opening range is the day high/low at the first scan after the freeze. Move to the tick feed
# (ws_client) and history candles before trusting results to the rupee.
FREEZE_GRACE = timedelta(minutes=2)
MAX_SAMPLE_GAP = timedelta(seconds=150)


def intraday_charges(buy: Decimal, sell: Decimal, quantity: int, config: Config) -> Decimal:
    """Equity intraday round-trip charges, each line rounded half-up to a paisa."""
    costs, stocks = config.costs, config.stocks
    buy_value, sell_value = buy * quantity, sell * quantity
    turnover = buy_value + sell_value
    brokerage = sum(min(costs.brokerage_per_order_inr, max(stocks.brokerage_min_inr, value * stocks.brokerage_rate))
                    for value in (buy_value, sell_value))
    exchange, sebi = turnover * stocks.exchange_including_ipft_rate, turnover * costs.sebi_rate
    return sum(round_inr(line) for line in (
        brokerage, sell_value * stocks.stt_sell_rate, exchange, sebi, buy_value * costs.stamp_buy_rate,
        (brokerage + exchange + sebi) * costs.gst_rate,
    ))


def daily_stats(rows, session_open: time, range_minutes: int) -> list[tuple[str, float, int]]:
    """Per session in SmartAPI minute rows [stamp, open, high, low, close, volume]:
    (day, true range, opening-range volume, close, volume)."""
    total = session_open.hour * 60 + session_open.minute + range_minutes
    cutoff = f"{total // 60:02d}:{total % 60:02d}"
    sessions, previous_close = [], None
    for stamp, _, high, low, close, volume in rows:
        high, low, close = float(high), float(low), float(close)
        if not sessions or sessions[-1][0] != stamp[:10]:
            if sessions:
                previous_close = sessions[-1][3]
            sessions.append([stamp[:10], high, low, close, 0, previous_close, 0])
        item = sessions[-1]
        item[1], item[2], item[3], item[6] = max(item[1], high), min(item[2], low), close, item[6] + int(volume)
        if stamp[11:16] < cutoff:
            item[4] += int(volume)
    return [(day, high - low if before is None else max(high - low, abs(high - before), abs(low - before)), opening, close, total)
            for day, high, low, close, opening, before, total in sessions]


def watch_scores(stats_by_symbol: dict[str, list], before: str, period: int, size: int) -> list[dict]:
    """Pre-market watchlist: rank stocks on how much they move (ATR as % of price), how busy the last session
    was against its average, and how far they moved over five sessions. The score is the mean percentile, 0-100."""
    rows = []
    for symbol, stats in stats_by_symbol.items():
        past = [item for item in stats if item[0] < before][-period:]
        if len(past) < period or past[-1][3] <= 0 or not sum(item[4] for item in past):
            continue
        close = past[-1][3]
        rows.append({
            "symbol": symbol, "atr_pct": 100 * sum(item[1] for item in past) / period / close,
            "relative_volume": past[-1][4] / (sum(item[4] for item in past) / period),
            "change_5d": 100 * (close / past[-6][3] - 1) if len(past) >= 6 and past[-6][3] > 0 else 0.0,
        })
    for key, measure in (("atr_pct", lambda row: row["atr_pct"]), ("relative_volume", lambda row: row["relative_volume"]),
                         ("change_5d", lambda row: abs(row["change_5d"]))):
        for rank, row in enumerate(sorted(rows, key=measure)):
            row["score"] = row.get("score", 0) + 100 * (rank + 1) / len(rows) / 3
    top = sorted(rows, key=lambda row: row["score"], reverse=True)[:size]
    return [{key: row[key] if key == "symbol" else round(row[key], 2) for key in ("symbol", "score", "atr_pct", "relative_volume", "change_5d")}
            for row in top]


def baseline(stats, before: str, period: int) -> tuple[Decimal, Decimal] | None:
    """(ATR, average opening-range volume) over the `period` sessions before ISO day `before`."""
    past = [item for item in stats if item[0] < before][-period:]
    atr, volume = sum(item[1] for item in past) / period, sum(item[2] for item in past) / period
    if len(past) < period or atr <= 0 or volume <= 0:
        return None
    return Decimal(str(round(atr, 4))), Decimal(str(round(volume, 2)))


def strategy_stats(connection) -> list[dict]:
    """Accuracy of every closed paper trade in the journal, per strategy and overall."""
    groups: dict[str, list[int]] = {}
    for strategy, net in connection.execute("SELECT strategy, net_paise FROM stock_trades WHERE exit_at_utc IS NOT NULL"):
        groups.setdefault(strategy, []).append(net)
        groups.setdefault("all", []).append(net)

    def line(name, nets):
        wins, losses = [net for net in nets if net > 0], [net for net in nets if net <= 0]
        return {
            "strategy": name, "trades": len(nets), "wins": len(wins), "win_rate": round(100 * len(wins) / len(nets), 1),
            "net_paise": sum(nets), "average_win_paise": round(sum(wins) / len(wins)) if wins else 0,
            "average_loss_paise": round(sum(losses) / len(losses)) if losses else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if sum(losses) < 0 else None,
        }

    return [line(name, groups[name]) for name in sorted(groups, key=lambda name: (name == "all", name))]


@dataclass(frozen=True, slots=True)
class Tick:
    token: str
    symbol: str
    ltp: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    previous_close: Decimal
    vwap: Decimal
    volume: int
    bid: Decimal
    ask: Decimal
    bid_quantity: int
    ask_quantity: int

    @classmethod
    def parse(cls, quote: dict, universe: dict[str, str]) -> "Tick":
        number = lambda key: Decimal(str(quote[key]))  # noqa: E731
        bid, ask = quote["depth"]["buy"][0], quote["depth"]["sell"][0]
        tick = cls(quote["symbolToken"], universe[quote["symbolToken"]], number("ltp"), number("open"), number("high"),
                   number("low"), number("close"), number("avgPrice"), int(quote["tradeVolume"]),
                   Decimal(str(bid["price"])), Decimal(str(ask["price"])), int(bid["quantity"]), int(ask["quantity"]))
        prices = (tick.ltp, tick.open, tick.low, tick.previous_close, tick.vwap, tick.bid)
        if not all(price.is_finite() and price > 0 for price in prices) or not tick.high >= tick.low or not tick.ask > tick.bid:
            raise ValueError("Unusable quote.")
        return tick


class _Series:
    """Rolling minute samples of one stock for the VWAP entry."""
    def __init__(self, config: Config):
        self.samples = deque(maxlen=config.strategy_a.volume_lookback + 2)
        self.gain, self.loss = WilderAverage(config.data.rsi_period), WilderAverage(config.data.rsi_period)
        self.atr_average = WilderAverage(config.data.atr_period)
        self.rsi = self.atr = None

    def add(self, tick: Tick) -> None:
        if self.samples:
            change = tick.ltp - self.samples[-1].ltp
            gain, loss = self.gain.update(max(change, Decimal(0))), self.loss.update(max(-change, Decimal(0)))
            if gain is not None and loss is not None:
                self.rsi = 100 * gain / (gain + loss) if gain or loss else Decimal(50)
            self.atr = self.atr_average.update(abs(change))
        self.samples.append(tick)


@dataclass(frozen=True, slots=True)
class _Candidate:
    tick: Tick
    strategy: str
    side: str
    distance: Decimal
    reward: Decimal | None  # target distance as a multiple of the stop distance; None holds to the square-off


class StockPaperTrader:
    def __init__(self, config: Config, db: Path, *, activity=None):
        """`activity(job, message)` receives entries and exits for the dashboard's job log."""
        self.config, self.db, self.activity = config, db, activity
        self.status = "Waiting for the first scan"
        self._series: dict[str, _Series] = {}
        self._marks: dict[str, Decimal] = {}
        self._ticks: dict[str, Tick] = {}
        self._last_at: datetime | None = None
        self._baselines: dict[str, tuple[Decimal, Decimal]] = {}
        self._baseline_day: date | None = None
        self.baseline_status: str | None = None
        self.watchlist: list[dict] = []

    def set_baselines(self, day: date, baselines: dict[str, tuple[Decimal, Decimal]], watchlist=()) -> None:
        """Per symbol (14-day ATR, average opening-range volume) for the stocks-in-play scan of `day`, and that
        day's pre-market watchlist from `watch_scores`."""
        self._baselines, self._baseline_day, self.baseline_status = baselines, day, None
        self.watchlist = list(watchlist)

    # -- per-scan entry point -------------------------------------------------------------------
    def on_scan(self, quotes, universe: dict[str, str], at: datetime) -> None:
        now, ticks = as_ist(at), self._parse(quotes, universe)
        if self._last_at is None or now.date() != self._last_at.date() or now - self._last_at > MAX_SAMPLE_GAP:
            self._series.clear()  # a gap would corrupt minute-based volume, RSI and crossover checks
        self._last_at, self._ticks = now, ticks
        for tick in ticks.values():
            self._series.setdefault(tick.token, _Series(self.config)).add(tick)
            self._marks[tick.token] = tick.ltp
        with open_store(self.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            day, halted, square_off = self._exits(connection, ticks, now, signals=True)
            if self.config.stocks.watchlist_only:  # entries only in today's watchlist; none until it is ready
                names = {row["symbol"] for row in self.watchlist} if self._baseline_day == now.date() else set()
                ticks = {token: tick for token, tick in ticks.items() if tick.symbol in names}
            self.status = self._enter(connection, ticks, now, day, halted, square_off)

    @staticmethod
    def _parse(quotes, universe: dict[str, str]) -> dict[str, Tick]:
        ticks = {}
        for quote in quotes:
            try:
                tick = Tick.parse(quote, universe)
            except (KeyError, IndexError, TypeError, ValueError, InvalidOperation):
                continue
            ticks[tick.token] = tick
        return ticks

    def held(self) -> list[dict]:
        """Open paper positions, for the fast price watch between scans."""
        with open_store(self.db) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT trade_id, token, symbol, side, stop, target FROM stock_trades WHERE exit_at_utc IS NULL")]

    def manage(self, quotes, universe: dict[str, str], at: datetime) -> None:
        """Check open positions against fresh quotes between full scans. It never opens a position."""
        now, ticks = as_ist(at), self._parse(quotes, universe)
        self._ticks = self._ticks | ticks
        for tick in ticks.values():
            self._marks[tick.token] = tick.ltp
        with open_store(self.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._exits(connection, ticks, now)

    def _exits(self, connection, ticks, now: datetime, *, signals: bool = False) -> tuple[str, bool, bool]:
        """`signals` adds the indicator exits, which are only meaningful on the minute scan."""
        day = now.date().isoformat()
        halted = bool(connection.execute("SELECT 1 FROM kill_switch_latches WHERE session_day=?", (day,)).fetchone())
        square_off = now.time() >= self.config.exits.square_off_time
        for trade in connection.execute("SELECT * FROM stock_trades WHERE exit_at_utc IS NULL").fetchall():
            self._manage(connection, trade, ticks.get(trade["token"]), now,
                         "kill_switch" if halted else "square_off" if square_off or trade["session_day"] != day else None,
                         signals)
        return day, halted, square_off

    # -- exits ------------------------------------------------------------------------------------
    def _manage(self, connection, trade, tick: Tick | None, now: datetime, forced: str | None, signals: bool = False) -> None:
        if tick is None:
            return  # no usable two-sided quote this scan; try again on the next one
        long = trade["side"] == "long"
        price = tick.bid if long else tick.ask
        direction = 1 if long else -1
        reason = forced
        if reason is None and direction * (price - Decimal(trade["stop"])) <= 0:
            reason = "stop"
        if reason is None and trade["target"] and direction * (price - Decimal(trade["target"])) >= 0:
            reason = "target"
        if reason is None and signals:
            stocks, limits, series = self.config.stocks, self.config.exits, self._series.get(trade["token"])
            if stocks.exit_on_rsi and series is not None and series.rsi is not None and (
                    series.rsi > limits.long_exit_rsi if long else series.rsi < limits.short_exit_rsi):
                reason = "rsi"  # momentum is stretched in the trade's favour
            elif stocks.exit_on_vwap_cross and direction * (tick.ltp - tick.vwap) < 0:
                reason = "vwap_cross"  # price is back on the wrong side of the day's average
        if reason is None:
            return
        entry, quantity = Decimal(trade["entry_price"]), trade["quantity"]
        buy, sell = (entry, price) if long else (price, entry)
        charges = intraday_charges(buy, sell, quantity, self.config)
        net = round_inr((sell - buy) * quantity - charges)
        connection.execute("""
            UPDATE stock_trades SET exit_at_utc=?, exit_price=?, exit_reason=?, charges_paise=?, net_paise=? WHERE trade_id=?
        """, (to_storage(now), str(round_inr(price)), reason, int(charges / PAISA), int(net / PAISA), trade["trade_id"]))
        queue_alert(connection, f"stock_exit:{trade['trade_id']}", "stock_exit", f"stock-{trade['trade_id']}", now,
                    {"symbol": trade["symbol"], "reason": reason, "net_inr": str(net)})
        if self.activity:
            self.activity("Paper trader", f"Closed {trade['symbol']} at ₹{round_inr(price)} · {reason.replace('_', ' ')} · net ₹{net}")

    # -- entries ----------------------------------------------------------------------------------
    def _enter(self, connection, ticks, now: datetime, day: str, halted: bool, square_off: bool) -> str:
        cfg, risk = self.config, self.config.risk
        stocks, orb, vwap = cfg.stocks, cfg.strategy_b, cfg.strategy_a
        opening = datetime.combine(now.date(), cfg.market.session_open, now.tzinfo)
        freeze_at = opening + timedelta(minutes=orb.range_minutes)
        freeze = freeze_at.time()
        frozen = connection.execute("SELECT 1 FROM stock_ranges WHERE session_day=? LIMIT 1", (day,)).fetchone()
        if not frozen and freeze_at <= now < freeze_at + FREEZE_GRACE:
            connection.executemany("INSERT INTO stock_ranges(session_day, token, open, high, low) VALUES (?, ?, ?, ?, ?)", [
                (day, tick.token, str(tick.open), str(tick.high), str(tick.low)) for tick in ticks.values()])
            frozen = True
        clock = now.time()
        # Both are evaluated on every scan, even while entries are blocked, so each first break is recorded.
        breakouts = self._breakouts(connection, ticks, day) if frozen and freeze <= clock < orb.entry_window_end else []
        plays, play_status = self._plays(connection, ticks, now, day, opening) if stocks.sip_entries else ([], None)

        trades = connection.execute("SELECT * FROM stock_trades WHERE session_day=? ORDER BY trade_id", (day,)).fetchall()
        closed = [trade for trade in trades if trade["exit_at_utc"]]
        held = {trade["token"] for trade in trades if not trade["exit_at_utc"]}
        losses = 0
        for trade in reversed(closed):
            if trade["net_paise"] >= 0:
                break
            losses += 1
        net = sum(trade["net_paise"] for trade in closed) * PAISA

        def managing():
            return "Managing the open position" if len(held) == 1 else f"Managing {len(held)} open positions"

        if halted:
            return "Stopped for today · Stop trading pressed"
        if square_off:
            return "Square-off time passed · No new entries"
        if len(held) >= stocks.max_positions:
            return managing()
        if net <= -stocks.capital_inr * risk.daily_loss_limit_pct / 100:
            return "Stopped for today · Daily loss limit reached"
        if losses >= risk.max_consecutive_losses:
            return f"Stopped for today · {losses} losses in a row"
        if len(trades) >= risk.max_entries_per_day:
            return "Stopped for today · Maximum entries reached"
        if closed and now - from_storage(closed[-1]["exit_at_utc"]) < timedelta(minutes=vwap.cooldown_minutes):
            return managing() if held else "Cooling down after the last exit"

        if clock < freeze:
            candidates, status = [], f"Building the opening range until {freeze:%H:%M}"
        elif clock < orb.entry_window_end:
            candidates, status = (breakouts, "Watching opening-range breakouts") if frozen else (
                [], f"Opening range missed (scanner started after {freeze:%H:%M})")
        elif clock >= vwap.entry_close:
            candidates, status = [], "Entry window closed · No new entries"
        elif any(trade["strategy"] == "orb" for trade in trades):
            candidates, status = [], "Done for today · The opening-range trade used the session"
        elif not stocks.vwap_entries:
            candidates, status = [], "Opening-range window closed · No automatic entries until tomorrow"
        else:
            candidates, status = self._crossovers(ticks), "Watching VWAP crossovers"

        entries = len(trades)
        for item in plays + sorted(candidates, key=lambda item: item.tick.vwap * item.tick.volume, reverse=True):
            if len(held) >= stocks.max_positions or entries >= risk.max_entries_per_day:
                break
            if item.tick.token not in held and self._open(connection, item, now, day) is None:
                held.add(item.tick.token)
                entries += 1
        return managing() if held else play_status or status

    def _plays(self, connection, ticks, now: datetime, day: str, opening: datetime) -> tuple[list[_Candidate], str | None]:
        """Stocks in play: after the opening range, the stocks with the highest relative opening volume are
        traded on a break of that range in the direction of its first move (Zarattini, Barbon and Aziz, 2024)."""
        stocks = self.config.stocks
        freeze_at = opening + timedelta(minutes=stocks.sip_range_minutes)
        query = "SELECT * FROM stock_plays WHERE session_day=? AND token != '' ORDER BY relative_volume DESC"
        if not connection.execute("SELECT 1 FROM stock_plays WHERE session_day=? LIMIT 1", (day,)).fetchone():
            if now < freeze_at:
                return [], self.baseline_status or f"Stocks-in-play scan at {freeze_at:%H:%M}"
            if now >= freeze_at + FREEZE_GRACE or self._baseline_day != now.date():
                return [], None
            ranked = []
            for tick in ticks.values():
                direction = (tick.ltp > tick.open) - (tick.ltp < tick.open)
                if tick.symbol not in self._baselines or not direction:
                    continue
                atr, average = self._baselines[tick.symbol]
                relative = tick.volume / average
                if relative >= stocks.sip_min_relative_volume:
                    ranked.append((day, tick.token, "long" if direction > 0 else "short", str(tick.high if direction > 0 else tick.low),
                                   str(stocks.sip_stop_atr * atr), float(relative), "waiting"))
            ranked.sort(key=lambda row: row[5], reverse=True)
            connection.executemany("INSERT INTO stock_plays VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   [(day, "", "none", "0", "0", 0, "done"), *ranked[:stocks.sip_candidates]])
        found, done, waiting = [], [], 0
        for row in connection.execute(query, (day,)).fetchall():
            tick = ticks.get(row["token"])
            if row["state"] != "waiting":
                continue
            direction = 1 if row["side"] == "long" else -1
            if tick is None or direction * (tick.ltp - Decimal(row["edge"])) <= 0:
                waiting += 1
                continue
            done.append((day, row["token"]))  # only the first break is tradable
            found.append(_Candidate(tick, "sip", row["side"], Decimal(row["stop_distance"]), None))
        connection.executemany("UPDATE stock_plays SET state='done' WHERE session_day=? AND token=?", done)
        if now.time() >= self.config.strategy_a.entry_close:
            return [], None
        return found, f"Watching {waiting} stock{'' if waiting == 1 else 's'} in play" if waiting else None

    def _open(self, connection, item: _Candidate, now: datetime, day: str) -> str | None:
        """Size and book one paper entry. Returns None when booked, otherwise why it was refused."""
        cfg, stocks, tick, long = self.config, self.config.stocks, item.tick, item.side == "long"
        entry, available = (tick.ask, tick.ask_quantity) if long else (tick.bid, tick.bid_quantity)
        if (tick.ask - tick.bid) / tick.ltp > stocks.max_spread_fraction:
            return "The buy/sell spread is too wide right now."
        # The stocks-in-play stop is a fraction of the daily ATR and is used as published; other stops are banded.
        distance = item.distance if item.strategy == "sip" else min(
            max(item.distance, entry * stocks.min_stop_pct / 100), entry * stocks.max_stop_pct / 100)
        quantity = int(min(stocks.capital_inr * stocks.risk_per_trade_pct / 100 / distance,
                           stocks.capital_inr / stocks.max_positions / entry))
        if quantity < 1 or quantity > available:
            return "One share costs more than a position's capital, or too few shares are on offer."
        if intraday_charges(entry, entry, quantity, cfg) > quantity * distance * cfg.risk.max_round_trip_friction_pct / 100:
            return "Charges are too large against the risk of this trade."
        direction = 1 if long else -1
        stop = round_inr(entry - direction * distance)
        target = "" if item.reward is None else round_inr(entry + direction * distance * item.reward)
        trade_id = connection.execute("""
            INSERT INTO stock_trades(session_day, token, symbol, strategy, side, quantity, entry_at_utc, entry_price, stop, target)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (day, tick.token, tick.symbol, item.strategy, item.side, quantity, to_storage(now), str(round_inr(entry)),
              str(stop), str(target))).lastrowid
        queue_alert(connection, f"stock_entry:{trade_id}", "stock_entry", f"stock-{trade_id}", now,
                    {"symbol": tick.symbol, "side": item.side, "strategy": item.strategy, "quantity": quantity})
        if self.activity:
            self.activity("Paper trader", f"{'Bought' if long else 'Sold short'} {quantity} {tick.symbol} at ₹{round_inr(entry)} "
                                          f"· {item.strategy} · stop ₹{stop}")
        return None

    # -- manual paper trades from the dashboard ---------------------------------------------------
    def manual_entry(self, symbol: str, side: str, at: datetime) -> str | None:
        """Paper-enter `symbol` at the last scanned quote. Returns None when booked, otherwise why not."""
        now, cfg = as_ist(at), self.config
        tick = next((tick for tick in self._ticks.values() if tick.symbol == symbol), None)
        if side not in {"long", "short"} or tick is None or self._last_at is None or now - self._last_at > MAX_SAMPLE_GAP:
            return "No fresh quote for that stock. Use an F&O stock symbol such as HDFCBANK during market hours."
        with open_store(self.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            day = now.date().isoformat()
            if connection.execute("SELECT 1 FROM kill_switch_latches WHERE session_day=?", (day,)).fetchone():
                return "Stop trading was pressed today."
            if now.time() >= cfg.exits.square_off_time:
                return "It is past the square-off time."
            held = [row[0] for row in connection.execute("SELECT token FROM stock_trades WHERE exit_at_utc IS NULL")]
            if tick.token in held:
                return f"{symbol} already has an open position."
            if len(held) >= cfg.stocks.max_positions:
                return "All position slots are in use. Close one first."
            net = connection.execute("SELECT COALESCE(SUM(net_paise), 0) FROM stock_trades WHERE session_day=?", (day,)).fetchone()[0]
            if net * PAISA <= -cfg.stocks.capital_inr * cfg.risk.daily_loss_limit_pct / 100:
                return "The daily loss limit is reached."
            atr = self._series[tick.token].atr if tick.token in self._series else None
            distance = cfg.strategy_a.k_sl * atr if atr else tick.ltp * cfg.stocks.min_stop_pct / 100
            refusal = self._open(connection, _Candidate(tick, "manual", side, distance, cfg.strategy_a.k_tgt / cfg.strategy_a.k_sl),
                                 now, day)
            if refusal is None:
                self.status = "Managing the open position"
            return refusal

    def manual_exit(self, trade_id: int, at: datetime) -> str | None:
        with open_store(self.db) as connection:
            connection.execute("BEGIN IMMEDIATE")
            trade = connection.execute("SELECT * FROM stock_trades WHERE trade_id=? AND exit_at_utc IS NULL", (trade_id,)).fetchone()
            tick = self._ticks.get(trade["token"]) if trade else None
            if tick is None:
                return "No open position with a fresh quote to close."
            self._manage(connection, trade, tick, as_ist(at), "manual")
        return None

    def _breakouts(self, connection, ticks, day: str) -> list[_Candidate]:
        cfg, found, broken = self.config.strategy_b, [], []
        for row in connection.execute("SELECT * FROM stock_ranges WHERE session_day=? AND broken=0", (day,)).fetchall():
            tick = ticks.get(row["token"])
            if tick is None:
                continue
            opened, high, low = Decimal(row["open"]), Decimal(row["high"]), Decimal(row["low"])
            tolerance = opened * cfg.ohl_buffer_pct
            gap = opened - tick.previous_close
            for side, tagged, edge, opposite, direction in (("long", opened - low <= tolerance, high, low, 1),
                                                            ("short", high - opened <= tolerance, low, high, -1)):
                level = edge * (1 + direction * cfg.breakout_buffer_pct)
                if direction * (tick.ltp - level) < 0:
                    continue
                broken.append((day, row["token"]))  # only the first break of an edge is tradable
                if tagged and high > low and (not cfg.require_gap_agreement or direction * gap > 0):
                    entry = tick.ask if side == "long" else tick.bid
                    found.append(_Candidate(tick, "orb", side, direction * (entry - opposite), cfg.ohl_target_r))
        connection.executemany("UPDATE stock_ranges SET broken=1 WHERE session_day=? AND token=?", broken)
        return found

    def _crossovers(self, ticks) -> list[_Candidate]:
        cfg, found = self.config.strategy_a, []
        for token, tick in ticks.items():
            series = self._series[token]
            samples = list(series.samples)
            if len(samples) < cfg.volume_lookback + 2 or series.rsi is None or not series.atr:
                continue
            previous = samples[-2]
            volumes = [later.volume - earlier.volume for earlier, later in zip(samples, samples[1:])]
            if volumes[-1] <= 0 or volumes[-1] * cfg.volume_lookback < cfg.volume_multiple * sum(volumes[:-1]):
                continue
            for side, direction, band in (("long", 1, cfg.rsi_long_band), ("short", -1, cfg.rsi_short_band)):
                crossed = direction * (previous.ltp - previous.vwap) <= 0 < direction * (tick.ltp - tick.vwap)
                with_trend = direction * (tick.ltp - tick.previous_close) > 0
                if crossed and with_trend and band[0] <= series.rsi <= band[1]:
                    found.append(_Candidate(tick, "vwap", side, cfg.k_sl * series.atr, cfg.k_tgt / cfg.k_sl))
        return found

    # -- dashboard view -----------------------------------------------------------------------------
    def snapshot(self, at: datetime) -> dict:
        now, stocks = as_ist(at), self.config.stocks
        with open_store(self.db) as connection:
            rows = connection.execute("SELECT * FROM stock_trades WHERE session_day=? OR exit_at_utc IS NULL ORDER BY trade_id",
                                      (now.date().isoformat(),)).fetchall()
            stats = strategy_stats(connection)

        def view(row):
            mark = self._marks.get(row["token"])
            return {
                "trade_id": row["trade_id"], "symbol": row["symbol"], "strategy": row["strategy"], "side": row["side"], "quantity": row["quantity"],
                "entry_at": from_storage(row["entry_at_utc"]).isoformat(), "entry": row["entry_price"],
                "stop": row["stop"], "target": row["target"] or None, "mark": str(mark) if mark is not None else None,
                "exit_at": from_storage(row["exit_at_utc"]).isoformat() if row["exit_at_utc"] else None,
                "exit": row["exit_price"], "exit_reason": row["exit_reason"], "net_paise": row["net_paise"],
            }

        closed = [view(row) for row in rows if row["exit_at_utc"]]
        changes = {tick.symbol: float(round(100 * (tick.ltp / tick.previous_close - 1), 2)) for tick in self._ticks.values()}
        return {
            "status": self.status, "capital_paise": int(stocks.capital_inr / PAISA),
            "risk_paise": int(stocks.capital_inr * stocks.risk_per_trade_pct / 100 / PAISA),
            "loss_limit_paise": int(stocks.capital_inr * self.config.risk.daily_loss_limit_pct / 100 / PAISA),
            "net_paise": sum(trade["net_paise"] for trade in closed),
            "open": [view(row) for row in rows if not row["exit_at_utc"]], "trades": closed, "stats": stats,
            "watchlist": [row | {"today_pct": changes.get(row["symbol"])} for row in self.watchlist]
            if self._baseline_day == now.date() else [],
        }
