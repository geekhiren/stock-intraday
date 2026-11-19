from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, ValidationError, model_validator
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
    session_open: time
    session_close: time

    @model_validator(mode="after")
    def check_session(self):
        if any(value.tzinfo or value.second or value.microsecond for value in (self.session_open, self.session_close)):
            raise ValueError("Session times must be IST minute boundaries.")
        if self.session_open >= self.session_close:
            raise ValueError("Session open must precede session close.")
        return self


PositiveSeconds = Annotated[FiniteFloat, Field(strict=True, gt=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]


class DataConfig(Section):
    instrument_master_url: Literal[
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    ]
    instrument_cache_dir: Path
    instrument_download_timeout_seconds: PositiveSeconds
    history_cache_dir: Path
    tick_archive_dir: Path
    archive_batch_rows: PositiveCount
    archive_flush_seconds: PositiveSeconds
    close_grace_seconds: Annotated[FiniteFloat, Field(strict=True, ge=0, lt=60)]
    rsi_period: PositiveCount
    atr_period: PositiveCount
    history_attempts: PositiveCount
    history_backoff_seconds: PositiveSeconds
    history_backoff_max_seconds: PositiveSeconds
    previous_session_lookback_days: Annotated[int, Field(strict=True, gt=0, le=30)]
    capture_lead_minutes: PositiveCount
    websocket_url: Literal["wss://smartapisocket.angelone.in/smart-stream"]
    websocket_subscription_limit: Annotated[int, Field(strict=True, gt=0, le=1000)]
    websocket_connect_timeout_seconds: PositiveSeconds
    websocket_poll_seconds: PositiveSeconds
    websocket_heartbeat_seconds: Annotated[FiniteFloat, Field(strict=True, gt=0, le=30)]
    websocket_stale_seconds: PositiveSeconds
    websocket_reconnect_seconds: PositiveSeconds
    websocket_backoff_seconds: PositiveSeconds
    websocket_backoff_max_seconds: PositiveSeconds

    @model_validator(mode="after")
    def check_timing(self):
        if not self.websocket_poll_seconds < self.websocket_stale_seconds < self.websocket_reconnect_seconds:
            raise ValueError("WebSocket poll, staleness and reconnect intervals must increase.")
        if self.websocket_backoff_seconds > self.websocket_backoff_max_seconds:
            raise ValueError("WebSocket initial backoff must fit within its cap.")
        if self.history_backoff_seconds > self.history_backoff_max_seconds:
            raise ValueError("History initial backoff must fit within its cap.")
        return self


PositivePercentage = Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]
TurnoverRate = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonnegativeCount = Annotated[int, Field(strict=True, ge=0)]


class OptionsConfig(Section):
    monthly_futures_count: PositiveCount
    min_later_trading_sessions: PositiveCount
    strike_interval: PositiveDecimal
    candidate_wings: PositiveCount
    max_itm_steps: NonnegativeCount
    max_otm_steps: Annotated[int, Field(strict=True, ge=0, le=1)]
    min_abs_delta: TurnoverRate
    target_abs_delta: TurnoverRate
    max_abs_delta: TurnoverRate
    min_premium: PositiveDecimal
    max_premium: PositiveDecimal
    max_spread_fraction: TurnoverRate
    max_spread_ticks: PositiveCount
    depth_levels: Annotated[int, Field(strict=True, ge=1, le=5)]
    depth_multiple: PositiveCount
    min_open_interest: NonnegativeCount
    min_day_volume: NonnegativeCount
    quote_max_age_seconds: PositiveSeconds
    max_requote_mid_fraction: TurnoverRate
    quantity_freeze_units: PositiveCount
    expiry_close: time
    annual_rate: TurnoverRate
    dividend_yield: TurnoverRate
    maximum_iv: PositiveDecimal
    iv_price_tolerance: PositiveDecimal
    iv_max_iterations: PositiveCount
    broker_delta_tolerance: TurnoverRate

    @model_validator(mode="after")
    def check_option_ranges(self):
        if not self.min_abs_delta <= self.target_abs_delta <= self.max_abs_delta:
            raise ValueError("Target absolute delta must be inside the allowed band.")
        if self.min_premium > self.max_premium:
            raise ValueError("Minimum option premium must be at most the maximum.")
        if max(self.max_itm_steps, self.max_otm_steps) > self.candidate_wings:
            raise ValueError("Moneyness limits must fit within the candidate window.")
        if self.expiry_close.tzinfo or self.expiry_close.second or self.expiry_close.microsecond:
            raise ValueError("Option expiry close must be an IST minute boundary.")
        return self


