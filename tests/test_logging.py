import json
from datetime import datetime, timedelta
from pathlib import Path

from orderbot.core.config import load_config
from orderbot.core.event_log import open_logs


def test_json_logs_rotate_and_append_audit(tmp_path):
    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml").logging
    config = config.model_copy(update={"rotate_bytes": 1})
    with open_logs(config, tmp_path) as logs:
        logs.application.info("startup", extra={"correlation_id": "signal-1"})
        logs.application.info("ready", extra={"correlation_id": "signal-1"})
        logs.order_audit.info("intent", extra={"correlation_id": "signal-1"})
    with open_logs(config, tmp_path) as logs:
        logs.order_audit.info("ack", extra={"correlation_id": "signal-1"})
    app_path = tmp_path / config.application_path
    assert app_path.with_name(app_path.name + ".1").exists()
    event = json.loads(app_path.read_text(encoding="utf-8"))
    assert event["event"] == "ready"
    assert event["correlation_id"] == "signal-1"
    assert datetime.fromisoformat(event["timestamp"]).utcoffset() == timedelta(hours=5, minutes=30)
    audit_path = tmp_path / config.order_audit_path
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in audit] == ["intent", "ack"]
    assert not audit_path.with_name(audit_path.name + ".1").exists()
