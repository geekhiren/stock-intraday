from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from orderbot.core.ist_time import as_ist, to_storage
from orderbot.core.money import is_finite_decimal
from orderbot.core.state_store import open_store


def nearest_rank(values: tuple[Decimal, ...], percentile: Decimal) -> Decimal:
    if not values or not is_finite_decimal(percentile) or not 0 < percentile <= 100:
        raise ValueError("Nearest rank needs samples and a percentile in (0, 100].")
    rank = int((percentile * len(values) / 100).to_integral_value(rounding=ROUND_CEILING))
    return sorted(values)[rank - 1]


class IVStore:
    """Keep the latest delivered ATM IV observation per IST session and option right."""

    def __init__(self, path: Path):
        self.path = path

    def record(self, right: str, at: datetime, iv: Decimal) -> None:
        if right not in {"CE", "PE"} or not is_finite_decimal(iv) or iv <= 0:
            raise ValueError("An ATM IV observation requires a valid option right and positive IV.")
        now = as_ist(at)
        with open_store(self.path) as connection:
            connection.execute("""
                INSERT INTO atm_iv VALUES (?, ?, ?, ?)
                ON CONFLICT(option_right, session_day) DO UPDATE
                SET observed_at_utc = excluded.observed_at_utc, iv = excluded.iv
                WHERE excluded.observed_at_utc > atm_iv.observed_at_utc
            """, (now.date().isoformat(), right, to_storage(now), str(iv)))

    def before(self, day: date, right: str, limit: int) -> tuple[Decimal, ...]:
        if right not in {"CE", "PE"} or type(limit) is not int or limit <= 0:
            raise ValueError("ATM IV lookup requires a valid right and positive session count.")
        with open_store(self.path) as connection:
            rows = connection.execute("""
                SELECT iv FROM atm_iv WHERE option_right = ? AND session_day < ?
                ORDER BY session_day DESC LIMIT ?
            """, (right, day.isoformat(), limit)).fetchall()
        values = tuple(Decimal(row[0]) for row in rows)
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("Stored ATM IV observations are invalid.")
        return values
