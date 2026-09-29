from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import ConfigError, load_settings


def _edit(path: Path, mutate: object) -> None:
    data = yaml.safe_load(path.read_text())
    assert callable(mutate)
    mutate(data)
    path.write_text(yaml.safe_dump(data))


def test_default_mode_is_backtest_and_live_is_not_representable(test_env: dict[str, str]) -> None:
    assert load_settings(test_env).mode == "BACKTEST"
    assert load_settings({}).mode == "BACKTEST"


@pytest.mark.parametrize("env", [{"TD_MODE": "LIVE"}, {"TD_MODE": "live"}, {"TD_PROFILE": "live"}])
def test_live_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings({"TD_ENVIRONMENT": "test", **env})


def test_unknown_td_variable_is_rejected() -> None:
    with pytest.raises(ConfigError, match="TD_LIVE_ENABLED"):
        load_settings({"TD_ENVIRONMENT": "test", "TD_LIVE_ENABLED": "true"})


def test_invalid_boolean_is_rejected() -> None:
    with pytest.raises(ConfigError):
        load_settings({"TD_ENVIRONMENT": "test", "TD_DEBUG": "maybe"})


def test_paper_profile_allowed_outside_production_only(
    test_env: dict[str, str], prod_env: dict[str, str]
) -> None:
    assert load_settings({**test_env, "TD_PROFILE": "paper"}).mode == "PAPER"
    with pytest.raises(ConfigError, match="BACKTEST"):
        load_settings({**prod_env, "TD_PROFILE": "paper"})


def test_valid_production_configuration_loads(prod_env: dict[str, str]) -> None:
    settings = load_settings(prod_env)
    assert settings.environment == "production"
    assert settings.mode == "BACKTEST"
    assert settings.debug is False


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"TD_DEBUG": "true"}, "debug mode"),
        ({"TD_SECRET_KEY": "CHANGE_ME_" + "a" * 40}, "placeholder"),
        ({"TD_SECRET_KEY": "tooshort"}, "shorter"),
        ({"TD_SECRET_KEY": ""}, "missing"),
        ({"TD_APP_HOSTNAME": ""}, "TD_APP_HOSTNAME"),
        ({"TD_APP_HOSTNAME": "Bad_Host.example.com"}, "TD_APP_HOSTNAME"),
        ({"TD_APP_HOSTNAME": "https://app.test-host.net/"}, "TD_APP_HOSTNAME"),
        ({"TD_APP_HOSTNAME": "localhost"}, "TD_APP_HOSTNAME"),
        ({"TD_GRAFANA_HOSTNAME": ""}, "TD_GRAFANA_HOSTNAME"),
        ({"TD_GRAFANA_HOSTNAME": "app.test-host.net"}, "must differ"),
    ],
)
def test_production_validator_fails(
    prod_env: dict[str, str], override: dict[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load_settings({**prod_env, **override})


def test_missing_secret_variable_fails_in_production(prod_env: dict[str, str]) -> None:
    del prod_env["TD_SECRET_KEY"]
    with pytest.raises(ConfigError, match="missing"):
        load_settings(prod_env)


def test_production_cookie_settings_absent_fails(
    prod_env: dict[str, str], config_copy: Path
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d.pop("cookie"))
    with pytest.raises(ConfigError, match="cookie settings are absent"):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [("secure", False, "secure"), ("httponly", False, "httponly"), ("samesite", "lax", "samesite")],
)
def test_production_cookie_weakening_fails(
    prod_env: dict[str, str], config_copy: Path, key: str, value: object, fragment: str
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["cookie"].__setitem__(key, value))
    with pytest.raises(ConfigError, match=fragment):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("total_capital", "60"),
        ("max_deployment", "36"),
        ("min_reserve", "10"),
        ("grid_levels", 6),
        ("grid_levels", 2),
        ("max_active_pairs", 2),
        ("regridding_enabled", True),
        ("capital_growth_enabled", True),
        ("quote_currencies", ["USDC", "USD"]),
        ("total_capital", 50.0),
    ],
)
def test_pair_policy_ceilings_cannot_be_loosened(
    test_env: dict[str, str], config_copy: Path, key: str, value: object
) -> None:
    _edit(config_copy / "pair-policy.yaml", lambda d: d.__setitem__(key, value))
    with pytest.raises(ConfigError):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


def test_pair_policy_defaults_match_contract(test_env: dict[str, str]) -> None:
    policy = load_settings(test_env).pair_policy
    assert str(policy.total_capital) == "50"
    assert str(policy.min_reserve) == "15"
    assert str(policy.max_deployment) == "35"
    assert policy.watchlist == ("BTC-USDC", "ETH-USDC", "SOL-USDC")
