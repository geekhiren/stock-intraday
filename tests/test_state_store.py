import sqlite3

import pytest

from orderbot.core.ist_time import now_ist, to_storage
from orderbot.core.state_store import open_store


def test_schema_is_idempotent_and_enforces_order_journal_relationship(tmp_path):
    path = tmp_path / "runtime" / "state.sqlite3"
    timestamp = to_storage(now_ist())
    with open_store(path) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"decisions", "orders", "order_events", "trades"} <= tables
        connection.execute(
            "INSERT INTO decisions (decision_id, at_utc, strategy, outcome, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            ("signal-one", timestamp, "vwap", "skipped", "size_zero"),
        )
    with open_store(path) as connection:
        assert connection.execute("SELECT reason FROM decisions").fetchone()[0] == "size_zero"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO order_events (order_tag, at_utc, state, filled_quantity) "
                "VALUES (?, ?, ?, ?)",
                ("missing", timestamp, "SENT", 0),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO decisions (decision_id, at_utc, strategy, outcome, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                ("signal-two", "2026-11-18T00:30:00", "vwap", "skipped", "size_zero"),
            )
