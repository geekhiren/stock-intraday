from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from orderbot.broker.angel_client import AngelClient, LoginError, read_credentials
from orderbot.broker.instruments import load_master
from orderbot.core.config import ConfigError, load_config, validate_runtime_paths
from orderbot.core.event_log import open_logs
from orderbot.core.ist_time import now_ist
from orderbot.core.state_store import open_store
from orderbot.data.capture import capture_session, compare_with_broker, load_replay
from orderbot.execution.exit_replay import replay_exits
from orderbot.execution.replay import replay_execution
from orderbot.options.replay import replay_options
from orderbot.reporting.alerts import alert_worker
from orderbot.reporting.daily_report import build_report, daily_report_worker, render_markdown
from orderbot.reporting.dashboard import make_server
from orderbot.reporting.fixtures import seed_surface_fixture
from orderbot.strategy.replay import replay_strategy_a


def run_surface(config, directory, args, db, *, fixture_clock=None) -> int:
    if args.report:
        report = build_report(db, args.report)
        sys.stdout.reconfigure(encoding="utf-8")
        if args.report_format == "markdown":
            print(render_markdown(report), end="")
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["reconciliation"]["status"] == "reconciled" else 1

    fixture = fixture_clock is not None
    logs_context = nullcontext() if fixture else open_logs(config.logging, directory)
    with make_server(config, db, fixture=fixture, clock=(lambda: fixture_clock) if fixture else now_ist) as server:
        with logs_context as logs:
            alerts = nullcontext() if fixture else alert_worker(config.alerts, db, logs.application)
            reports = nullcontext() if fixture else daily_report_worker(config.reporting, db, directory, logs.application)
            with alerts, reports:
                print(f"OrderBOT PAPER dashboard at http://127.0.0.1:{server.server_port}/", flush=True)
                server.serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OrderBOT paper-mode market data")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--mode", choices=("paper", "live"), help="Override the configured mode")
    parser.add_argument("--strategy", choices=("vwap", "ohl"), help="Override the configured strategy")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-config", action="store_true", help="Validate configuration and exit")
    action.add_argument("--broker-login", action="store_true", help="Authenticate to SmartAPI and exit")
    action.add_argument("--fetch-instruments", action="store_true", help="Validate and cache today's instruments")
    action.add_argument("--capture-data", action="store_true", help="Capture SmartAPI market data for research")
    action.add_argument("--replay", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Replay a dated tick archive")
    action.add_argument("--replay-options", type=Path, metavar="FIXTURE", help="Replay paper option selection decisions")
    action.add_argument("--replay-execution", type=Path, metavar="FIXTURE", help="Replay paper execution and restart")
    action.add_argument("--replay-strategy-a", type=Path, metavar="FIXTURE", help="Replay Strategy A against fixtures")
    action.add_argument("--replay-exits", type=Path, metavar="FIXTURE", help="Drill paper exits and kill switches")
    action.add_argument("--dashboard", action="store_true", help="Serve the local paper dashboard")
    action.add_argument("--report", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Report an IST session")
    action.add_argument("--compare-history", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Compare a capture with SmartAPI historical candles")
    parser.add_argument("--option-token", action="append", default=[], help="Include a dated NIFTY option in capture")
    parser.add_argument("--surface-fixture", type=Path, metavar="FIXTURE", help="Use an isolated dashboard or report fixture")
    parser.add_argument("--report-format", choices=("json", "markdown"), help="Daily report format (default JSON)")
    args = parser.parse_args()
    if args.option_token and not args.capture_data:
        parser.error("--option-token requires --capture-data")
    if args.surface_fixture and not (args.dashboard or args.report):
        parser.error("--surface-fixture requires --dashboard or --report")
    if args.report_format and not args.report:
        parser.error("--report-format requires --report")

    try:
        config = load_config(args.config)
        if args.mode == "live":
            raise ConfigError("This build requires paper mode.")
        directory = args.config.resolve().parent
        validate_runtime_paths(config, args.config)
        if args.check_config:
            print("Configuration valid (PAPER).")
            return 0
        if args.fetch_instruments:
            day = now_ist().date()
            master = load_master(config.data, config.market.underlying, day, directory)
            print(f"Cached NIFTY instruments for {day}. Front future {master.front_future(day).symbol}.")
            return 0
        if args.replay:
            print(load_replay(config, directory, args.replay).to_json())
            return 0
        if args.replay_options:
            print(json.dumps(replay_options(args.replay_options, config), sort_keys=True, indent=2))
            return 0
        if args.replay_execution:
            print(json.dumps(replay_execution(args.replay_execution, config), sort_keys=True, indent=2))
            return 0
        if args.replay_strategy_a:
            print(json.dumps(replay_strategy_a(args.replay_strategy_a, config), sort_keys=True, indent=2))
            return 0
        if args.replay_exits:
            print(json.dumps(replay_exits(args.replay_exits, config), sort_keys=True, indent=2))
            return 0
        if args.dashboard or args.report:
            if args.surface_fixture:
                with TemporaryDirectory(prefix="orderbot-surface-") as temporary:
                    db = Path(temporary) / "fixture.sqlite3"
                    fixture_clock = seed_surface_fixture(args.surface_fixture, config, db)
                    if args.report and args.report != fixture_clock.date():
                        raise ValueError(f"Surface fixture session is {fixture_clock.date()} IST.")
                    return run_surface(config, directory, args, db, fixture_clock=fixture_clock)
            return run_surface(config, directory, args, directory / config.storage.sqlite_path)
        if args.compare_history:
            report, passed = compare_with_broker(config, directory, args.compare_history)
            print(json.dumps(report, sort_keys=True))
            return 0 if passed else 1

        context = {"correlation_id": uuid4().hex}
        with open_logs(config.logging, directory) as logs:
            try:
                with open_store(directory / config.storage.sqlite_path):
                    pass
                logs.application.info(
                    "foundation_ready",
                    extra={
                        **context,
                        "fields": {
                            "mode": config.runtime.mode,
                            "strategy": args.strategy or config.runtime.strategy,
                            "risk_configured": (
                                config.risk.capital_inr is not None
                                and config.risk.risk_per_trade_pct is not None
                            ),
                        },
                    },
                )
                if args.broker_login:
                    AngelClient(config.broker).login(read_credentials(config.broker))
                    logs.application.info("broker_login_succeeded", extra=context)
                if args.capture_data:
                    count = capture_session(config, directory, logs.application, context["correlation_id"],
                                            args.option_token)
                    print(f"Paper data capture closed {count} new futures candles.")
                    return 0
            except LoginError as exc:
                logs.application.error("broker_login_failed", extra={**context, "fields": {"reason": str(exc)}})
                raise
        print("SmartAPI login succeeded (PAPER)." if args.broker_login else "Foundation ready (PAPER).")
        return 0
    except (ValueError, LoginError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped PAPER session.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
