"""Run one in-session verification against the active paper dashboard."""
from datetime import date, timedelta
import json
from pathlib import Path
from time import sleep

import requests

from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import Config
from orderbot.core.ist_time import now_ist

from .candle_builder import MINUTE, TradingSession


def verify_market_session(config: Config, directory: Path, calendar: Path, *, day: date | None = None) -> tuple[dict, bool]:
    try:
        document = json.loads(calendar.read_text(encoding="utf-8-sig"))
        sessions = tuple(date.fromisoformat(value) for value in document["trading_sessions"])
        if sessions != tuple(sorted(set(sessions))):
            raise ValueError("Unordered session dates.")
    except (KeyError, TypeError, ValueError):
        raise ValueError("The NSE calendar needs ordered, unique ISO dates in trading_sessions.") from None
    now = now_ist()
    chosen = day or now.date()
    if chosen not in sessions or chosen.weekday() >= 5:
        raise ValueError("Choose a regular trading day from the supplied NSE calendar.")
    if config.runtime.mode != "paper" or config.runtime.live_enabled or any(
            budget.capital_inr is not None or budget.risk_per_trade_pct is not None for budget in (config.risk, config.stocks)):
        raise ValueError("Verification requires paper mode with both budgets unset.")
    cash = TradingSession.from_config(chosen, config.market, segment="cash")
    trial = config.paper_trial
    due = cash.opens_at + trial.market_verification_minutes * MINUTE + timedelta(seconds=trial.finalize_delay_seconds)
    if day and now < due:
        print(f"Waiting to verify at {due:%Y-%m-%d %H:%M:%S} IST.", flush=True)
        while (now := now_ist()) < due:
            sleep(min(MINUTE.total_seconds(), (due - now).total_seconds()))
    if not due <= now < cash.closes_at:
        raise ValueError("Run during regular NSE hours after the opening candles have closed.")
    path = directory / trial.output_directory / f"market-data-{chosen.isoformat()}.json"
    base = f"http://127.0.0.1:{config.reporting.dashboard_port}"
    try:
        with requests.Session() as http:
            http.trust_env = False
            state_response = http.get(base + "/api/status", timeout=config.broker.timeout_seconds)
            state_response.raise_for_status()
            state = state_response.json()
            if state["mode"] != "PAPER":
                raise ValueError("The dashboard needs to be in paper mode.")
            headers = {"Origin": base, "X-OrderBOT-Stop": state["stop_token"]}
            response = http.post(base + "/api/verify-market-data", headers=headers,
                                 timeout=4 * config.broker.timeout_seconds + config.reporting.refresh_seconds)
            result = response.json()
            if response.status_code != 200:
                result = {"issues": [result.get("error", "The dashboard refused market verification.")]}
    except (requests.RequestException, ValueError, KeyError):
        result = {"issues": ["The local paper dashboard was unavailable or returned an invalid verification response."]}
    result |= {"session_date": chosen.isoformat(), "calendar_source": document.get("source"), "report_path": str(path)}
    with atomic_path(path) as temporary:
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result, not result["issues"]
