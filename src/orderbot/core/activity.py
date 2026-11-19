"""In-memory log of what the dashboard's background jobs are doing, newest last. Lost on restart by design;
the durable record stays in the JSON logs and the journal."""
from collections import deque

from .ist_time import now_ist

_events: deque[dict] = deque(maxlen=400)


def record(job: str, message: str, *, error: bool = False) -> None:
    _events.append({"at": now_ist().isoformat(), "job": job, "message": message, "error": error})


def recent(limit: int = 150) -> list[dict]:
    return list(_events)[-limit:][::-1]
