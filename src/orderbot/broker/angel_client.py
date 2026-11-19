from __future__ import annotations

import binascii
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from uuid import getnode

import pyotp
import requests

from orderbot.core.config import BrokerConfig
from orderbot.core.ist_time import as_ist, now_ist, require_aware


LOGIN_ROUTE = "/rest/auth/angelbroking/user/v1/loginByPassword"
HISTORY_ROUTE = "/rest/secure/angelbroking/historical/v1/getCandleData"


class LoginError(RuntimeError):
    """SmartAPI authentication failed. Messages omit sensitive request and response data."""


class RequestError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    client_code: str = field(repr=False)
    mpin: str = field(repr=False)
    totp_secret: str = field(repr=False)
    local_ip: str | None
    public_ip: str | None
    mac_address: str


@dataclass(frozen=True)
class Session:
    jwt_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    feed_token: str = field(repr=False)


def read_credentials(
    config: BrokerConfig, environment: Mapping[str, str] = os.environ, *, env_file: Path | None = None,
) -> Credentials:
    names = {
        "api_key": config.api_key_env,
        "client_code": config.client_code_env,
        "mpin": config.mpin_env,
        "totp_secret": config.totp_secret_env,
        "local_ip": config.local_ip_env,
        "public_ip": config.public_ip_env,
        "mac_address": config.mac_address_env,
    }
    values = {key: environment.get(name, "").strip() for key, name in names.items()}
    if env_file is not None and not all(values.values()):
        fallback = _read_broker_env(env_file, {name for key, name in names.items() if not values[key]})
        values = {key: values[key] or fallback.get(name, "") for key, name in names.items()}
    missing = [names[key] for key in ("api_key", "client_code", "mpin", "totp_secret") if not values[key]]
    if missing:
        raise LoginError("Missing SmartAPI environment variables " + ", ".join(missing) + ".")
    for key in ("local_ip", "public_ip"):
        if not values[key]:
            values[key] = None
            continue
        try:
            ip_address(values[key])
        except ValueError:
            raise LoginError(f"Expected an IP address in {names[key]}.") from None
    if not values["mac_address"]:
        node = getnode()
        if not node or node & (1 << 40):
            raise LoginError(f"Could not detect the adapter MAC. Set {config.mac_address_env} in .env.")
        values["mac_address"] = node.to_bytes(6, "big").hex(":")
    if not re.fullmatch(r"(?:[\da-fA-F]{2}:){5}[\da-fA-F]{2}", values["mac_address"]):
        raise LoginError(f"Expected a colon-delimited MAC address in {config.mac_address_env}.")
    return Credentials(**values)


def _read_broker_env(path: Path, names: set[str]) -> dict[str, str]:
    values = {}
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for number, line in enumerate(stream, start=1):
                name, separator, raw_value = line.strip().partition("=")
                name = name.strip()
                if name not in names:
                    continue
                value = raw_value.strip()
                if not separator or name in values:
                    raise LoginError(f"Invalid SmartAPI setting on .env line {number}.")
                if value.startswith(("'", '"')):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise LoginError(f"Unclosed quote on .env line {number}.")
                    value = value[1:-1].strip()
                values[name] = value
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        raise LoginError("Could not read the SmartAPI .env file as UTF-8.") from None
    return values


class AngelClient:
    def __init__(
        self,
        config: BrokerConfig,
        *,
        transport=requests,
        clock: Callable[[], datetime] = now_ist,
    ):
        self.config = config
        self._transport = transport
        self._clock = clock
        self._rest_lock = Lock()
        self._next_request_at = 0.0

    @staticmethod
    def _headers(credentials: Credentials) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-PrivateKey": credentials.api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-MACAddress": credentials.mac_address,
        }
        if credentials.local_ip is not None:
            headers["X-ClientLocalIP"] = credentials.local_ip
        if credentials.public_ip is not None:
            headers["X-ClientPublicIP"] = credentials.public_ip
        return headers
    def login(self, credentials: Credentials) -> Session:
        def payload():
            try:
                now = require_aware(self._clock())
                code = pyotp.TOTP(credentials.totp_secret).at(now)
            except (ValueError, binascii.Error):
                raise LoginError("Could not generate TOTP from the configured secret and clock.") from None
            return {"clientcode": credentials.client_code, "password": credentials.mpin, "totp": code}

        try:
            data = self._post("login", LOGIN_ROUTE, self._headers(credentials), payload)
        except RequestError as exc:
            raise LoginError(str(exc)) from None
        if not isinstance(data, dict):
            raise LoginError("SmartAPI login response is missing session tokens.")
        tokens = (data.get("jwtToken"), data.get("refreshToken"), data.get("feedToken"))
        if not all(isinstance(token, str) and token.strip() for token in tokens):
            raise LoginError("SmartAPI login response is missing session tokens.")
        return Session(*tokens)

    def historical_minutes(
        self, credentials: Credentials, session: Session, exchange: str, token: str,
        start: datetime, end: datetime,
    ) -> list:
        headers = self._headers(credentials) | {"Authorization": "Bearer " + session.jwt_token}
        data = self._post("history", HISTORY_ROUTE, headers, lambda: {
            "exchange": exchange,
            "symboltoken": token,
            "interval": "ONE_MINUTE",
            "fromdate": as_ist(start).strftime("%Y-%m-%d %H:%M"),
            "todate": as_ist(end).strftime("%Y-%m-%d %H:%M"),
        })
        if not isinstance(data, list):
            raise RequestError("SmartAPI history response did not contain a candle array.")
        return data

    def _post(self, action: str, route: str, headers: dict, payload: Callable[[], dict]):
        if (action, route) not in {("login", LOGIN_ROUTE), ("history", HISTORY_ROUTE)}:
            raise RequestError("This paper-mode client only supports login and candle history.")
        with self._rest_lock:
            delay = self._next_request_at - monotonic()
            if delay > 0:
                sleep(delay)
            parameters = payload()
            self._next_request_at = monotonic() + 1 / self.config.rest_max_requests_per_second
            try:
                response = self._transport.post(
                    self.config.base_url + route,
                    headers=headers,
                    json=parameters,
                    timeout=self.config.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise RequestError(f"SmartAPI {action} transport failed.", retryable=True) from None

        if not 200 <= response.status_code < 300:
            raise RequestError(
                f"SmartAPI {action} returned HTTP {response.status_code}.",
                retryable=response.status_code in {403, 429} or response.status_code >= 500,
            )
        try:
            envelope = response.json()
        except ValueError:
            raise RequestError(f"SmartAPI returned an invalid {action} response.") from None
        if not isinstance(envelope, dict):
            raise RequestError(f"SmartAPI returned an invalid {action} response.")
        if envelope.get("status") is not True:
            error_code = envelope.get("errorcode")
            if isinstance(error_code, str) and re.fullmatch(r"AB\d{4}", error_code):
                raise RequestError(
                    f"SmartAPI rejected {action} ({error_code}).", retryable=error_code == "AB1021"
                )
            raise RequestError(f"SmartAPI rejected {action}.")
        return envelope.get("data")
