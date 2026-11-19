"""Read-only scan of NSE F&O stocks for the dashboard: top gainers, top losers and breadth."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
from time import monotonic, sleep

from orderbot.broker.angel_client import AngelClient, LoginError, RequestError, read_credentials
from orderbot.broker.history import HistoryCache, encode_minutes
from orderbot.broker.instruments import load_master
from orderbot.core.ist_time import IST, as_ist, now_ist
from orderbot.data.candle_builder import minute_floor
from orderbot.strategy.stock_backtest import backtest
from orderbot.core.activity import record
from orderbot.core.atomic_file import atomic_path
from orderbot.strategy.stock_paper import StockPaperTrader, baseline, daily_stats, watch_scores

BATCH = 50  # SmartAPI quote limit per request
SCAN_SECONDS = 60
# ponytail: open positions are re-quoted over REST every few seconds. Move them to the tick feed (ws_client)
# if stops need to react faster than this.
WATCH_SECONDS = 5
TOP = 8
HISTORY_SPAN_DAYS = 28  # one SmartAPI minute-history request; also covers the 14 sessions the baselines need
BACKTEST_LOOKBACK_DAYS = HISTORY_SPAN_DAYS  # earlier candles that supply the previous close and the baselines
BACKTEST_HISTORY = 20  # past runs kept for comparison
CHAIN_WINGS = 6  # strikes shown on each side of the at-the-money strike
LOGIN_ATTEMPTS = 3  # stop before repeated bad logins can lock the broker account


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


class MoversScanner:
    def __init__(self, config, directory: Path, *, client: AngelClient | None = None, clock=now_ist,
                 trader: StockPaperTrader | None = None):
        self.config, self.directory, self.clock, self.trader = config, directory, clock, trader
        self.client = client or AngelClient(config.broker)
        self.snapshot = {"state": "starting"}
        self._day = self._universe = self._credentials = self._session = None
        self._login_failures = 0
        self._baselines_for = None
        self.paused = False  # set by the dashboard's Disconnect, or after repeated login failures
        self._master, self.option_chain = None, None
        self._wake, self._force = Event(), False

    def __call__(self) -> dict:
        return self.snapshot

    def scan_once(self) -> None:
        now = self.clock()
        if self._day != now.date():
            self._master = load_master(self.config.data, self.config.market.underlying, now.date(), self.directory)
            cache = self.directory / self.config.data.instrument_cache_dir / f"instruments-{now.date().isoformat()}.json"
            self._universe = stock_universe(json.loads(cache.read_text(encoding="utf-8")))
            self._day = now.date()
        if self._session is None:
            self._credentials = read_credentials(self.config.broker, env_file=self.directory / ".env")
            self._session = self.client.login(self._credentials)
            self._login_failures = 0
        if self.trader is not None and self._baselines_for != now.date():
            self._baselines_for = now.date()
            Thread(target=self._prepare_baselines, args=(now.date(), dict(self._universe)), name="orderbot-baselines",
                   daemon=True).start()
        tokens, quotes = sorted(self._universe), []
        for start in range(0, len(tokens), BATCH):
            quotes += self.client.quotes(self._credentials, self._session, "NSE", tokens[start:start + BATCH])
        at = self.clock()
        self.snapshot = rank_movers(quotes, self._universe, at)
        record("Stock scan", f"Scanned {self.snapshot['quoted']} stocks · {self.snapshot['advancers']} up, "
                             f"{self.snapshot['decliners']} down")
        try:
            self.option_chain = self._option_chain(at)
        except (RequestError, KeyError, IndexError, TypeError, ValueError):
            if self.option_chain:  # keep showing the last prices, marked as old
                self.option_chain = self.option_chain | {"stale": True}
        if self.trader is not None:
            before = self.trader.status
            self.trader.on_scan(quotes, self._universe, at)
            if self.trader.status != before:
                record("Paper trader", self.trader.status)

    def _prepare_baselines(self, day: date, universe: dict[str, str]) -> None:
        """Give the trader each stock's 14-day ATR and average opening volume and the day's watchlist, from a
        per-day file or SmartAPI history."""
        stocks, market = self.config.stocks, self.config.market
        target = self.directory / self.config.data.history_cache_dir / f"stock-baselines-{day.isoformat()}.json"
        try:
            saved = json.loads(target.read_text(encoding="utf-8"))
            found, watchlist = saved["baselines"], saved["watchlist"]
            record("14-day history", f"Loaded today's saved figures for {len(found)} stocks")
        except (OSError, ValueError, KeyError, TypeError):
            found, history = {}, {}
            record("14-day history", f"Fetching minute history for {len(universe)} stocks")
            end = datetime.combine(day, market.session_open, IST)
            for count, (token, symbol) in enumerate(sorted(universe.items(), key=lambda item: item[1]), start=1):
                self.trader.baseline_status = f"Preparing 14-day history · {count} of {len(universe)} stocks"
                if count % 25 == 0:
                    record("14-day history", f"{count} of {len(universe)} stocks done")
                for attempt in range(5):
                    try:
                        rows = self.client.historical_minutes(self._credentials, self._session, "NSE", token,
                                                              end - timedelta(days=HISTORY_SPAN_DAYS), end)
                    except (RequestError, AttributeError):  # rate limits are routine; AttributeError means no session yet
                        sleep(2 + 2 * attempt)
                        continue
                    history[symbol] = daily_stats(rows, market.session_open, stocks.sip_range_minutes)
                    item = baseline(history[symbol], day.isoformat(), stocks.sip_baseline_days)
                    if item:
                        found[symbol] = [str(item[0]), str(item[1])]
                    break
                else:
                    record("14-day history", f"Gave up on {symbol} after 5 broker refusals", error=True)
            watchlist = watch_scores(history, day.isoformat(), stocks.sip_baseline_days, stocks.watchlist_size)
            with atomic_path(target) as temporary:
                temporary.write_text(json.dumps({"baselines": found, "watchlist": watchlist}), encoding="utf-8")
            record("14-day history", f"Ready · figures for {len(found)} of {len(universe)} stocks")
        self.trader.set_baselines(day, {symbol: (Decimal(atr), Decimal(volume)) for symbol, (atr, volume) in found.items()}, watchlist)
        record("Watchlist", "Today: " + ", ".join(row["symbol"] for row in watchlist) if watchlist else "No watchlist today")

    def _option_chain(self, at: datetime) -> dict:
        """NIFTY spot and the nearest expiry's calls and puts around the money, for viewing only."""
        master, step = self._master, float(self.config.options.strike_interval)
        index = self.client.quotes(self._credentials, self._session, master.spot.exchange, [master.spot.token])[0]
        spot = float(index["ltp"])
        expiry = min(option.expiry for option in master.options if option.expiry >= as_ist(at).date())
        atm = round(spot / step) * step
        wanted = {option.token: option for option in master.options
                  if option.expiry == expiry and abs(float(option.strike) - atm) <= CHAIN_WINGS * step}
        quotes = {quote["symbolToken"]: quote for quote in self.client.quotes(self._credentials, self._session, "NFO", sorted(wanted))}
        rows: dict[float, dict] = {}
        for token, option in wanted.items():
            if token in quotes:
                quote = quotes[token]
                rows.setdefault(float(option.strike), {})[option.right] = {
                    "price": float(quote["ltp"]), "pct": float(quote["percentChange"]),
                    "open_interest": int(quote["opnInterest"]), "volume": int(quote["tradeVolume"])}
        strikes = [{"strike": strike, "call": rows[strike].get("CE"), "put": rows[strike].get("PE")} for strike in sorted(rows)]
        calls = sum(row["call"]["open_interest"] for row in strikes if row["call"])
        puts = sum(row["put"]["open_interest"] for row in strikes if row["put"])
        money = rows.get(atm, {})
        return {
            "at": as_ist(at).isoformat(), "spot": spot, "spot_pct": float(index["percentChange"]), "expiry": expiry.isoformat(),
            "atm": atm, "lot_size": next(iter(wanted.values())).lot_size if wanted else None, "strikes": strikes,
            "straddle": money["CE"]["price"] + money["PE"]["price"] if "CE" in money and "PE" in money else None,
            "put_call_ratio": round(puts / calls, 2) if calls else None, "stale": False,
        }

    def trade_chart(self, trade_id: int) -> dict:
        """Minute closes of the session of one paper trade, with its entry, stop, target and exit."""
        trade = self.trader.trade(trade_id) if self.trader else None
        if trade is None or self._session is None:
            raise ValueError("That trade or the broker session is not available.")
        day, market = date.fromisoformat(trade["session_day"]), self.config.market
        start = datetime.combine(day, market.session_open, IST)
        end = min(datetime.combine(day, market.session_close, IST), minute_floor(self.clock()))
        for attempt in range(4):
            try:
                rows = self.client.historical_minutes(self._credentials, self._session, "NSE", trade["token"], start, end)
                break
            except RequestError:
                sleep(1 + 2 * attempt)
        else:
            raise ValueError("The broker did not return prices just now. Try again in a moment.")
        clock = lambda stamp: as_ist(datetime.fromisoformat(stamp)).strftime("%H:%M") if stamp else None  # noqa: E731
        return {
            "symbol": trade["symbol"], "day": trade["session_day"], "strategy": trade["strategy"], "side": trade["side"],
            "quantity": trade["quantity"], "entry": trade["entry_price"], "stop": trade["stop"], "target": trade["target"] or None,
            "exit": trade["exit_price"], "exit_reason": trade["exit_reason"], "net_paise": trade["net_paise"],
            "entry_at": clock(trade["entry_at_utc"]), "exit_at": clock(trade["exit_at_utc"]),
            "points": [[row[0][11:16], float(row[4])] for row in rows],
        }

    def backtest(self, symbols: list[str], start: date, end: date) -> dict:
        """Replay the stock strategies over SmartAPI minute history. Raises ValueError with a readable reason."""
        if self._session is None or not self._universe:
            raise ValueError("The broker session is not ready yet. Try again after the first stock scan.")
        tokens = {name: token for token, name in self._universe.items()}
        unknown = [symbol for symbol in symbols if symbol not in tokens]
        if unknown:
            raise ValueError("Not an F&O stock symbol: " + ", ".join(unknown) + ".")
        market = self.config.market
        now = minute_floor(self.clock())
        begin = datetime.combine(start - timedelta(days=BACKTEST_LOOKBACK_DAYS), market.session_open, IST)
        finish = min(datetime.combine(end, market.session_close, IST), now)
        today = datetime.combine(now.date(), market.session_open, IST)
        # Past sessions are cached on disk under a stable key; today's still-growing part is fetched separately.
        past, spans = min(finish, today), []
        while begin < past:  # each request stays inside SmartAPI's minute-history limit
            spans.append((begin, min(begin + timedelta(days=HISTORY_SPAN_DAYS), past)))
            begin = spans[-1][1]
        if finish > today:
            spans.append((today, finish))
        cache, candles = HistoryCache(self.config.data, self.directory), {}
        record("Backtest", f"Started · {len(symbols)} stocks, {start:%d/%m/%Y} to {end:%d/%m/%Y}")
        for symbol in symbols:
            candles[symbol] = [row for since, until in spans for row in encode_minutes(cache.get(
                tokens[symbol], since, until, lambda since=since, until=until: self.client.historical_minutes(
                    self._credentials, self._session, "NSE", tokens[symbol], since, until)))]
            record("Backtest", f"Fetched candles for {symbol} ({len(candles)} of {len(symbols)})")
        config = self.trader.config if self.trader else self.config  # the Settings screen's choices apply to backtests too
        result = backtest(config, candles, start)
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

    def _fail(self, state: str, message: str) -> None:
        self._session = None
        self.snapshot = self.snapshot | {"state": state, "message": message}
        record("Stock scan", message, error=True)

    def _market_open(self, now: datetime) -> bool:
        market = self.config.market
        return now.weekday() < 5 and market.session_open <= now.time() <= market.session_close

    def connection(self, at: datetime) -> dict:
        """The broker link as the dashboard shows it."""
        state = ("paused" if self.paused else "connecting" if self.snapshot.get("state") == "starting" else
                 "connected" if self._session is not None else "disconnected")
        seen = self.snapshot.get("at")
        if state == "connected" and self._market_open(at) and seen and (as_ist(at) - datetime.fromisoformat(seen)).total_seconds() > 180:
            state = "stale"
        return {"state": state, "observed_at": seen, "market_open": self._market_open(at), "controllable": True,
                "message": self.snapshot.get("message") if state in {"paused", "disconnected"} else None}

    def disconnect(self) -> None:
        """Stop talking to the broker until `connect`. Refused while a paper position needs prices to exit."""
        if self.trader is not None and self.trader.held():
            raise ValueError("Close the open paper position first. It needs live prices to reach its stop or target.")
        self.paused, self._session = True, None
        self.snapshot = self.snapshot | {"state": "paused", "message": "Disconnected from the dashboard."}
        record("Broker", "Disconnected from the dashboard")

    def connect(self) -> None:
        """Log in again and scan once straight away, even outside market hours."""
        self.paused, self._login_failures, self._force = False, 0, True
        self.snapshot = {key: value for key, value in self.snapshot.items() if key != "message"} | {"state": "starting"}
        record("Broker", "Connecting from the dashboard")
        self._wake.set()

    def run(self, stopped: Event) -> None:
        while not stopped.is_set():
            now = self.clock()
            forced, self._force = self._force, False
            if not self.paused and (self._market_open(now) or "at" not in self.snapshot or forced):
                try:
                    self.scan_once()
                except LoginError as exc:
                    self._login_failures += 1
                    if self._login_failures >= LOGIN_ATTEMPTS:
                        self.paused = True  # stop before repeated bad logins can lock the broker account
                        self._fail("stopped", f"{exc} Fix the login, then press Connect.")
                    else:
                        self._fail("error", str(exc))
                except RequestError as exc:
                    self._fail("error", str(exc))
                except sqlite3.Error:
                    self._fail("error", "Stock paper journal unavailable.")
                except (OSError, ValueError):
                    self._fail("error", "Instrument list unavailable.")
            deadline = monotonic() + SCAN_SECONDS
            while monotonic() < deadline and not stopped.is_set():
                if self._wake.wait(WATCH_SECONDS):  # Connect was pressed: scan now
                    self._wake.clear()
                    break
                self._watch_positions()

    def _watch_positions(self) -> None:
        """Between scans, check the stops and targets of open paper positions against fresh quotes."""
        if self.trader is None or self._session is None:
            return
        try:
            tokens = sorted({trade["token"] for trade in self.trader.held()})
            if tokens:
                self.trader.manage(self.client.quotes(self._credentials, self._session, "NSE", tokens), self._universe, self.clock())
        except (RequestError, sqlite3.Error):
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
