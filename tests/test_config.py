from pathlib import Path

import pytest
import yaml

from orderbot.core.config import ConfigError, load_config


CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def test_checked_in_config_round_trips(tmp_path):
    config = load_config(CONFIG)
    copy = tmp_path / "config.yaml"
    copy.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    assert load_config(copy) == config
    assert config.runtime.mode == "paper"
    assert config.risk.capital_inr is None
    assert config.risk.risk_per_trade_pct is None


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("runtime", "mode", "live"),
        ("runtime", "live_enabled", True),
        ("runtime", "live_enabled", "false"),
        ("runtime", "timezone", "UTC"),
        ("risk", "capital_inr", -1),
        ("risk", "risk_per_trade_pct", 101),
        ("risk", "risk_per_trade_pct", float("nan")),
        ("risk", "risk_per_trade_pct", True),
        ("broker", "base_url", "https://other.example"),
        ("broker", "timeout_seconds", 0),
        ("broker", "unused_setting", "secret-value"),
    ],
)
def test_invalid_config_fails_closed(tmp_path, section, key, value):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data[section][key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret-value" not in str(error.value)


def test_duplicate_keys_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("runtime: {}\nruntime: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(path)
