import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from orderbot.broker.instruments import parse_master
from orderbot.broker.ws_client import Level
from orderbot.core.config import Config
from orderbot.core.ist_time import parse_timestamp

from .liquidity_gate import OptionQuote
from .strike_selector import StrikeDecision, revalidate_selection, select_strike


def _audit(event_id: str, decision: StrikeDecision, *, risk_configured: bool) -> dict:
    report = asdict(decision)
    report.update(id=event_id, mode="paper", source="fixture", risk_configured=risk_configured)
    for candidate in report["candidates"]:
        contract = candidate["contract"]
        token = contract["token"] if contract is not None else None
        candidate["token"] = token
        if candidate["reasons"] or decision.outcome == "aborted_requote":
            candidate["status"] = "rejected"
        elif token == decision.selected_token:
            candidate["status"] = decision.outcome
        else:
            candidate["status"] = "eligible_not_selected"
    return report


def _encode(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported option audit type {type(value).__name__}.")


def replay_options(path: Path, config: Config) -> list[dict]:
    """Run chronological fixture quote and decision events without opening a broker session."""
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        master_name = fixture["instrument_master"]
        if Path(master_name).name != master_name:
            raise ValueError("Instrument fixture must be a file beside the replay fixture.")
        master_rows = json.loads(path.with_name(master_name).read_text(encoding="utf-8"))
        master = parse_master(master_rows, config.market.underlying)
        sessions = tuple(date.fromisoformat(day) for day in fixture["trading_sessions"])
        events = fixture["events"]
        if not isinstance(events, list) or not events:
            raise ValueError("The option fixture needs a nonempty event array.")
    except (KeyError, TypeError, UnicodeError):
        raise ValueError("Malformed options fixture header.") from None

    quotes: dict[str, OptionQuote] = {}
    selections: dict[str, StrikeDecision] = {}
    decision_ids: set[str] = set()
    report = []
    cursor = None
    risk_configured = config.risk.capital_inr is not None and config.risk.risk_per_trade_pct is not None

    for event in events:
        try:
            at = parse_timestamp(event["at"])
            if cursor is not None and (at < cursor or at.date() != cursor.date()):
                raise ValueError("Option replay events must be ordered within one IST date.")
            cursor = at
            kind = event["event"]
            if kind == "quotes":
                for row in event["quotes"]:
                    token = row["token"]
                    delta = Decimal(row["broker_delta"]) if "broker_delta" in row else None
                    quotes[token] = OptionQuote(
                        token, parse_timestamp(row.get("exchange_at", event["at"])), at,
                        tuple(Level(Decimal(price), qty, orders) for price, qty, orders in row["bids"]),
                        tuple(Level(Decimal(price), qty, orders) for price, qty, orders in row["asks"]),
                        row["open_interest"], row["day_volume"], delta,
                    )
                continue

            event_id = event["id"]
            if not isinstance(event_id, str) or not event_id or event_id in decision_ids:
                raise ValueError("Option replay decision IDs must be unique strings.")
            decision_ids.add(event_id)
            spot = Decimal(event["spot"])
            if kind == "select":
                decision = select_strike(
                    master, sessions, quotes, at=at, spot=spot,
                    right=event["right"], lots=event["lots"], config=config.options,
                )
                selections[event_id] = decision
            elif kind == "requote":
                selected = selections[event["selection"]]
                decision = revalidate_selection(
                    selected, quotes.get(selected.selected_token), at=at, spot=spot,
                    lots=event.get("lots"), config=config.options,
                )
            else:
                raise ValueError("Unexpected option fixture event.")
            record = _audit(event_id, decision, risk_configured=risk_configured)
            if kind == "requote":
                record["revalidates"] = event["selection"]
            report.append(record)
        except (KeyError, TypeError, InvalidOperation):
            raise ValueError("Malformed option quote or decision fixture event.") from None
    return json.loads(json.dumps(report, default=_encode))
