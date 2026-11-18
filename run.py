from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

from orderbot.broker.angel_client import AngelClient, LoginError, read_credentials
from orderbot.broker.instruments import load_master
from orderbot.core.config import ConfigError, load_config, validate_runtime_paths
from orderbot.core.event_log import open_logs
from orderbot.core.ist_time import now_ist
from orderbot.core.state_store import open_store
from orderbot.data.capture import capture_session, compare_with_broker, load_replay
from orderbot.execution.replay import replay_execution
from orderbot.options.replay import replay_options


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
    action.add_argument("--compare-history", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="Compare a capture with SmartAPI historical candles")
    parser.add_argument("--option-token", action="append", default=[], help="Include a dated NIFTY option in capture")
    args = parser.parse_args()
    if args.option_token and not args.capture_data:
        parser.error("--option-token requires --capture-data")

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
        print("Paper data capture interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
