from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, ValidationError
from yaml.constructor import ConstructorError


class ConfigError(ValueError):
    """A configuration error safe to print without revealing input values."""


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        keys = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in keys:
                raise ConstructorError(None, None, "Expected unique string keys", key_node.start_mark)
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConfig(Section):
    mode: Literal["paper"]
    live_enabled: Annotated[bool, Field(strict=True)]
    strategy: Literal["vwap", "ohl"]
    timezone: Literal["Asia/Kolkata"]


class MarketConfig(Section):
    underlying: Literal["NIFTY"]
    signal_series: Literal["near_month_futures"]


class RiskConfig(Section):
    capital_inr: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    risk_per_trade_pct: Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)] | None


class StorageConfig(Section):
    sqlite_path: Path


class LoggingConfig(Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    application_path: Path
    order_audit_path: Path
    rotate_bytes: Annotated[int, Field(strict=True, gt=0)]
    backup_count: Annotated[int, Field(strict=True, gt=0)]


EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]


class BrokerConfig(Section):
    provider: Literal["angel_one"]
    base_url: Literal["https://apiconnect.angelone.in"]
    timeout_seconds: Annotated[FiniteFloat, Field(strict=True, gt=0)]
    rest_max_requests_per_second: Annotated[FiniteFloat, Field(strict=True, gt=0, le=1)]
    api_key_env: EnvName
    client_code_env: EnvName
    mpin_env: EnvName
    totp_secret_env: EnvName
    local_ip_env: EnvName
    public_ip_env: EnvName
    mac_address_env: EnvName


class Config(Section):
    runtime: RuntimeConfig
    market: MarketConfig
    risk: RiskConfig
    storage: StorageConfig
    logging: LoggingConfig
    broker: BrokerConfig


def load_config(path: Path) -> Config:
    try:
        with path.open(encoding="utf-8") as stream:
            values = yaml.load(stream, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError):
        raise ConfigError("Invalid config YAML. Check syntax and use unique string keys.") from None
    try:
        config = Config.model_validate(values)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(map(str, item["loc"])) or "<root>"
            for item in exc.errors(include_input=False)
            if item["type"] != "extra_forbidden"
        )
        details = fields or "unexpected setting"
        raise ConfigError(f"Invalid configuration at {details}.") from None
    if config.runtime.live_enabled:
        raise ConfigError("runtime.live_enabled must be false in this paper-only build.")
    return config


def validate_runtime_paths(config: Config, config_path: Path) -> None:
    config_path = config_path.resolve()
    directory = config_path.parent
    app_log = (directory / config.logging.application_path).resolve()
    audit_log = (directory / config.logging.order_audit_path).resolve()
    database = (directory / config.storage.sqlite_path).resolve()
    paths = {app_log, audit_log, database, config_path}
    if len(paths) != 4:
        raise ConfigError("Configuration, database and log paths must be distinct.")
    for path in paths - {app_log}:
        prefix = app_log.name + "."
        if path.parent == app_log.parent and path.name.startswith(prefix):
            suffix = path.name.removeprefix(prefix)
            if suffix.isdecimal() and 1 <= int(suffix) <= config.logging.backup_count:
                raise ConfigError("An application log backup path conflicts with another file.")
