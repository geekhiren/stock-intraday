import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from orderbot.broker import angel_client
from orderbot.broker.angel_client import AngelClient, LoginError, read_credentials
from orderbot.core.config import load_config


ROOT = Path(__file__).resolve().parents[1]
TOTP = json.loads((ROOT / "tests" / "fixtures" / "totp.json").read_text(encoding="utf-8"))


@pytest.fixture
def broker():
    return load_config(ROOT / "config.yaml").broker


@pytest.fixture
def credential_environment(broker):
    return {
        broker.api_key_env: "test-key",
        broker.client_code_env: "test-client",
        broker.mpin_env: "0123",
        broker.totp_secret_env: TOTP["secret"],
        broker.local_ip_env: "192.168.10.5",
        broker.public_ip_env: "203.0.113.10",
        broker.mac_address_env: "02:00:00:00:00:01",
    }


def test_login_uses_documented_endpoint_and_totp(broker, credential_environment):
    transport = Mock()
    transport.post.return_value.status_code = 200
    transport.post.return_value.json.return_value = {
        "status": True,
        "data": {"jwtToken": "test-jwt", "refreshToken": "test-refresh", "feedToken": "test-feed"},
    }
    client = AngelClient(broker, transport=transport, clock=lambda: datetime.fromisoformat(TOTP["instant"]))
    session = client.login(read_credentials(broker, credential_environment))
    request = transport.post.call_args
    assert request.args == (broker.base_url + "/rest/auth/angelbroking/user/v1/loginByPassword",)
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["headers"]["X-PrivateKey"] == "test-key"
    assert request.kwargs["headers"]["X-ClientPublicIP"] == "203.0.113.10"
    assert request.kwargs["json"] == {
        "clientcode": "test-client", "password": "0123", "totp": TOTP["code"]
    }
    assert session.feed_token == "test-feed"
    assert "test-jwt" not in repr(session)


def test_rejected_login_hides_echoed_credentials_and_sends_once(broker, credential_environment):
    transport = Mock()
    transport.post.return_value.status_code = 200
    transport.post.return_value.json.return_value = {
        "status": "false", "errorcode": "AB1007", "message": "test-key",
    }
    with pytest.raises(LoginError, match="AB1007") as error:
        AngelClient(broker, transport=transport).login(read_credentials(broker, credential_environment))
    assert "test-key" not in str(error.value)
    assert transport.post.call_count == 1


def test_missing_credentials_fail_before_connecting(broker):
    with pytest.raises(LoginError, match=broker.api_key_env):
        read_credentials(broker, {})


def test_dotenv_supplies_missing_credentials_without_overriding_environment(broker, credential_environment, tmp_path):
    path = tmp_path / ".env"
    entries = [f'{name}="{value}"' for name, value in credential_environment.items()]
    path.write_text("# SmartAPI\n" + "\n".join(entries), encoding="utf-8-sig")

    credentials = read_credentials(broker, {broker.api_key_env: "env-key"}, env_file=path)

    assert credentials.api_key == "env-key"
    assert credentials.mpin == "0123"
    assert credentials.totp_secret == TOTP["secret"]
    assert credentials.mac_address == "02:00:00:00:00:01"


def test_malformed_dotenv_stays_redacted(broker, tmp_path):
    path = tmp_path / ".env"
    path.write_text(f'{broker.api_key_env}="fixture-secret\n', encoding="utf-8")

    with pytest.raises(LoginError, match=r"\.env line 1") as error:
        read_credentials(broker, {}, env_file=path)
    assert "fixture-secret" not in str(error.value)


def test_four_login_fields_use_detected_mac_and_omit_unset_ip_headers(broker, credential_environment, monkeypatch):
    monkeypatch.setattr(angel_client, "getnode", lambda: 0x020000000001)
    names = (broker.api_key_env, broker.client_code_env, broker.mpin_env, broker.totp_secret_env)
    credentials = read_credentials(broker, {name: credential_environment[name] for name in names})
    headers = AngelClient(broker)._headers(credentials)

    assert headers["X-MACAddress"] == "02:00:00:00:00:01"
    assert "X-ClientLocalIP" not in headers and "X-ClientPublicIP" not in headers


def test_missing_mac_rejects_random_fallback(broker, credential_environment, monkeypatch):
    monkeypatch.setattr(angel_client, "getnode", lambda: 0x010000000001)
    del credential_environment[broker.mac_address_env]
    with pytest.raises(LoginError, match=broker.mac_address_env):
        read_credentials(broker, credential_environment)


def test_transport_timeout_is_redacted_and_not_retried(broker, credential_environment):
    transport = Mock()
    transport.post.side_effect = requests.Timeout("test-key")
    credentials = read_credentials(broker, credential_environment)
    with pytest.raises(LoginError, match="transport failed") as error:
        AngelClient(broker, transport=transport).login(credentials)
    assert "test-key" not in str(error.value)
    assert transport.post.call_count == 1


def test_redirect_is_rejected_without_reading_response(broker, credential_environment):
    transport = Mock()
    transport.post.return_value.status_code = 307
    with pytest.raises(LoginError, match="HTTP 307"):
        AngelClient(broker, transport=transport).login(read_credentials(broker, credential_environment))
    transport.post.return_value.json.assert_not_called()


def test_consecutive_login_calls_obey_configured_spacing(broker, credential_environment, monkeypatch):
    rate_fixture = json.loads((ROOT / "tests" / "fixtures" / "rest_limit.json").read_text(encoding="utf-8"))
    broker = broker.model_copy(update={"rest_max_requests_per_second": rate_fixture["rate"]})
    clock = Mock(side_effect=rate_fixture["monotonic_readings"])
    sleep = Mock()
    monkeypatch.setattr(angel_client, "monotonic", clock)
    monkeypatch.setattr(angel_client, "sleep", sleep)
    transport = Mock()
    transport.post.return_value.status_code = 200
    transport.post.return_value.json.return_value = {
        "status": True,
        "data": {"jwtToken": "jwt", "refreshToken": "refresh", "feedToken": "feed"},
    }
    client = AngelClient(broker, transport=transport)
    credentials = read_credentials(broker, credential_environment)
    client.login(credentials)
    client.login(credentials)
    sleep.assert_called_once_with(rate_fixture["expected_sleep"])
