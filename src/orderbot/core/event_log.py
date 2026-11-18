from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from .config import LoggingConfig
from .ist_time import IST


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=IST)
        return json.dumps(
            {
                "timestamp": timestamp.isoformat(timespec="milliseconds"),
                "level": record.levelname,
                "event": record.getMessage(),
                "correlation_id": getattr(record, "correlation_id", None) or uuid4().hex,
                "fields": getattr(record, "fields", {}),
            },
            ensure_ascii=False,
            allow_nan=False,
        )


@dataclass(frozen=True)
class Logs:
    application: logging.Logger
    order_audit: logging.Logger


@contextmanager
def open_logs(config: LoggingConfig, directory: Path) -> Iterator[Logs]:
    app_path = directory / config.application_path
    audit_path = directory / config.order_audit_path
    app_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    app = logging.Logger("orderbot.application", config.level)
    audit = logging.Logger("orderbot.order_audit", logging.INFO)
    app_handler = RotatingFileHandler(
        app_path, maxBytes=config.rotate_bytes, backupCount=config.backup_count, encoding="utf-8"
    )
    try:
        audit_handler = logging.FileHandler(audit_path, mode="a", encoding="utf-8")
        try:
            app_handler.setFormatter(JsonFormatter())
            audit_handler.setFormatter(JsonFormatter())
            app.addHandler(app_handler)
            audit.addHandler(audit_handler)
            yield Logs(application=app, order_audit=audit)
        finally:
            audit.removeHandler(audit_handler)
            audit_handler.close()
    finally:
        app.removeHandler(app_handler)
        app_handler.close()
