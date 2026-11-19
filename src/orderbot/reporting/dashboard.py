from datetime import date, datetime, timedelta
from functools import partial
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
from secrets import token_urlsafe
from sqlite3 import Error as DatabaseError
from urllib.parse import parse_qs, urlsplit

from orderbot.broker.angel_client import RequestError
from orderbot.core.activity import recent, record
from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist, from_storage, now_ist, parse_timestamp
from orderbot.core.money import PAISA, round_inr
from orderbot.core.state_store import open_store
from orderbot.execution.exit_store import ExitProgress, ExitStore
from orderbot.execution.journal import Journal
from orderbot.strategy.stop_plan import StopPlan
from orderbot.strategy.vwap_rsi import GATES

from .alerts import AlertOutbox
from .daily_report import exact_paise, ist_bounds, report_in


def _position_view(position, detail, state, now, config):
    premium_stop, underlying_stop = position.premium_stop, None
    try:
        plan = StopPlan.from_view(detail.get("execution_stop_plan") or detail["stop_plan"])
        plan = plan.with_entry_fill(position.average_price)
        underlying_stop, premium_stop = str(plan.underlying_stop), plan.premium_stop
    except (KeyError, ValueError, TypeError, ArithmeticError):
        pass
    if state.trail.stop is not None:
        premium_stop = max(premium_stop, state.trail.stop)
    stamp = state.last_tick_at
    return position.view() | {
        "premium": str(state.last_ltp) if state.last_ltp is not None else None,
        "premium_at": stamp.isoformat() if stamp is not None else None,
        "premium_stale": stamp is None or not 0 <= (now - stamp).total_seconds() < config.options.quote_max_age_seconds,
        "underlying_stop": underlying_stop, "premium_stop": str(round_inr(premium_stop)), "exit_reason": state.reason,
    }


def status_snapshot(config: Config, path: Path, at: datetime, *, fixture: bool) -> dict:
    now = as_ist(at)
    start, end = ist_bounds(now.date(), now)
    available = config.risk.capital_inr
    loss_limit = available * config.risk.daily_loss_limit_pct / 100 if available is not None else None
    with open_store(path) as connection:
        connection.execute("BEGIN")
        report = report_in(connection, now.date(), now)
        ledger = Journal(path, config.costs).read_in(connection)
        positions = []
        for position in ledger.positions:
            row = connection.execute("""
                SELECT d.detail_json, p.state_json FROM decisions d LEFT JOIN exit_progress p USING (decision_id)
                WHERE d.decision_id=?
            """, (position.decision_id,)).fetchone()
            state = ExitProgress.from_view(json.loads(row["state_json"])) if row["state_json"] else ExitProgress()
            positions.append(_position_view(position, json.loads(row["detail_json"]), state, now, config))
        broker = {"state": "disconnected", "observed_at": None}
        feed = connection.execute("SELECT * FROM feed_status WHERE singleton=1").fetchone()
        if feed is not None:
            seen = from_storage(feed["observed_at_utc"])
            state = feed["state"]
            if state in {"connected", "connecting", "reconnecting"} and not (
                0 <= (now - seen).total_seconds() < config.reporting.feed_status_max_age_seconds
            ):
                state = "stale"
            broker = {"state": state, "observed_at": seen.isoformat()}
        gates, candle = {}, None
        row = connection.execute("""
            SELECT detail_json FROM decisions
            WHERE at_utc >= ? AND at_utc < ? AND strategy='vwap'
              AND json_type(detail_json, '$.gates.closed_candle') = 'object'
            ORDER BY at_utc DESC, rowid DESC LIMIT 1
        """, (start, end)).fetchone()
        if row is not None:
            detail = json.loads(row[0])
            gates = detail["gates"]
            candle = parse_timestamp(detail["candle_at"]).isoformat() if detail.get("candle_at") else None
        alerts = AlertOutbox.recent_in(connection, config.reporting.alert_history_limit)
        kill = connection.execute("SELECT reason FROM kill_switch_latches WHERE session_day=?", (now.date().isoformat(),)).fetchone()
    return {
        "mode": "PAPER", "fixture": fixture, "now": now.isoformat(), "broker": broker,
        "halt": kill["reason"] if kill is not None else ledger.halts.get(now.date()),
        "stop_latched": kill is not None, "resumable": kill is not None and kill["reason"] == "dashboard_stop",
        "positions": positions,
        "daily_loss_limit": exact_paise(round_inr(loss_limit)) if loss_limit is not None else None,
        "daily_loss_limit_reached": loss_limit is not None and report["net_paise"] * PAISA <= -loss_limit,
        "risk_configured": available is not None and config.risk.risk_per_trade_pct is not None,
        "net_paise": report["net_paise"], "reconciliation": report["reconciliation"],
        "trades": report["trades"], "skips": report["skips"], "candle_at": candle,
        "gates": [{"name": name, "status": gates.get(name, {}).get("status", "not_evaluated")} for name in GATES],
        "alerts": alerts, "telegram_enabled": config.alerts.telegram_enabled and not fixture,
    }


