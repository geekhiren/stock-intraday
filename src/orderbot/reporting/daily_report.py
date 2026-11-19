from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from sqlite3 import Error as DatabaseError
from threading import Event, Thread

from orderbot.core.atomic_file import atomic_path
from orderbot.core.ist_time import IST, as_ist, from_storage, now_ist, to_storage
from orderbot.core.money import PAISA, round_inr
from orderbot.core.state_store import open_store


def ist_bounds(day: date, until: datetime | None = None) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tzinfo=IST)
    end = start + timedelta(days=1)
    if until is not None:
        end = min(end, as_ist(until) + datetime.resolution)
    return to_storage(start), to_storage(end)


def format_paise(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    rupees, paise = divmod(abs(value), 100)
    return f"{'−' if value < 0 else ''}₹{rupees:,}.{paise:02d}"


def exact_paise(value) -> int:
    amount = Decimal(value)
    if not amount.is_finite() or amount % PAISA:
        raise ValueError("Amount must be a whole number of paise.")
    return int(amount / PAISA)


def _filled_orders(connection, start, end):
    grouped = defaultdict(list)
    for row in connection.execute("""
        SELECT o.decision_id, o.side, o.detail_json AS intent_json, e.state, e.filled_quantity, e.detail_json
        FROM trades t JOIN orders o ON o.decision_id=t.decision_id
        JOIN order_events e ON e.event_id=(SELECT MAX(event_id) FROM order_events WHERE order_tag=o.order_tag)
        WHERE t.exit_at_utc >= ? AND t.exit_at_utc < ?
        ORDER BY e.event_id
    """, (start, end)):
        grouped[row["decision_id"]].append(row)
    return grouped


def _booked_charges(detail):
    parts = detail["charges"]
    components = ("brokerage", "stt", "exchange_including_ipft", "sebi", "stamp_duty", "gst")
    charges = [exact_paise(parts[name]) for name in components]
    total = exact_paise(parts["total"])
    if any(value < 0 for value in charges) or sum(charges) != total:
        raise ValueError("Booked charges disagree with their components.")
    return total


def _check_trade(row, orders):
    checks = []
    charges = recomputed = None
    detail = {}
    try:
        detail = json.loads(row["detail_json"])
        charges = _booked_charges(detail)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        if not isinstance(detail, dict):
            detail = {}
        checks.append("charges_unverified")
    fills = {"BUY": 0, "SELL": 0}
    values = {"BUY": Decimal(0), "SELL": Decimal(0)}
    actual = modeled = Decimal(0)
    missing = measured = 0
    last_exit = None
    try:
        for order in orders:
            if order["state"] not in {"FILLED", "CANCELLED", "REJECTED"}:
                raise ValueError("Closed trade has a working order.")
            quantity, side = order["filled_quantity"], order["side"]
            if not quantity:
                continue
            fill = json.loads(order["detail_json"])
            value = Decimal(fill["filled_value"])
            if not value.is_finite() or value <= 0:
                raise ValueError("Invalid fill value.")
            fills[side] += quantity
            values[side] += value
            if side == "SELL" and (last_exit is None or fill["last_fill_at"] >= last_exit["last_fill_at"]):
                last_exit = fill | {"intent_reason": json.loads(order["intent_json"])["reason"]}
            try:
                touch = Decimal(fill["touch_value"])
                model = Decimal(fill["modeled_slippage"])
                if not touch.is_finite() or touch <= 0 or not model.is_finite() or model < 0:
                    raise ValueError("Invalid slippage evidence.")
            except (KeyError, ValueError, TypeError, ArithmeticError):
                missing += 1
            else:
                actual += value - touch if side == "BUY" else touch - value
                modeled += model
                measured += 1
        if fills["BUY"] != row["quantity"] or fills["SELL"] != row["quantity"]:
            raise ValueError("Buy and sell fill quantities disagree with the trade.")
    except (KeyError, ValueError, TypeError, ArithmeticError):
        checks.append("fills_unverified")
        missing, measured = sum(order["filled_quantity"] > 0 for order in orders), 0
        actual = modeled = Decimal(0)
    else:
        if charges is not None:
            recomputed = exact_paise(round_inr(values["SELL"] - values["BUY"] - charges * PAISA))
    if recomputed is not None:
        try:
            agrees = recomputed == row["net_pnl_paise"] == exact_paise(detail["net_pnl_inr"])
        except (KeyError, ValueError, TypeError, ArithmeticError):
            agrees = False
        if not agrees:
            checks.append("net_pnl_mismatch")
    trade = {
        "id": row["trade_id"], "symbol": detail.get("symbol"), "quantity": row["quantity"],
        "entry_at": from_storage(row["entry_at_utc"]).isoformat(),
        "exit_at": from_storage(row["exit_at_utc"]).isoformat(),
        "exit_reason": last_exit["intent_reason"] if last_exit else None,
        "net_paise": row["net_pnl_paise"], "charges_paise": charges,
        "recomputed_net_paise": recomputed, "checks": checks,
    }
    return trade, actual, modeled, missing, measured


def report_in(connection, day: date, until: datetime | None = None) -> dict:
    start, end = ist_bounds(day, until)
    grouped = _filled_orders(connection, start, end)
    checked = [_check_trade(row, grouped[row["decision_id"]]) for row in connection.execute(
        "SELECT * FROM trades WHERE exit_at_utc >= ? AND exit_at_utc < ? ORDER BY exit_at_utc, trade_id", (start, end),
    )]
    trades = [item[0] for item in checked]
    skips = [{"id": row["decision_id"], "at": from_storage(row["at_utc"]).isoformat(),
              "strategy": row["strategy"], "reason": row["reason"]} for row in connection.execute("""
        SELECT * FROM decisions WHERE at_utc >= ? AND at_utc < ? AND outcome='skipped' ORDER BY at_utc, decision_id
    """, (start, end))]
    net = sum(trade["net_paise"] for trade in trades)
    charges = sum(trade["charges_paise"] for trade in trades) if all(
        trade["charges_paise"] is not None for trade in trades
    ) else None
    recomputed = sum(trade["recomputed_net_paise"] for trade in trades) if all(
        trade["recomputed_net_paise"] is not None for trade in trades
    ) else None
    status = "reconciled"
    if any("net_pnl_mismatch" in trade["checks"] for trade in trades):
        status = "mismatch"
    elif recomputed is None:
        status = "unverified"
    covered = not trades or any(item[4] for item in checked)
    actual = exact_paise(round_inr(sum((item[1] for item in checked), Decimal(0)))) if covered else None
    model = exact_paise(round_inr(sum((item[2] for item in checked), Decimal(0)))) if covered else None
    return {
        "date": day.isoformat(), "mode": "PAPER", "timezone": "Asia/Kolkata",
        "net_paise": net, "charges_paise": charges, "trades": trades, "skips": skips,
        "slippage": {"actual_paise": actual, "model_paise": model,
                     "delta_paise": actual - model if covered else None, "unavailable_orders": sum(x[3] for x in checked)},
        "reconciliation": {"status": status, "recomputed_net_paise": recomputed,
                           "delta_paise": net - recomputed if recomputed is not None else None},
    }


def build_report(path: Path, day: date) -> dict:
    with open_store(path) as connection:
        connection.execute("BEGIN")
        return report_in(connection, day)


def _ist_clock(timestamp: str) -> str:
    return timestamp.split("T", 1)[1].split("+", 1)[0]


def _cell(value) -> str:
    return str(value if value is not None else "Unavailable").replace("|", r"\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    reconciliation = report["reconciliation"]
    slippage = report["slippage"]
    lines = [f"# OrderBOT PAPER daily report · {report['date']} IST", "",
             f"- Realized net P&L {format_paise(report['net_paise'])}",
             f"- Booked charges {format_paise(report['charges_paise'])}",
             f"- Reconciliation {reconciliation['status']} · independently recomputed "
             f"{format_paise(reconciliation['recomputed_net_paise'])} · difference {format_paise(reconciliation['delta_paise'])}",
             f"- Slippage for measured orders {format_paise(slippage['actual_paise'])} actual · "
             f"{format_paise(slippage['model_paise'])} modeled · difference {format_paise(slippage['delta_paise'])}",
             f"- Filled orders without slippage evidence {slippage['unavailable_orders']}",
             "", "## Closed trades", ""]
    if report["trades"]:
        lines.extend(["| Entry IST | Exit IST | Contract | Qty | Charges | Net | Exit reason | Checks |",
                      "| --- | --- | --- | ---: | ---: | ---: | --- | --- |"])
        for trade in report["trades"]:
            values = (_ist_clock(trade["entry_at"]), _ist_clock(trade["exit_at"]), trade["symbol"], trade["quantity"],
                      format_paise(trade["charges_paise"]), format_paise(trade["net_paise"]), trade["exit_reason"],
                      ", ".join(trade["checks"]) or "Reconciled")
            lines.append("| " + " | ".join(map(_cell, values)) + " |")
    else:
        lines.append("No closed trades.")
    lines.extend(["", "## Skipped signals", ""])
    if report["skips"]:
        lines.extend(["| Time IST | Strategy | Reason |", "| --- | --- | --- |"])
        for skip in report["skips"]:
            values = (_ist_clock(skip["at"]), skip["strategy"], skip["reason"])
            lines.append("| " + " | ".join(map(_cell, values)) + " |")
    else:
        lines.append("No skipped signals.")
    return "\n".join(lines) + "\n"


def write_due_report(config, db: Path, directory: Path, at: datetime) -> Path | None:
    now = as_ist(at)
    scheduled = datetime.combine(now.date(), config.daily_report_time, tzinfo=IST)
    if now < scheduled:
        return None
    report = build_report(db, now.date())
    destination = directory / config.daily_report_directory / f"{now.date()}-daily-report.md"
    contents = render_markdown(report).encode("utf-8")
    if not destination.exists() or destination.read_bytes() != contents:
        with atomic_path(destination) as temporary:
            temporary.write_bytes(contents)
    return destination


@contextmanager
def daily_report_worker(config, db: Path, directory: Path, logger):
    shutdown = Event()

    def write_reports():
        completed = None
        while not shutdown.is_set():
            now = now_ist()
            if completed != now.date():
                try:
                    if write_due_report(config, db, directory, now) is not None:
                        completed = now.date()
                except (DatabaseError, OSError, ValueError):
                    logger.warning("daily_report_temporarily_unavailable")
            shutdown.wait(config.refresh_seconds)

    thread = Thread(target=write_reports, name="orderbot-daily-report", daemon=True)
    thread.start()
    try:
        yield
    finally:
        shutdown.set()
        thread.join(timeout=config.refresh_seconds)