class RiskConfig(Section):
    capital_inr: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    risk_per_trade_pct: PositivePercentage | None
    max_open_positions: Annotated[int, Field(strict=True, ge=1, le=1)]
    max_premium_deployed_pct: PositivePercentage
    max_entries_per_day: PositiveCount
    daily_loss_limit_pct: PositivePercentage
    max_consecutive_losses: PositiveCount
    max_round_trip_friction_pct: PositivePercentage


class CostConfig(Section):
    brokerage_per_order_inr: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    stt_sell_rate: TurnoverRate
    stt_exercise_rate: TurnoverRate
    exchange_including_ipft_rate: TurnoverRate
    sebi_rate: TurnoverRate
    stamp_buy_rate: TurnoverRate
    gst_rate: TurnoverRate
    slippage_ticks: Annotated[int, Field(strict=True, ge=0)]


class ExecutionConfig(Section):
    max_entry_premium: PositiveDecimal
    partial_timeout_seconds: PositiveSeconds
    timeout_lookup_delay_seconds: PositiveSeconds
    reconcile_interval_seconds: PositiveSeconds
    paper_rejection_pct: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]
    paper_rejection_seed: NonnegativeCount


RSIPoint = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]


class StrategyAConfig(Section):
    consolidation_band: TurnoverRate
    consolidation_lookback: PositiveCount
    consolidation_max_near: NonnegativeCount
    volume_lookback: PositiveCount
    volume_multiple: PositiveDecimal
    rsi_long_band: tuple[RSIPoint, RSIPoint]
    rsi_short_band: tuple[RSIPoint, RSIPoint]
    require_pivot: Annotated[bool, Field(strict=True)]
    pivot_tolerance: TurnoverRate
    entry_open: time
    entry_close: time
    expiry_entry_close: time
    iv_lookback_sessions: PositiveCount
    iv_percentile: PositivePercentage
    cooldown_minutes: NonnegativeCount
    k_sl: PositiveDecimal
    k_tgt: PositiveDecimal
    max_premium_loss_fraction: Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def check_strategy_ranges(self):
        if self.consolidation_max_near >= self.consolidation_lookback:
            raise ValueError("Consolidation maximum must be below its lookback.")
        if any(low > high for low, high in (self.rsi_long_band, self.rsi_short_band)):
            raise ValueError("RSI bands must be ascending.")
        times = self.entry_open, self.expiry_entry_close, self.entry_close
        if any(value.tzinfo or value.second or value.microsecond for value in times):
            raise ValueError("Entry times must be IST minute boundaries.")
        if not self.entry_open < self.expiry_entry_close <= self.entry_close:
            raise ValueError("Strategy A entry windows must be ordered.")
        return self


class ExitConfig(Section):
    square_off_time: time
    backstop_time: time
    expiry_force_exit_minutes: PositiveCount
    stall_minutes: PositiveCount
    max_hold_minutes: PositiveCount
    stall_minimum_r: PositiveDecimal
    break_even_r: PositiveDecimal
    trail_start_r: PositiveDecimal
    trail_peak_fraction: TurnoverRate
    long_exit_rsi: RSIPoint
    short_exit_rsi: RSIPoint
    reprice_seconds: PositiveSeconds
    reprice_extra_ticks: tuple[NonnegativeCount, NonnegativeCount, NonnegativeCount]
    paper_protection_fraction: Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def check_exit_ranges(self):
        if any(value.tzinfo or value.second or value.microsecond for value in (self.square_off_time, self.backstop_time)):
            raise ValueError("Exit timers must be IST minute boundaries.")
        if not self.square_off_time < self.backstop_time:
            raise ValueError("Use a backstop after the initial square-off.")
        if not self.stall_minutes < self.max_hold_minutes:
            raise ValueError("The stall timer must precede the maximum hold timer.")
        if not self.stall_minimum_r < self.break_even_r < self.trail_start_r:
            raise ValueError("Trailing and stall R thresholds must increase.")
        first, second, third = self.reprice_extra_ticks
        if not first == 0 < second < third:
            raise ValueError("Use zero followed by two increasing exit repricing offsets.")
        return self


class KillSwitchConfig(Section):
    rejection_limit: PositiveCount
    max_clock_drift_seconds: PositiveDecimal
    volatility_window_minutes: PositiveCount
    volatility_move_fraction: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    sentinel_path: Path


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
    data: DataConfig
    options: OptionsConfig
    risk: RiskConfig
    costs: CostConfig
    execution: ExecutionConfig
    strategy_a: StrategyAConfig
    exits: ExitConfig
    kill_switch: KillSwitchConfig
    storage: StorageConfig
    logging: LoggingConfig
    broker: BrokerConfig

    @model_validator(mode="after")
    def check_exit_session(self):
        if not self.market.session_open < self.exits.square_off_time < self.exits.backstop_time < self.market.session_close:
            raise ValueError("Square-off and its backstop must occur within the trading session.")
        return self


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
