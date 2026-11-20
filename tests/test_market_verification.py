from datetime import timedelta
import json
from json import loads
from pathlib import Path
from unittest.mock import Mock

from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.data import market_verification


CONFIG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
AT = parse_timestamp("2026-11-20T09:26:12+05:30")
END = "2026-11-20T09:20:00+05:30"
WINDOW = {"start": "2026-11-20T09:15:00+05:30", "end_exclusive": END}


def test_verification_checkpoints_quotes_and_only_retries_throttled_history(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"trading_sessions": [AT.date().isoformat()]}))
    clock = [AT]
    waits = []
    calls = []
    http = Mock()
    http.get.return_value.json.return_value = {"mode": "PAPER", "stop_token": "test"}
    monkeypatch.setattr(market_verification.requests, "Session", lambda: StubSession(http))
    monkeypatch.setattr(market_verification, "now_ist", lambda: clock[0])

    def wait_until(when):
        waits.append(when)
        clock[0] = when

    monkeypatch.setattr(market_verification, "_wait_until", wait_until)
    quotes = {"RELIANCE": {"state": "fresh", "ltp": 100}}
    cooldown_end = AT + timedelta(seconds=90)
    pending = tmp_path / CONFIG.paper_trial.output_directory / "market-data-2026-11-20.json"

    def post(url, *, json, headers, timeout):
        stage, segment = json["stage"], json["segment"]
        calls.append((stage, segment, clock[0]))
        assert json["candle_end"] == END
        response = {"mode": "PAPER", "candle_window": WINDOW, "issues": []}
        if stage == "quotes":
            response["quotes"] = quotes
        else:
            assert loads(pending.read_text())["quotes"] == quotes
            if segment == "cash" and clock[0] == AT:
                response |= {"pending_candles": ["cash"], "issues": ["Cash history throttled"], "cooldown": {
                    "stage": "cash_candles", "retry_after_seconds": 90, "resume_at_ist": cooldown_end.isoformat()}}
            else:
                response |= {"pending_candles": [], "candles": {segment: {"valid": 5, "local_overlap": 2}}}
        return Mock(status_code=200, json=Mock(return_value=response))

    http.post.side_effect = post
    result, passed = market_verification.verify_market_session(CONFIG, tmp_path, calendar, day=AT.date())
    assert passed and result["status"] == "complete" and result["issues"] == []
    assert result["quotes"] == quotes and set(result["candles"]) == {"cash", "derivatives"}
    assert len(result["cooldowns"]) == 1 and result["pending_candles"] == []
    assert waits == [cooldown_end]
    assert calls == [("quotes", None, AT), ("candles", "cash", AT),
                     ("candles", "derivatives", cooldown_end), ("candles", "cash", cooldown_end)]


def test_candles_only_keeps_original_report_and_uses_opening_window(tmp_path, monkeypatch):
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"trading_sessions": [AT.date().isoformat()]}))
    original = tmp_path / CONFIG.paper_trial.output_directory / "market-data-2026-11-20.json"
    original.parent.mkdir(parents=True)
    original.write_text('{"issues": ["Previous cooldown"]}')
    http = Mock()
    http.get.return_value.json.return_value = {"mode": "PAPER", "stop_token": "test"}
    monkeypatch.setattr(market_verification.requests, "Session", lambda: StubSession(http))
    monkeypatch.setattr(market_verification, "now_ist", lambda: AT)

    def post(url, *, json, headers, timeout):
        assert json["stage"] == "candles" and json["candle_end"] == END
        value = {"mode": "PAPER", "candle_window": WINDOW, "issues": [], "pending_candles": [],
                 "candles": {json["segment"]: {"valid": 5, "local_overlap": 0}}}
        return Mock(status_code=200, json=Mock(return_value=value))

    http.post.side_effect = post
    result, passed = market_verification.verify_market_session(CONFIG, tmp_path, calendar, candles_only=True)
    assert passed and result["quotes"] == {} and http.post.call_count == 2
    assert Path(result["report_path"]) != original
    assert json.loads(original.read_text())["issues"] == ["Previous cooldown"]


class StubSession:
    def __init__(self, http):
        self.http = http

    def __enter__(self):
        return self.http

    def __exit__(self, *exc):
        pass
