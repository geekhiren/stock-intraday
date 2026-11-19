import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from orderbot.broker.angel_client import AngelClient, Credentials, Session
from orderbot.broker.history import HistoryCache
from orderbot.core.config import load_config
from orderbot.data.candle_builder import TradingSession


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")


def test_history_retries_server_error_caches_and_excludes_open_candle(tmp_path):
    session = TradingSession.from_config(date(2026, 11, 18), CONFIG.market)
    start = session.opens_at
    until = start + timedelta(minutes=2)
    response = Mock()
    response.status_code = 200
    response.json.return_value = json.loads((ROOT / "tests" / "fixtures" / "history.json").read_text())
    unavailable = Mock(status_code=503)
    transport = Mock()
    transport.post.side_effect = [unavailable, response]
    credentials = Credentials("test-key", "test-client", "0123", "fake", "192.0.2.1", "203.0.113.10", "02:00:00:00:00:01")
    sleeper = Mock()
    history = HistoryCache(CONFIG.data, tmp_path, sleep=sleeper, jitter=lambda upper: upper / 2)
    broker = AngelClient(CONFIG.broker, transport=transport)
    fetch = lambda: broker.historical_minutes(credentials, Session("jwt", "refresh", "feed"), "NFO", "61471", start, until)
    result = history.get("61471", start, until, fetch)
    assert len(result) == 2
    assert result[-1].at == start + timedelta(minutes=1)
    assert result[-1].close == Decimal("22150.20")
    assert result[-1].volume == 20
    assert history.get("61471", start, until, fetch) == result
    assert transport.post.call_count == 2
    sleeper.assert_called_once_with(CONFIG.data.history_backoff_seconds / 2)
    request = transport.post.call_args
    assert request.args[0].endswith("/historical/v1/getCandleData")
    assert request.kwargs["headers"]["Authorization"] == "Bearer jwt"
    assert request.kwargs["json"]["fromdate"] == "2026-11-18 09:15"
    assert request.kwargs["json"]["todate"] == "2026-11-18 09:17"