MAX_BACKTEST_SYMBOLS = 25
MAX_BACKTEST_DAYS = 31

_STATIC = {"/": ("index.html", "text/html"), "/dashboard.css": ("dashboard.css", "text/css"),
           "/dashboard.js": ("dashboard.js", "text/javascript")}


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, config, path, clock, fixture, stop_token, movers, stocks, backtest, chart, **kwargs):
        self.config, self.db, self.clock, self.fixture, self.stop_token = config, path, clock, fixture, stop_token
        self.movers, self.stocks, self.backtest, self.chart = movers, stocks, backtest, chart
        super().__init__(*args, **kwargs)

    @property
    def host(self):
        return f"127.0.0.1:{self.server.server_port}"

    def log_message(self, *_args):
        pass

    def _send(self, code, kind, data):
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, value):
        self._send(code, "application/json", json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.headers.get("Host") != self.host:
            self._json(403, {"error": "Use the local dashboard address."})
        elif self.path in _STATIC:
            name, kind = _STATIC[self.path]
            data = files("orderbot.reporting.web").joinpath(name).read_bytes()
            if name == "index.html":
                data = data.replace(b"{{refresh_seconds}}", str(self.config.reporting.refresh_seconds).encode("ascii"))
            self._send(200, kind, data)
        elif self.path.startswith("/api/stock-chart?"):
            try:
                if self.chart is None:
                    raise ValueError("Trade charts are off in this session.")
                self._json(200, self.chart(int(parse_qs(urlsplit(self.path).query)["trade_id"][0])))
            except (ValueError, KeyError, IndexError) as exc:
                self._json(400, {"error": str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else "Malformed request."})
            except DatabaseError:
                self._json(503, {"error": "The paper journal is unavailable."})
        elif self.path == "/api/status":
            try:
                value = status_snapshot(self.config, self.db, self.clock(), fixture=self.fixture)
                value["stocks"] = self.stocks.snapshot(self.clock()) if self.stocks else None
            except (DatabaseError, ValueError, KeyError, TypeError, ArithmeticError):
                self._json(503, {"error": "Journal snapshot unavailable."})
            else:
                movers = self.movers() if self.movers else None
                # With no option tick feed running, a fresh stock scan is the evidence that the broker is reachable.
                if value["broker"]["state"] in {"disconnected", "stale"} and movers and movers.get("state") == "ok" and (
                    0 <= (self.clock() - parse_timestamp(movers["at"])).total_seconds() < 180
                ):
                    value["broker"] = {"state": "connected", "observed_at": movers["at"]}
                self._json(200, value | {"stop_token": self.stop_token, "movers": movers, "activity": recent()})
        else:
            self._json(404, {"error": "Page not found."})

    def _action(self, body: dict) -> dict:
        if self.stocks is None or self.backtest is None:
            raise ValueError("Stock paper trading is off in this session.")
        if self.path == "/api/backtest":
            symbols = [name.strip().upper() for name in body["symbols"].split(",") if name.strip()]
            start, end = date.fromisoformat(body["from"]), date.fromisoformat(body["to"])
            if not 1 <= len(symbols) <= MAX_BACKTEST_SYMBOLS:
                raise ValueError(f"Enter 1 to {MAX_BACKTEST_SYMBOLS} stock symbols separated by commas.")
            if not start <= end <= self.clock().date() or end - start > timedelta(days=MAX_BACKTEST_DAYS):
                raise ValueError(f"Choose a past date range of at most {MAX_BACKTEST_DAYS} days.")
            return self.backtest(symbols, start, end)
        if self.path == "/api/stock-entry":
            refusal = self.stocks.manual_entry(body["symbol"].strip().upper(), body["side"], self.clock())
        else:
            refusal = self.stocks.manual_exit(int(body["trade_id"]), self.clock())
        if refusal:
            raise ValueError(refusal)
        return {"ok": True}

    def do_POST(self):
        token = self.headers.get("X-OrderBOT-Stop", "").encode("utf-8")
        if self.headers.get("Host") != self.host or self.headers.get("Origin") != f"http://{self.host}" or not (
            compare_digest(token, self.stop_token.encode("ascii"))
        ):
            self._json(403, {"error": "Use the stop control on the local dashboard."})
        elif self.path in {"/api/stock-entry", "/api/stock-exit", "/api/backtest"}:
            try:
                self._json(200, self._action(json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)))))
            except (ValueError, KeyError, TypeError, AttributeError, RequestError) as exc:
                if self.path == "/api/backtest":
                    record("Backtest", str(exc) if isinstance(exc, (ValueError, RequestError)) else "Malformed request.", error=True)
                # ValueError and RequestError carry readable, secret-free reasons; the rest mean a malformed request.
                self._json(400, {"error": str(exc) if isinstance(exc, (ValueError, RequestError)) else "Malformed request."})
            except DatabaseError:
                self._json(503, {"error": "The paper journal is unavailable."})
        elif self.path == "/api/resume":
            try:
                resumed = ExitStore(self.db).resume(self.clock())
            except (DatabaseError, ValueError):
                self._json(503, {"error": "Resume could not be recorded."})
            else:
                if resumed:
                    record("Paper trader", "Trading resumed from the dashboard")
                self._json(200, {"resumed": resumed})
        elif self.path != "/api/stop":
            self._json(404, {"error": "Page not found."})
        else:
            try:
                reason, _ = ExitStore(self.db).latch(self.clock(), "dashboard_stop")
            except (DatabaseError, ValueError):
                self._json(503, {"error": "Stop request could not be recorded."})
            else:
                self._json(200, {"halted": reason is not None, "reason": reason})


class _SingleServer(ThreadingHTTPServer):
    # With address reuse on, Windows lets a second dashboard bind the same port and both would trade the journal.
    allow_reuse_address = False


def make_server(config: Config, path: Path, *, clock=now_ist, fixture=False, port=None, movers=None,
                stocks=None, backtest=None, chart=None) -> ThreadingHTTPServer:
    """`movers()` returns the stock scan, `stocks` is the stock paper trader and `backtest(symbols, start, end)` replays history."""
    if config.runtime.mode != "paper" or config.runtime.live_enabled:
        raise ValueError("The dashboard in this build requires paper mode.")
    handler = partial(DashboardHandler, config=config, path=path, clock=clock, fixture=fixture,
                      stop_token=token_urlsafe(), movers=movers, stocks=stocks, backtest=backtest, chart=chart)
    return _SingleServer(("127.0.0.1", config.reporting.dashboard_port if port is None else port), handler)
