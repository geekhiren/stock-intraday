"""Checkpoint the opening verification and resume throttled candle checks."""
from collections import deque
from datetime import date, timedelta
import json
from pathlib import Path
from time import sleep

import requests

from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import Config
from orderbot.core.ist_time import now_ist, parse_timestamp

from .candle_builder import MINUTE, TradingSession, minute_floor


def _wait_until(when) -> None:
    while (now := now_ist()) < when:
        sleep(min(MINUTE.total_seconds(), (when - now).total_seconds()))


def verify_market_session(config: Config, directory: Path, calendar: Path, *, day: date | None = None,
                          candles_only: bool = False) -> tuple[dict, bool]:
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
    opening_end = cash.opens_at + trial.market_verification_minutes * MINUTE
    finalization = timedelta(seconds=trial.finalize_delay_seconds)
    due = opening_end + finalization
    if day and now < due:
        print(f"Waiting to verify at {due:%Y-%m-%d %H:%M:%S} IST.", flush=True)
        _wait_until(due)
        now = now_ist()
    if not due <= now < cash.closes_at:
        raise ValueError("Run during regular NSE hours after the opening candles have closed.")
    path = directory / trial.output_directory / f"market-data-{chosen.isoformat()}.json"
    if candles_only or path.exists():
        label = "candles" if candles_only else "retry"
        path = path.with_name(f"{path.stem}-{label}-{now:%H%M%S%f}.json")
    end = opening_end if day or candles_only else minute_floor(now - finalization)
    start = end - trial.market_verification_minutes * MINUTE
    report = {"mode": "PAPER", "risk_configured": False, "checked_at": now.isoformat(),
              "session_date": chosen.isoformat(), "calendar_source": document.get("source"), "report_path": str(path),
              "candle_window": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
              "quotes": {}, "candles": {}, "cooldowns": [], "pending_candles": ["cash", "derivatives"],
              "issues": [], "status": "running"}
    if candles_only:
        report["quote_check"] = "not_requested"
    issues = {}

    def save():
        report["issues"] = [item for section in issues.values() for item in section]
        with atomic_path(path) as temporary:
            temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    base = f"http://127.0.0.1:{config.reporting.dashboard_port}"
    unavailable = "The local paper dashboard was unavailable or returned an invalid verification response."
    save()
    try:
        with requests.Session() as http:
            http.trust_env = False
            state_response = http.get(base + "/api/status", timeout=config.broker.timeout_seconds)
            state_response.raise_for_status()
            state = state_response.json()
            if state["mode"] != "PAPER":
                raise ValueError("The dashboard needs to be in paper mode.")
            headers = {"Origin": base, "X-OrderBOT-Stop": state["stop_token"]}

            def check(stage, segment=None):
                try:
                    response = http.post(base + "/api/verify-market-data", headers=headers,
                                         json={"stage": stage, "segment": segment, "candle_end": end.isoformat()},
                                         timeout=5 * config.broker.timeout_seconds + config.reporting.refresh_seconds)
                    value = response.json()
                    if response.status_code != 200:
                        return {"issues": [value.get("error", "The dashboard refused market verification.")]}
                    if value["mode"] != "PAPER" or value["candle_window"] != report["candle_window"]:
                        raise ValueError("Mismatched verification response.")
                    return value
                except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
                    return {"issues": [unavailable]}

            cooldown_until = None

            def collect(section, result):
                nonlocal cooldown_until
                issues[section] = result.get("issues", [])
                report["quotes"].update(result.get("quotes", {}))
                report["candles"].update(result.get("candles", {}))
                if (section in report["pending_candles"] and result.get("candles")
                        and not result.get("pending_candles", [section])):
                    report["pending_candles"].remove(section)
                cooldown = result.get("cooldown")
                if cooldown:
                    report["cooldowns"].append(cooldown)
                    cooldown_until = parse_timestamp(cooldown["resume_at_ist"])
                save()
                return bool(cooldown)

            if not candles_only:
                collect("quotes", check("quotes"))
            retries = 0 if candles_only else trial.market_verification_history_retries
            queue = deque((segment, retries) for segment in report["pending_candles"])
            while queue:
                if cooldown_until and cooldown_until > now_ist():
                    if cooldown_until >= cash.closes_at:
                        issues["session"] = ["SmartAPI cooldown extends beyond the regular cash session."]
                        break
                    print(f"Waiting for SmartAPI cooldown until {cooldown_until:%H:%M:%S} IST.", flush=True)
                    _wait_until(cooldown_until)
                if not cash.contains(now_ist()):
                    issues["session"] = ["The regular cash session ended with candle checks pending."]
                    break
                segment, remaining = queue.popleft()
                if collect(segment, check("candles", segment)) and remaining:
                    queue.append((segment, remaining - 1))
    except (requests.RequestException, ValueError, KeyError):
        issues["dashboard"] = [unavailable]
    report["status"] = "partial" if report["pending_candles"] or any(issues.values()) else "complete"
    save()
    return report, report["status"] == "complete"
