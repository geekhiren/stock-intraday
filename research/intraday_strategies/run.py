"""Test the published intraday strategies one by one on one stock.

    python -m research.intraday_strategies.run --symbol RELIANCE
    python -m research.intraday_strategies.run --symbol HDFCBANK --only s2_breakout --trades

It reads about 90 days of one-minute candles through the SmartAPI history cache (three requests the first time,
none after that on the same day) and needs the broker settings in .env only when the cache is empty.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from importlib import import_module
import json
from pathlib import Path

from orderbot.broker.angel_client import AngelClient, read_credentials
from orderbot.broker.history import HistoryCache, encode_minutes
from orderbot.core.config import load_config
from orderbot.core.ist_time import IST, now_ist
from orderbot.data.movers import stock_universe

from .common import build_days, simulate, summary

ROOT = Path(__file__).resolve().parents[2]
STRATEGIES = ("s2_breakout", "s6_gap_and_go")  # the others were removed for accuracy below 40%; see README.md
PIECES, PIECE_DAYS = 3, 30  # one SmartAPI minute-history request covers about 30 days


def load_minutes(symbol: str, config) -> list:
    today = now_ist().date()
    master = ROOT / config.data.instrument_cache_dir / f"instruments-{today.isoformat()}.json"
    if not master.is_file():
        raise SystemExit("Today's instrument list is missing. Run: python run.py --fetch-instruments")
    tokens = {name: token for token, name in stock_universe(json.loads(master.read_text(encoding="utf-8"))).items()}
    if symbol not in tokens:
        raise SystemExit(f"{symbol} is not an F&O stock symbol.")
    cache, session, rows = HistoryCache(config.data, ROOT), {}, []
    edge = datetime.combine(today, config.market.session_open, IST)

    def fetch(since, until):
        if not session:  # log in only when the cache does not already hold this piece
            client = AngelClient(config.broker)
            credentials = read_credentials(config.broker, env_file=ROOT / ".env")
            session.update(client=client, credentials=credentials, tokens=client.login(credentials))
        return session["client"].historical_minutes(session["credentials"], session["tokens"], "NSE", tokens[symbol], since, until)

    for piece in reversed(range(PIECES)):
        since, until = edge - timedelta(days=PIECE_DAYS * (piece + 1)), edge - timedelta(days=PIECE_DAYS * piece)
        rows += encode_minutes(cache.get(tokens[symbol], since, until, lambda: fetch(since, until)))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest published intraday strategies one by one on one stock")
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--only", choices=STRATEGIES, help="Run one strategy file")
    parser.add_argument("--trades", action="store_true", help="List every trade")
    args = parser.parse_args()
    config = load_config(ROOT / "config.yaml")
    days = build_days(load_minutes(args.symbol.upper(), config))
    risk = config.stocks.capital_inr * config.stocks.risk_per_trade_pct / 100
    print(f"{args.symbol.upper()} · {len(days)} sessions · {days[0].date} to {days[-1].date} · "
          f"Rs {risk:.0f} risk per trade, Rs {config.stocks.capital_inr / config.stocks.max_positions:.0f} per position, Indian intraday charges\n")
    print(f"{'Strategy':34} {'Trades':>6} {'Accuracy':>9} {'Net P&L':>11} {'Before charges':>15} {'Avg win':>9} {'Avg loss':>9} {'PF':>5}")
    for name in ([args.only] if args.only else STRATEGIES):
        module = import_module(f"{__package__}.{name}")
        trades = simulate(days, module.plans, config)
        result = summary(trades)
        accuracy = f"{result['accuracy_pct']:.1f}%" if trades else "-"
        factor = f"{result['profit_factor']:.2f}" if result["profit_factor"] is not None else "-"
        print(f"{module.NAME:34} {result['trades']:>6} {accuracy:>9} {result['net']:>11.2f} {result['gross']:>15.2f} "
              f"{result['average_win']:>9.2f} {result['average_loss']:>9.2f} {factor:>5}")
        if args.trades:
            for trade in trades:
                print(f"    {trade['day']} {trade['entry_at']}-{trade['exit_at']} {trade['side']:5} {trade['quantity']:4} "
                      f"{trade['entry']:9.2f} -> {trade['exit']:9.2f} {trade['reason']:6} net {trade['net']:9.2f}")


if __name__ == "__main__":
    main()
