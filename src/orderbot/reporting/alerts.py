from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from logging import Logger
import os
from pathlib import Path
import re
from threading import Event, Thread
from uuid import uuid4

import requests

from orderbot.core.config import AlertsConfig
from orderbot.core.ist_time import from_storage, now_ist, to_storage
from orderbot.core.state_store import open_store


def queue_alert(connection, key: str, kind: str, correlation_id: str, at: datetime, fields: dict):
    stamp = to_storage(at)
    connection.execute("""
        INSERT INTO alert_outbox(event_key, kind, correlation_id, at_utc, fields_json, next_attempt_utc)
        VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING
    """, (key, kind, correlation_id, stamp, json.dumps(fields), stamp))


class AlertOutbox:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _event(row) -> dict:
        return {
            "kind": row["kind"], "correlation_id": row["correlation_id"],
            "at": from_storage(row["at_utc"]).isoformat(), "fields": json.loads(row["fields_json"]),
        }

    @classmethod
    def recent_in(cls, connection, limit: int) -> list[dict]:
        rows = connection.execute("SELECT * FROM alert_outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [cls._event(row) | {
            "attempts": row["attempts"], "status": "sent" if row["sent_at_utc"] else "queued",
        } for row in rows]

    def recent(self, limit: int) -> list[dict]:
        with open_store(self.path) as connection:
            return self.recent_in(connection, limit)

    def claim(self, at: datetime, lease_seconds: float) -> tuple[int, str, int, dict] | None:
        stamp = to_storage(at)
        with open_store(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT * FROM alert_outbox
                WHERE sent_at_utc IS NULL AND next_attempt_utc <= ?
                  AND (lease_until_utc IS NULL OR lease_until_utc <= ?)
                ORDER BY next_attempt_utc, id LIMIT 1
            """, (stamp, stamp)).fetchone()
            if row is None:
                return None
            lease = uuid4().hex
            connection.execute("""
                UPDATE alert_outbox SET lease_until_utc=?, lease_id=?, attempts=attempts+1 WHERE id=?
            """, (to_storage(at + timedelta(seconds=lease_seconds)), lease, row["id"]))
            return row["id"], lease, row["attempts"] + 1, self._event(row)

    def finish(self, event_id: int, lease: str, at: datetime, retry_seconds: float | None) -> None:
        with open_store(self.path) as connection:
            if retry_seconds is None:
                connection.execute("""
                    UPDATE alert_outbox SET sent_at_utc=?, lease_until_utc=NULL, lease_id=NULL
                    WHERE id=? AND lease_id=?
                """, (to_storage(at), event_id, lease))
            else:
                connection.execute("""
                    UPDATE alert_outbox SET next_attempt_utc=?, lease_until_utc=NULL, lease_id=NULL
                    WHERE id=? AND lease_id=?
                """, (to_storage(at + timedelta(seconds=retry_seconds)), event_id, lease))


class DeliveryError(RuntimeError):
    """A sanitized alert delivery failure."""


def dispatch_once(outbox: AlertOutbox, sender: Callable[[dict], None], clock, config: AlertsConfig) -> str:
    claim = outbox.claim(clock(), config.lease_seconds)
    if claim is None:
        return "idle"
    event_id, lease, attempts, event = claim
    try:
        sender(event)
    except (DeliveryError, OSError):
        delay = config.retry_initial_seconds
        for _ in range(attempts - 1):
            if delay >= config.retry_max_seconds:
                break
            delay = min(delay * 2, config.retry_max_seconds)
        outbox.finish(event_id, lease, clock(), delay)
        return "deferred"
    outbox.finish(event_id, lease, clock(), None)
    return "sent"


class TelegramSender:
    def __init__(self, config: AlertsConfig, *, post=None):
        token = os.environ.get(config.telegram_token_env, "")
        chat = os.environ.get(config.telegram_chat_id_env, "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not chat:
            raise DeliveryError("Telegram environment variables are missing or invalid.")
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat, self._timeout = chat, config.request_timeout_seconds
        self._post = post if post is not None else requests.post

    def __call__(self, event: dict) -> None:
        fields = " · ".join(f"{key.replace('_', ' ')} {value}" for key, value in event["fields"].items())
        title = event["kind"].replace("_", " ").capitalize()
        stamp = event["at"].replace("T", " ")
        message = f"OrderBOT PAPER · {title}\n{stamp} IST\n{event['correlation_id']}\n{fields}"
        try:
            response = self._post(
                self._url, json={"chat_id": self._chat, "text": message}, timeout=self._timeout, allow_redirects=False,
            )
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise DeliveryError("Telegram rejected the alert.")
        except (requests.RequestException, ValueError, AttributeError):
            raise DeliveryError("Telegram delivery could not be confirmed.") from None


@contextmanager
def alert_worker(config: AlertsConfig, path: Path, logger: Logger):
    if not config.telegram_enabled:
        yield
        return
    try:
        sender = TelegramSender(config)
    except DeliveryError:
        logger.warning("alert_delivery_unavailable")
        yield
        return
    stopped = Event()
    outbox = AlertOutbox(path)

    def deliver():
        while not stopped.is_set():
            try:
                result = dispatch_once(outbox, sender, now_ist, config)
            except Exception:
                logger.warning("alert_worker_temporarily_unavailable")
                result = "deferred"
            if result == "deferred":
                logger.warning("alert_delivery_deferred")
            if result != "sent":
                stopped.wait(config.worker_poll_seconds)

    thread = Thread(target=deliver, name="orderbot-alerts", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=config.request_timeout_seconds)
