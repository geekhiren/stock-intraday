from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from orderbot.broker.angel_client import AngelClient, LoginError, read_credentials
from orderbot.core.config import ConfigError, load_config, validate_runtime_paths
from orderbot.core.event_log import open_logs
from orderbot.core.state_store import open_store


def main() -> int:
    parser = argparse.ArgumentParser(description="OrderBOT paper-mode foundation")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--mode", choices=("paper", "live"), help="Override the configured mode")
    parser.add_argument("--strategy", choices=("vwap", "ohl"), help="Override the configured strategy")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-config", action="store_true", help="Validate configuration and exit")
    action.add_argument("--broker-login", action="store_true", help="Authenticate to SmartAPI and exit")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        if args.mode == "live":
            raise ConfigError("This build requires paper mode.")
        directory = args.config.resolve().parent
        validate_runtime_paths(config, args.config)
        if args.check_config:
            print("Configuration valid (PAPER).")
            return 0

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
            except LoginError as exc:
                logs.application.error("broker_login_failed", extra={**context, "fields": {"reason": str(exc)}})
                raise
        print("SmartAPI login succeeded (PAPER)." if args.broker_login else "Foundation ready (PAPER).")
        return 0
    except (ConfigError, LoginError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
