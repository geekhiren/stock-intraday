from pathlib import Path
from threading import Event
from unittest.mock import Mock

from orderbot.broker.angel_client import RateLimitError, Session
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data import movers


CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
AT = parse_timestamp("2026-11-30T11:00:00+05:30")


def scanner_for(tmp_path, client, trader):
    scanner = movers.MoversScanner(CONFIG, tmp_path, client=client, clock=lambda: AT, trader=trader)
    scanner._day = scanner._baselines_for = AT.date()
    scanner._universe = {str(token): f"STOCK{token}" for token in range(1, 52)}
    scanner._credentials, scanner._session = object(), Session("jwt", "refresh", "feed")
    return scanner


def test_disconnect_mid_batch_discards_results_without_killing_worker(monkeypatch, tmp_path):
    client, trader, stopped = Mock(), Mock(), Event()
    trader.held.return_value = []
    scanner = scanner_for(tmp_path, client, trader)

    def disconnect(*args, **kwargs):
        scanner.disconnect()
        stopped.set()
        return []

    client.quotes.side_effect = disconnect
    scanner.run(stopped)
    client.quotes.assert_called_once()
    trader.on_scan.assert_not_called()
    assert scanner.connection(AT)["state"] == "paused"
    monkeypatch.setattr(movers, "read_credentials", lambda *args, **kwargs: object())
    client.login.return_value = Session("jwt", "refresh", "feed")
    client.quotes.side_effect = None
    client.quotes.return_value = []
    monkeypatch.setattr(scanner, "_option_chain", lambda *args: None)
    scanner.connect()
    scanner.scan_once()
    trader.on_scan.assert_called_once()


def test_history_cooldown_stops_baseline_queue_without_saving_partial_results(tmp_path):
    client, trader = Mock(), Mock()
    scanner = scanner_for(tmp_path, client, trader)
    client.historical_minutes.side_effect = RateLimitError("SmartAPI cooldown")
    scanner._prepare_baselines(AT.date(), scanner._universe)
    client.historical_minutes.assert_called_once()
    trader.set_baselines.assert_not_called()
    assert not list(tmp_path.rglob("stock-baselines-*.json"))
