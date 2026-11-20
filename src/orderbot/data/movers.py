"""Read-only scan of NSE F&O stocks for the dashboard: top gainers, top losers and breadth."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial
import json
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
from time import monotonic

from orderbot.broker.angel_client import AngelClient, LoginError, RateLimitError, RequestError, read_credentials
from orderbot.broker.history import HistoryCache, HistoryError, encode_minutes, parse_minutes
from orderbot.broker.instruments import load_master
from orderbot.core.ist_time import IST, as_ist, now_ist, parse_exchange_timestamp
from orderbot.data.candle_builder import MINUTE, TradingSession, minute_floor
from orderbot.data.stock_history import StockHistory, stock_candle_days
from orderbot.data import candle_store
from orderbot.strategy.stock_backtest import backtest
from orderbot.core.activity import record
from orderbot.core.atomic_file import atomic_path
from orderbot.strategy.stock_paper import StockPaperTrader, baseline, daily_stats, watch_scores

SCAN_SECONDS = 60
# ponytail: open positions are re-quoted over REST every few seconds. Move them to the tick feed (ws_client)
# if stops need to react faster than this.
WATCH_SECONDS = 5
TOP = 8
BACKTEST_HISTORY = 20  # past runs kept for comparison
CHAIN_WINGS = 6  # strikes shown on each side of the at-the-money strike
LOGIN_ATTEMPTS = 3  # stop before repeated bad logins can lock the broker account


class _Disconnected(ValueError):
    """The broker session changed while a scan or request was in progress."""


def stock_universe(rows) -> dict[str, str]:
    """Cash-segment tokens of every stock that has listed futures, keyed token -> name."""
    names = {row.get("name") for row in rows if row.get("exch_seg") == "NFO" and row.get("instrumenttype") == "FUTSTK"}
    return {
        row["token"]: row["name"] for row in rows
        if row.get("exch_seg") == "NSE" and row.get("name") in names and "NSETEST" not in row["name"]
        and row.get("symbol") == row["name"] + "-EQ"
    }


def _row(quote, universe):
    ltp, high, low, vwap, pct = (float(quote[key]) for key in ("ltp", "high", "low", "avgPrice", "percentChange"))
    if not (ltp > 0 and high >= low > 0):
        raise ValueError("No trade yet.")
    position = (ltp - low) / (high - low) if high > low else 0.5
    # ponytail: watchlist tag from VWAP side and day-range position, not a backtested signal.
    # Replace with a tested stock strategy before anything trades on it.
    setup = ("holding_highs" if pct > 0 and ltp > vwap and position >= 0.75 else
             "holding_lows" if pct < 0 and ltp < vwap and position <= 0.25 else None)
    return {
        "symbol": universe[quote["symbolToken"]], "ltp": ltp, "pct": pct, "vwap": vwap, "high": high, "low": low,
        "range_position": round(min(max(position, 0), 1), 3),
        "turnover_cr": round(vwap * float(quote["tradeVolume"]) / 1e7, 1), "setup": setup,
        "feed_timestamp": quote.get("exchFeedTime"),
    }


def rank_movers(quotes, universe: dict[str, str], at: datetime, top: int = TOP) -> dict:
    rows = []
    for quote in quotes:
        try:
            rows.append(_row(quote, universe))
        except (KeyError, TypeError, ValueError):
            continue  # untraded, malformed or unrequested rows are left out of the ranking
    rows.sort(key=lambda row: row["pct"], reverse=True)
    gainers = [row for row in rows if row["pct"] > 0]
    losers = [row for row in rows if row["pct"] < 0]
    return {
        "state": "ok", "at": as_ist(at).isoformat(), "universe": len(universe), "quoted": len(rows),
        "advancers": len(gainers), "decliners": len(losers),
        "gainers": gainers[:top], "losers": losers[::-1][:top],
    }


def fixture_movers(path: Path, at: datetime) -> dict | None:
    data = json.loads(path.read_text(encoding="utf-8")).get("movers")
    return rank_movers(data["quotes"], data["universe"], at) if data else None


def quote_freshness(timestamp, at: datetime, session: TradingSession, max_age: float, *, disconnected=False) -> dict:
    try:
        feed = parse_exchange_timestamp(timestamp)
    except ValueError:
        feed = None
    now = as_ist(at)
    age = (now - feed).total_seconds() if feed else None
    if now.weekday() >= 5 or not session.contains(now):
        state = "market_closed"
    elif disconnected:
        state = "stale"
    elif feed is None:
        state = "unknown"
    elif not session.contains(feed) or not 0 <= age <= max_age:
        state = "stale"
    else:
        state = "fresh"
    return {"state": state, "feed_at": feed.isoformat() if feed else None, "age_seconds": int(age) if age is not None else None}


class MoversScanner:
    def __init__(self, config, directory: Path, *, client: AngelClient | None = None, clock=now_ist,
                 trader: StockPaperTrader | None = None):
        self.config, self.directory, self.clock, self.trader = config, directory, clock, trader
        self.client = client or AngelClient.for_workspace(config, directory)
        self.stock_history = StockHistory(config, HistoryCache(config.data, directory))
        self.snapshot = {"state": "starting"}
        self._stock_feed_timestamps = ()
        self._day = self._universe = self._credentials = self._session = None
        self._login_failures = 0
        self._baselines_for = None
        self.paused = False  # set by the dashboard's Disconnect, or after repeated login failures
        self._master, self.option_chain = None, None
        self._wake, self._force = Event(), False
        self._connection_lock, self._generation = RLock(), 0

    def __call__(self) -> dict:
        with self._connection_lock:
            snapshot, timestamps = self.snapshot, self._stock_feed_timestamps
            disconnected = self.paused or self._session is None or snapshot["state"] != "ok"
        if "at" not in snapshot:
            return snapshot
        at = as_ist(self.clock())
        session = TradingSession.from_config(at.date(), self.config.market, segment="cash")
        age_limit = self.config.reporting.rest_quote_max_age_seconds
        quote = partial(quote_freshness, at=at, session=session, max_age=age_limit, disconnected=disconnected)
        fresh = [quote(stamp) for stamp in timestamps]
        updated = max((item["feed_at"] for item in fresh if item["feed_at"]), default=None)
        return snapshot | {
            "fresh_quotes": sum(item["state"] == "fresh" for item in fresh),
            "quote_phase": self.config.market.cash_phase(at),
            "latest_feed_at": updated,
            **{side: [row | {"quote": quote(row.get("feed_timestamp"))} for row in snapshot[side]]
               for side in ("gainers", "losers")},
        }

    def option_snapshot(self, at: datetime) -> dict | None:
        with self._connection_lock:
            chain = self.option_chain
            disconnected = self.paused or self._session is None or (chain and chain.get("stale"))
        if not chain:
            return None
        now = as_ist(at)
        market = self.config.market
        expiry = date.fromisoformat(chain["expiry"])
        session = TradingSession.from_config(now.date(), market, segment="derivatives", index_expiry=expiry)
        spot_session = TradingSession.from_config(now.date(), market)
        limit = self.config.reporting.rest_quote_max_age_seconds
        spot = quote_freshness(chain.get("spot_timestamp"), now, spot_session, limit, disconnected=disconnected)
        strikes = []
        for row in chain["strikes"]:
            strikes.append(row | {side: row[side] | {"quote": quote_freshness(
                row[side].get("feed_timestamp"), now, session, limit, disconnected=disconnected)}
                if row[side] else None for side in ("call", "put")})
        legs = [leg["quote"] for row in strikes for leg in (row["call"], row["put"]) if leg]
        latest = max((leg["feed_at"] for leg in legs if leg["feed_at"]), default=None)
        fresh = sum(leg["state"] == "fresh" for leg in legs)
        return chain | {"strikes": strikes, "spot_quote": spot, "latest_feed_at": latest, "fresh_quotes": fresh,
                        "total_quotes": len(legs), "stale": not legs or fresh != len(legs),
                        "market_open": now.weekday() < 5 and session.contains(now), "close_at": session.closes_at.isoformat()}

    def _check(self, generation: int) -> None:
        with self._connection_lock:
            if self.paused or generation != self._generation:
                raise _Disconnected("The broker connection changed. Try again after the next stock scan.")

    def _fetch(self, generation, method, *args):
        with self._connection_lock:
            self._check(generation)
            credentials, session = self._credentials, self._session
            if session is None:
                raise _Disconnected("The broker session is not ready yet. Try again after the first stock scan.")
        result = method(credentials, session, *args, check=partial(self._check, generation))
        self._check(generation)
        return result

    def _quote_batches(self, generation, exchange, tokens):
        size = self.config.broker.rest_quote_token_limit
        for start in range(0, len(tokens), size):
            batch = tokens[start:start + size]
            yield batch, self._fetch(generation, self.client.quotes, exchange, batch)

    def _quotes(self, generation, exchange, tokens):
        return [quote for _, rows in self._quote_batches(generation, exchange, tokens) for quote in rows]

    def scan_once(self, generation: int | None = None, *, refresh_cash=True) -> None:
        with self._connection_lock:
            generation = self._generation if generation is None else generation
            self._check(generation)
            session = self._session
        now = as_ist(self.clock())
        if self._day != now.date():
            if self._day is not None:
                session = None  # SmartAPI's data session is dated to the prior IST day.
            self._master = load_master(self.config.data, self.config.market.underlying, now.date(), self.directory)
            cache = self.directory / self.config.data.instrument_cache_dir / f"instruments-{now.date().isoformat()}.json"
            self._universe = stock_universe(json.loads(cache.read_text(encoding="utf-8")))
            self._day = now.date()
        if session is None:
            credentials = read_credentials(self.config.broker, env_file=self.directory / ".env")
            session = self.client.login(credentials, check=partial(self._check, generation))
            with self._connection_lock:
                self._check(generation)
                self._credentials, self._session = credentials, session
                self._login_failures = 0
        if self.trader is not None and self._baselines_for != now.date():
            self._baselines_for = now.date()
            Thread(target=self._prepare_baselines, args=(now.date(), dict(self._universe), generation), name="orderbot-baselines",
                   daemon=True).start()
        if refresh_cash:
            quotes = self._quotes(generation, "NSE", sorted(self._universe))
            at = as_ist(self.clock())
            with self._connection_lock:
                self._check(generation)
                self.snapshot = rank_movers(quotes, self._universe, at)
                self._stock_feed_timestamps = tuple(row.get("exchFeedTime") for row in quotes)
                record("Stock scan", f"Scanned {self.snapshot['quoted']} stocks · {self.snapshot['advancers']} up, "
                                     f"{self.snapshot['decliners']} down")
                if self.trader is not None:
                    before = self.trader.status
                    self.trader.on_scan(quotes, self._universe, at)
                    if self.trader.status != before:
                        record("Paper trader", self.trader.status)
        try:
            chain = self._option_chain(as_ist(self.clock()), generation)
        except (_Disconnected, RateLimitError):
            raise
        except (RequestError, KeyError, IndexError, TypeError, ValueError):
            chain = self.option_chain | {"stale": True} if self.option_chain else None
        with self._connection_lock:
            self._check(generation)
            self.option_chain = chain

    def _prepare_baselines(self, day: date, universe: dict[str, str], generation: int | None = None) -> None:
        generation = self._generation if generation is None else generation
        try:
            self._load_baselines(day, universe, generation)
        except (_Disconnected, RateLimitError, OSError) as exc:
            self._baselines_for = None
            self.trader.baseline_status = "14-day history paused. Waiting for the next scan."
            record("14-day history", str(exc) if isinstance(exc, RateLimitError) else self.trader.baseline_status, error=True)

    def _load_baselines(self, day, universe, generation):
        """Give the trader each stock's 14-day ATR and average opening volume and the day's watchlist, from a
        per-day file or SmartAPI history."""
        stocks, market = self.config.stocks, self.config.market
        target = self.directory / self.config.data.history_cache_dir / f"stock-baselines-complete-{day.isoformat()}.json"
        try:
            saved = json.loads(target.read_text(encoding="utf-8"))
            found, watchlist = saved["baselines"], saved["watchlist"]
            record("14-day history", f"Loaded today's saved figures for {len(found)} stocks")
        except (OSError, ValueError, KeyError, TypeError):
            found, history = {}, {}
            record("14-day history", f"Loading local minute history for {len(universe)} stocks")
            end = datetime.combine(day, market.session_open, IST)
            start = end - timedelta(days=self.config.data.stock_history_span_days)
            for count, (token, symbol) in enumerate(sorted(universe.items(), key=lambda item: item[1]), start=1):
                self._check(generation)
                self.trader.baseline_status = f"Preparing 14-day history · {count} of {len(universe)} stocks"
                if count % 25 == 0:
                    record("14-day history", f"{count} of {len(universe)} stocks done")
                try:
                    fetch = None
                    if self.config.data.stock_baselines_fetch_missing:
                        fetch = lambda since, until: self._stock_minutes(generation, token, since, until)
                    candles = self.stock_history.get(symbol, token, start, end, fetch)
                except HistoryError as exc:
                    record("14-day history", f"{symbol}: {exc}", error=True)
                    continue
                history[symbol] = daily_stats(stock_candle_days(candles, market), stocks.sip_range_minutes)
                item = baseline(history[symbol], day.isoformat(), stocks.sip_baseline_days)
                if item:
                    found[symbol] = [str(item[0]), str(item[1])]
            watchlist = watch_scores(history, day.isoformat(), stocks.sip_baseline_days, stocks.watchlist_size)
            with atomic_path(target) as temporary:
                temporary.write_text(json.dumps({"baselines": found, "watchlist": watchlist}), encoding="utf-8")
            record("14-day history", f"Ready · figures for {len(found)} of {len(universe)} stocks")
        with self._connection_lock:
            self._check(generation)
            self.trader.set_baselines(day, {symbol: (Decimal(atr), Decimal(volume)) for symbol, (atr, volume) in found.items()}, watchlist)
        record("Watchlist", "Today: " + ", ".join(row["symbol"] for row in watchlist) if watchlist else "No watchlist today")

    def _option_chain(self, at: datetime, generation: int) -> dict:
        """NIFTY spot and the nearest expiry's calls and puts around the money, for viewing only."""
        master, step = self._master, float(self.config.options.strike_interval)
        index = self._quotes(generation, master.spot.exchange, [master.spot.token])[0]
        spot = float(index["ltp"])
        expiry = min(option.expiry for option in master.options if option.expiry >= as_ist(at).date())
        atm = round(spot / step) * step
        wanted = {option.token: option for option in master.options
                  if option.expiry == expiry and abs(float(option.strike) - atm) <= CHAIN_WINGS * step}
        quotes = {quote["symbolToken"]: quote for quote in self._quotes(generation, "NFO", sorted(wanted))}
        rows: dict[float, dict] = {}
        for token, option in wanted.items():
            if token in quotes:
                quote = quotes[token]
                rows.setdefault(float(option.strike), {})[option.right] = {
                    "price": float(quote["ltp"]), "pct": float(quote["percentChange"]),
                    "open_interest": int(quote["opnInterest"]), "volume": int(quote["tradeVolume"]),
                    "feed_timestamp": quote.get("exchFeedTime")}
        strikes = [{"strike": strike, "call": rows[strike].get("CE"), "put": rows[strike].get("PE")} for strike in sorted(rows)]
        calls = sum(row["call"]["open_interest"] for row in strikes if row["call"])
        puts = sum(row["put"]["open_interest"] for row in strikes if row["put"])
        money = rows.get(atm, {})
        return {
            "at": as_ist(self.clock()).isoformat(), "spot": spot, "spot_pct": float(index["percentChange"]),
            "spot_timestamp": index.get("exchFeedTime"), "expiry": expiry.isoformat(),
            "atm": atm, "lot_size": next(iter(wanted.values())).lot_size if wanted else None, "strikes": strikes,
            "straddle": money["CE"]["price"] + money["PE"]["price"] if "CE" in money and "PE" in money else None,
            "put_call_ratio": round(puts / calls, 2) if calls else None, "stale": False,
        }

    def _stock_minutes(self, generation, token, start, end):
        return self._fetch(generation, self.client.historical_minutes, "NSE", token, start, end)

    def verify_market_data(self, *, stage="all", candle_end=None, segment=None) -> dict:
        """Sample real NSE and NFO data through the running dashboard's shared SmartAPI session."""
        if stage not in {"all", "quotes", "candles"} or (
                segment is not None and (stage != "candles" or segment not in {"cash", "derivatives"})):
            raise ValueError("Choose a valid market verification stage and segment.")
        config, now = (self.trader.config if self.trader else self.config), as_ist(self.clock())
        if config.runtime.mode != "paper" or config.runtime.live_enabled or any(
                budget.capital_inr is not None or budget.risk_per_trade_pct is not None
                for budget in (config.risk, config.stocks)):
            raise ValueError("Market verification requires paper mode with both budgets unset.")
        cash = TradingSession.from_config(now.date(), config.market, segment="cash")
        if now.weekday() >= 5 or not cash.contains(now):
            raise ValueError("The verification requires the cash and F&O regular sessions to be open.")
        settings = config.paper_trial
        finalized = minute_floor(now - timedelta(seconds=settings.finalize_delay_seconds))
        end = as_ist(candle_end) if candle_end is not None else finalized
        start = end - settings.market_verification_minutes * MINUTE
        if not cash.opens_at <= start < end <= finalized or minute_floor(end) != end:
            raise ValueError("The candle verification window must contain finalized minutes from today's regular session.")
        with self._connection_lock:
            generation, master, universe = self._generation, self._master, self._universe
            if self.paused or not self._session or not master or not universe or self._day != now.date():
                raise ValueError("Wait for today's first connected stock scan.")
        name = settings.market_verification_symbol
        stock = next((token for token, symbol in universe.items() if symbol == name), None)
        if stock is None:
            raise ValueError(f"{name} is unavailable in the current instrument master.")
        future = master.front_future(now.date())
        instruments = {"cash": (name, "NSE", stock), "derivatives": (future.symbol, "NFO", future.token)}
        pending = [segment] if segment else list(instruments)
        report = {"mode": "PAPER", "risk_configured": False, "checked_at": now.isoformat(),
                  "quotes": {}, "candles": {}, "issues": [], "pending_candles": pending,
                  "candle_window": {"start": start.isoformat(), "end_exclusive": end.isoformat()}}
        if stage != "candles" and not self._verification_quotes(report, config, generation, master, future, stock, cash):
            return report
        if stage == "quotes":
            return report
        for market in tuple(pending):
            symbol, exchange, token = instruments[market]
            try:
                raw = self._fetch(generation, self.client.historical_minutes, exchange, token, start, end)
                bars = {bar.at: bar for bar in parse_minutes(raw, start, end)}
            except RateLimitError as exc:
                self._verification_cooldown(report, f"{market}_candles", exc)
                break
            except _Disconnected as exc:
                report["issues"].append(f"{symbol} history: {exc}")
                break
            except (RequestError, HistoryError) as exc:
                report["issues"].append(f"{symbol} history: {exc}")
                continue
            self._verification_candles(report, symbol, start, end, bars)
            pending.remove(market)
        return report

    def _verification_cooldown(self, report, stage, error):
        delay = error.retry_after_seconds or self.config.broker.rest_rate_limit_cooldown_seconds
        resume = as_ist(self.clock()) + timedelta(seconds=delay)
        report["cooldown"] = {"stage": stage, "retry_after_seconds": delay, "resume_at_ist": resume.isoformat()}
        report["issues"].append(f"{stage}: {error}")

    def _verification_quotes(self, report, config, generation, master, future, stock, cash):
        spot_key = "NIFTY spot"
        market = config.market
        cash_targets = {stock: (config.paper_trial.market_verification_symbol, cash),
                        master.spot.token: (spot_key, TradingSession.from_config(cash.day, market))}
        for exchange in ("NSE", "NFO"):
            stage = "cash_quotes" if exchange == "NSE" else "derivatives_quotes"
            targets = cash_targets
            if exchange == "NFO":
                spot = report["quotes"].get(spot_key, {}).get("ltp")
                try:
                    spot = float(spot)
                except (ValueError, TypeError):
                    spot = 0.0
                contracts = [future]
                if spot > 0:
                    expiry = min((item.expiry for item in master.options if item.expiry >= cash.day), default=None)
                    listed = [item for item in master.options if item.expiry == expiry]
                    if listed:
                        strike = min((item.strike for item in listed), key=lambda value: abs(float(value) - spot))
                        contracts += [item for item in listed if item.strike == strike]
                if len(contracts) == 1:
                    report["issues"].append("The at-the-money options could not be selected for the quote check.")
                targets = {item.token: (item.symbol, TradingSession.from_config(
                    cash.day, market, segment="derivatives", index_expiry=item.expiry)) for item in contracts}
            try:
                for batch, raw in self._quote_batches(generation, exchange, list(targets)):
                    received = as_ist(self.clock())
                    rows = {row["symbolToken"]: row for row in raw}
                    for token in batch:
                        symbol, window = targets[token]
                        row = rows.get(token)
                        state = quote_freshness(row.get("exchFeedTime") if row else None, received, window,
                                                config.stocks.max_quote_age_seconds)
                        report["quotes"][symbol] = state | {"ltp": row.get("ltp") if row else None,
                                                              "received_at_ist": received.isoformat()}
                        if not row or state["state"] != "fresh":
                            report["issues"].append(f"{symbol}: {'missing quote' if not row else state['state']}")
            except RateLimitError as exc:
                self._verification_cooldown(report, stage, exc)
                return False
            except (RequestError, _Disconnected) as exc:
                report["issues"].append(f"{stage}: {exc}")
                return False
        return True

    @staticmethod
    def _verification_candles(report, symbol, start, end, bars):
        expected = {start + index * MINUTE for index in range((end - start) // MINUTE)}
        local = {}
        local_state = "no_overlap"
        try:
            raw = candle_store.load(symbol, start.date(), end.date())
            local = {bar.at: bar for bar in parse_minutes(raw, start, end)}
        except (OSError, ValueError, TypeError):
            local_state = "unavailable"
            report["issues"].append(f"{symbol} local history: data is unavailable or malformed.")
        overlap = bars.keys() & local.keys()
        missing = [stamp.isoformat() for stamp in sorted(expected - bars.keys())]
        mismatches = [stamp.isoformat() for stamp in sorted(overlap) if bars[stamp] != local[stamp]]
        report["candles"][symbol] = {"source": "Angel One ONE_MINUTE", "start": start.isoformat(),
                                      "end_exclusive": end.isoformat(), "valid": len(bars), "missing": missing,
                                      "local_overlap": len(overlap), "local_mismatches": mismatches,
                                      "local_comparison": "compared" if overlap else local_state}
        if missing or mismatches:
            report["issues"].append(f"{symbol} history: {len(missing)} missing, {len(mismatches)} local mismatches")

    def trade_chart(self, trade_id: int) -> dict:
        """Minute closes of the session of one paper trade, with its entry, stop, target and exit."""
        trade = self.trader.trade(trade_id) if self.trader else None
        with self._connection_lock:
            generation, session = self._generation, self._session
        if trade is None:
            raise ValueError("That stock trade is not available.")
        day, market = date.fromisoformat(trade["session_day"]), self.config.market
        start = datetime.combine(day, market.session_open, IST)
        end = min(datetime.combine(day, market.cash_close(day), IST), minute_floor(self.clock()))
        fetch = (lambda since, until: self._stock_minutes(generation, trade["token"], since, until)) if session else None
        rows = self.stock_history.get(trade["symbol"], trade["token"], start, end, fetch)
        if not rows:
            raise ValueError("No closed stock candles are available for that trade.")
        clock = lambda stamp: as_ist(datetime.fromisoformat(stamp)).strftime("%H:%M") if stamp else None  # noqa: E731
        return {
            "symbol": trade["symbol"], "day": trade["session_day"], "strategy": trade["strategy"], "side": trade["side"],
            "quantity": trade["quantity"], "entry": trade["entry_price"], "stop": trade["stop"], "target": trade["target"] or None,
            "exit": trade["exit_price"], "exit_reason": trade["exit_reason"], "net_paise": trade["net_paise"],
            "entry_at": clock(trade["entry_at_utc"]), "exit_at": clock(trade["exit_at_utc"]),
            "points": [[row.at.strftime("%H:%M"), float(row.close)] for row in rows],
        }

    def backtest(self, symbols: list[str], start: date, end: date) -> dict:
        """Replay the stock strategies over SmartAPI minute history. Raises ValueError with a readable reason."""
        config = self.trader.config if self.trader else self.config
        if config.stocks.capital_inr is None or config.stocks.risk_per_trade_pct is None:
            raise ValueError("Set stock paper capital and risk per trade before running a backtest.")
        with self._connection_lock:
            generation, session, universe = self._generation, self._session, self._universe
        tokens = {name: token for token, name in (universe or {}).items()}
        known = set(tokens) | set(candle_store.symbols())
        unknown = [symbol for symbol in symbols if symbol not in known]
        if unknown:
            raise ValueError("Not an F&O stock symbol: " + ", ".join(unknown) + ".")
        market = self.config.market
        now = minute_floor(self.clock())
        begin = datetime.combine(start - timedelta(days=self.config.data.stock_history_span_days), market.session_open, IST)
        finish = min(datetime.combine(end, market.cash_close(end), IST), now)
        candles = {}
        record("Backtest", f"Started · {len(symbols)} stocks, {start:%d/%m/%Y} to {end:%d/%m/%Y}")
        for symbol in symbols:
            token = tokens.get(symbol)
            fetch = (lambda since, until: self._stock_minutes(generation, token, since, until)) if session and token else None
            candles[symbol] = encode_minutes(self.stock_history.get(symbol, token, begin, finish, fetch))
            if not candles[symbol]:
                raise ValueError(f"No stock candle data is available for {symbol} in that range.")
            record("Backtest", f"Loaded candles for {symbol} ({len(candles)} of {len(symbols)})")
        result = backtest(config, candles, start, through=finish)
        record("Backtest", f"Finished · {len(result['trades'])} trades over {result['sessions']} sessions")
        self._remember(result, config)
        return result

    def _history_path(self) -> Path:
        return (self.trader.db if self.trader else self.directory / self.config.storage.sqlite_path).with_name("backtest-history.json")

    def backtest_history(self) -> list[dict]:
        """Summaries of the last backtests, newest first, for side-by-side comparison."""
        try:
            return json.loads(self._history_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _remember(self, result: dict, config) -> None:
        stocks = config.stocks
        entry = {
            "at": self.clock().isoformat(), "symbols": result["symbols"], "from": result["from"], "to": result["to"],
            "sessions": result["sessions"], "stats": result["stats"],
            "settings": {"capital_inr": str(stocks.capital_inr), "risk_per_trade_pct": str(stocks.risk_per_trade_pct),
                         "max_positions": stocks.max_positions, "sip_entries": stocks.sip_entries, "vwap_entries": stocks.vwap_entries,
                         "exit_on_rsi": stocks.exit_on_rsi, "exit_on_vwap_cross": stocks.exit_on_vwap_cross,
                         "watchlist_only": stocks.watchlist_only},
        }
        with atomic_path(self._history_path()) as temporary:
            temporary.write_text(json.dumps([entry, *self.backtest_history()][:BACKTEST_HISTORY]), encoding="utf-8")

    def _fail(self, state: str, message: str, *, keep_session=False) -> None:
        if not keep_session:
            self._session = None
            self._generation += 1
        if self.trader:
            self.trader.invalidate_quotes()
        if self.option_chain:
            self.option_chain = self.option_chain | {"stale": True}
        self.snapshot = self.snapshot | {"state": state, "message": message}
        record("Stock scan", message, error=True)

    def _market_open(self, now: datetime) -> bool:
        now = as_ist(now)
        market = self.config.market
        return now.weekday() < 5 and market.session_open <= now.time() < market.fo_close(now.date())

    def connection(self, at: datetime) -> dict:
        """The broker link as the dashboard shows it."""
        state = ("paused" if self.paused else "rate_limited" if self.snapshot.get("state") == "rate_limited" else
                 "connecting" if self.snapshot.get("state") == "starting" else
                 "connected" if self._session is not None else "disconnected")
        seen = max(filter(None, (self.snapshot.get("at"), (self.option_chain or {}).get("at"))), default=None)
        if state == "connected" and self._market_open(at) and seen and (as_ist(at) - datetime.fromisoformat(seen)).total_seconds() > 180:
            state = "stale"
        now, market = as_ist(at), self.config.market
        return {"state": state, "observed_at": seen, "market_open": self._market_open(at), "controllable": True,
                "cash_phase": market.cash_phase(now), "cash_close": market.cash_close(now.date()).strftime("%H:%M"),
                "fo_close": market.fo_close(now.date()).strftime("%H:%M"),
                "message": self.snapshot.get("message") if state in {"paused", "disconnected", "rate_limited"} else None}

    def disconnect(self) -> None:
        """Stop talking to the broker until `connect`. Refused while a paper position needs prices to exit."""
        with self._connection_lock:
            if self.trader is not None:
                self.trader.invalidate_quotes(require_flat=True)
            self.paused, self._session = True, None
            self._generation += 1
            if self.option_chain:
                self.option_chain = self.option_chain | {"stale": True}
            self.snapshot = self.snapshot | {"state": "paused", "message": "Disconnected from the dashboard."}
            record("Broker", "Disconnected from the dashboard")

    def connect(self) -> None:
        """Log in again and scan once straight away, even outside market hours."""
        with self._connection_lock:
            self.paused, self._login_failures, self._force = False, 0, True
            self.snapshot = {key: value for key, value in self.snapshot.items() if key != "message"} | {"state": "starting"}
            record("Broker", "Connecting from the dashboard")
            self._wake.set()

    def run(self, stopped: Event) -> None:
        while not stopped.is_set():
            now = self.clock()
            with self._connection_lock:
                force = self._force or "at" not in self.snapshot
                should_scan = not self.paused and (self._market_open(now) or force)
                self._force = False
                generation = self._generation
            if should_scan:
                try:
                    self.scan_once(generation, refresh_cash=force or self.config.market.cash_phase(now) == "continuous")
                except _Disconnected:
                    pass
                except (LoginError, RequestError, sqlite3.Error, OSError, ValueError) as exc:
                    with self._connection_lock:
                        if generation == self._generation and not self.paused:
                            self._scan_failed(exc)
            deadline = monotonic() + SCAN_SECONDS
            while monotonic() < deadline and not stopped.is_set():
                if self._wake.wait(WATCH_SECONDS):  # Connect was pressed: scan now
                    self._wake.clear()
                    break
                self._watch_positions()

    def _scan_failed(self, exc) -> None:
        if isinstance(exc, LoginError):
            self._login_failures += 1
            if self._login_failures >= LOGIN_ATTEMPTS:
                self.paused = True
                self._fail("stopped", f"{exc} Fix the login, then press Connect.")
                return
        message = (str(exc) if isinstance(exc, (LoginError, RequestError)) else
                   "Stock paper journal unavailable." if isinstance(exc, sqlite3.Error) else "Instrument list unavailable.")
        throttled = isinstance(exc, RateLimitError)
        self._fail("rate_limited" if throttled else "error", message, keep_session=throttled)

    def _watch_positions(self) -> None:
        """Between scans, check the stops and targets of open paper positions against fresh quotes."""
        with self._connection_lock:
            if (self.trader is None or self._session is None or self.paused
                    or self.config.market.cash_phase(self.clock()) != "continuous"):
                return
            generation = self._generation
        try:
            tokens = sorted({trade["token"] for trade in self.trader.held()})
            if tokens:
                quotes = self._quotes(generation, "NSE", tokens)
                with self._connection_lock:
                    self._check(generation)
                    self.trader.manage(quotes, self._universe, self.clock())
        except (_Disconnected, RequestError, sqlite3.Error):
            pass  # the next full scan reports a broker or journal problem


@contextmanager
def movers_worker(config, directory: Path, db: Path):
    trader = StockPaperTrader(config, db, activity=record, settings_path=db.with_name("stock-settings.json")) \
        if config.stocks.paper_trading else None
    scanner, stopped = MoversScanner(config, directory, trader=trader), Event()
    Thread(target=scanner.run, args=(stopped,), name="orderbot-movers", daemon=True).start()
    try:
        yield scanner
    finally:
        stopped.set()
        scanner._wake.set()
