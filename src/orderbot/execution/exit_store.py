from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from orderbot.core.ist_time import as_ist, from_storage, parse_timestamp, to_storage
from orderbot.core.state_store import open_store
from orderbot.reporting.alerts import queue_alert

from .exit_rules import TrailState


@dataclass(frozen=True, slots=True)
class ExitProgress:
    trail: TrailState = TrailState()
    last_ltp: Decimal | None = None
    last_tick_at: datetime | None = None
    reason: str | None = None
    normal_attempts: int = 0
    last_attempt: datetime | None = None
    protected_attempted: bool = False

    def view(self) -> dict:
        return {
            "peak": str(self.trail.peak) if self.trail.peak is not None else None,
            "stop": str(self.trail.stop) if self.trail.stop is not None else None,
            "last_candle_at": _iso(self.trail.last_candle_at),
            "last_ltp": str(self.last_ltp) if self.last_ltp is not None else None,
            "last_tick_at": _iso(self.last_tick_at),
            "reason": self.reason, "normal_attempts": self.normal_attempts,
            "last_attempt": _iso(self.last_attempt), "protected_attempted": self.protected_attempted,
        }

    @classmethod
    def from_view(cls, data: dict) -> "ExitProgress":
        def price(name):
            return Decimal(data[name]) if data[name] is not None else None

        def timestamp(name):
            return parse_timestamp(data[name]) if data[name] is not None else None

        return cls(
            TrailState(price("peak"), price("stop"), timestamp("last_candle_at")),
            price("last_ltp"), timestamp("last_tick_at"), data["reason"], data["normal_attempts"],
            timestamp("last_attempt"), data["protected_attempted"],
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ExitStore:
    def __init__(self, path: Path):
        self.path = path

    def progress(self, decision_id: str) -> ExitProgress:
        with open_store(self.path) as connection:
            row = connection.execute("SELECT state_json FROM exit_progress WHERE decision_id=?", (decision_id,)).fetchone()
        return ExitProgress.from_view(json.loads(row[0])) if row else ExitProgress()

    def save(self, decision_id: str, progress: ExitProgress) -> None:
        with open_store(self.path) as connection:
            connection.execute("""
                INSERT INTO exit_progress VALUES (?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET state_json=excluded.state_json
            """, (decision_id, json.dumps(progress.view())))

    def latch(self, at: datetime, reason: str | None) -> tuple[str | None, bool]:
        day, utc = as_ist(at).date().isoformat(), to_storage(at)
        inserted = False
        with open_store(self.path) as connection:
            if reason is not None:
                connection.execute("BEGIN IMMEDIATE")
                inserted = bool(connection.execute(
                    "INSERT OR IGNORE INTO kill_switch_latches VALUES (?, ?, ?)", (day, utc, reason),
                ).rowcount)
                connection.execute(
                    "INSERT OR IGNORE INTO execution_halts VALUES (?, ?, ?)", (day, utc, reason),
                )
                if inserted:
                    connection.execute("INSERT INTO exit_audit(at_utc, event, fields_json) VALUES (?, ?, ?)",
                                       (utc, "kill_switch_fired", json.dumps({"reason": reason})))
                    queue_alert(connection, f"kill_switch:{day}", "kill_switch", day, at, {"reason": reason})
            row = connection.execute("SELECT reason FROM kill_switch_latches WHERE session_day=?", (day,)).fetchone()
        return (row[0] if row else None), inserted

    def resume(self, at: datetime) -> bool:
        """Clear today's latch if the dashboard Stop control set it. Other kill switches stay latched."""
        day = as_ist(at).date().isoformat()
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cleared = bool(connection.execute(
                "DELETE FROM kill_switch_latches WHERE session_day=? AND reason='dashboard_stop'", (day,)).rowcount)
            if cleared:
                connection.execute("DELETE FROM execution_halts WHERE session_day=? AND reason='dashboard_stop'", (day,))
                connection.execute("INSERT INTO exit_audit(at_utc, event, fields_json) VALUES (?, ?, ?)",
                                   (to_storage(at), "dashboard_resume", json.dumps({"reason": "dashboard_stop"})))
        return cleared

    def emit(self, at: datetime, event: str, fields: dict, *, once_key: str | None = None,
             alert_correlation_id: str | None = None) -> bool:
        with open_store(self.path) as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO exit_audit(at_utc, event, fields_json, once_key) VALUES (?, ?, ?, ?)",
                (to_storage(at), event, json.dumps(fields), once_key),
            ).rowcount
            if inserted and alert_correlation_id is not None:
                queue_alert(connection, f"{event}:{alert_correlation_id}", event, alert_correlation_id, at, fields)
            return bool(inserted)

    def events(self) -> list[dict]:
        with open_store(self.path) as connection:
            return [
                {"at": from_storage(row["at_utc"]).isoformat(), "event": row["event"], **json.loads(row["fields_json"])}
                for row in connection.execute("SELECT * FROM exit_audit ORDER BY event_id")
            ]
