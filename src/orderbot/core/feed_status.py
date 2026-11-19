from datetime import datetime
from pathlib import Path

from .ist_time import to_storage
from .state_store import open_store


def record_feed_status(path: Path, state: str, at: datetime) -> None:
    with open_store(path) as connection:
        connection.execute("""
            INSERT INTO feed_status VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                state=excluded.state, observed_at_utc=excluded.observed_at_utc
            WHERE excluded.observed_at_utc >= feed_status.observed_at_utc
        """, (state, to_storage(at)))
