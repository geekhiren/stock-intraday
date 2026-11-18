from __future__ import annotations

import binascii
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from threading import Lock
from time import monotonic, sleep

import pyotp
import requests

from orderbot.core.config import BrokerConfig
from orderbot.core.ist_time import now_ist, require_aware


LOGIN_ROUTE = "/rest/auth/angelbroking/user/v1/loginByPassword"


class LoginError(RuntimeError):
    """SmartAPI authentication failed. Messages omit sensitive request and response data."""


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    client_code: str = field(repr=False)
    mpin: str = field(repr=False)
    totp_secret: str = field(repr=False)
    local_ip: str
    public_ip: str
    mac_address: str


@dataclass(frozen=True)
class Session:
    jwt_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    feed_token: str = field(repr=False)


def read_credentials(
    config: BrokerConfig, environment: Mapping[str, str] = os.environ
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
    missing = [names[key] for key, value in values.items() if not value]
    if missing:
        raise LoginError("Missing SmartAPI environment variables " + ", ".join(missing) + ".")
    for key in ("local_ip", "public_ip"):
        try:
            ip_address(values[key])
        except ValueError:
            raise LoginError(f"Expected an IP address in {names[key]}.") from None
    if not re.fullmatch(r"(?:[\da-fA-F]{2}:){5}[\da-fA-F]{2}", values["mac_address"]):
        raise LoginError(f"Expected a colon-delimited MAC address in {config.mac_address_env}.")
    return Credentials(**values)


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

    def login(self, credentials: Credentials) -> Session:
        headers = {
            "Accept": "application/json",
            "X-PrivateKey": credentials.api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": credentials.local_ip,
            "X-ClientPublicIP": credentials.public_ip,
            "X-MACAddress": credentials.mac_address,
        }
        with self._rest_lock:
            delay = self._next_request_at - monotonic()
            if delay > 0:
                sleep(delay)
            try:
                now = require_aware(self._clock())
                code = pyotp.TOTP(credentials.totp_secret).at(now)
            except (ValueError, binascii.Error):
                raise LoginError("Could not generate TOTP from the configured secret and clock.") from None
            self._next_request_at = monotonic() + 1 / self.config.rest_max_requests_per_second
            try:
                response = self._transport.post(
                    self.config.base_url + LOGIN_ROUTE,
                    headers=headers,
                    json={"clientcode": credentials.client_code, "password": credentials.mpin, "totp": code},
                    timeout=self.config.timeout_seconds,
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise LoginError("SmartAPI login transport failed.") from None

        if not 200 <= response.status_code < 300:
            raise LoginError(f"SmartAPI login returned HTTP {response.status_code}.")
        try:
            envelope = response.json()
        except ValueError:
            raise LoginError("SmartAPI returned an invalid login response.") from None
        if not isinstance(envelope, dict):
            raise LoginError("SmartAPI returned an invalid login response.")
        if envelope.get("status") is not True:
            error_code = envelope.get("errorcode")
            if isinstance(error_code, str) and re.fullmatch(r"AB\d{4}", error_code):
                raise LoginError(f"SmartAPI rejected login ({error_code}).")
            raise LoginError("SmartAPI rejected login.")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise LoginError("SmartAPI login response is missing session tokens.")
        tokens = (data.get("jwtToken"), data.get("refreshToken"), data.get("feedToken"))
        if not all(isinstance(token, str) and token.strip() for token in tokens):
            raise LoginError("SmartAPI login response is missing session tokens.")
        return Session(*tokens)
