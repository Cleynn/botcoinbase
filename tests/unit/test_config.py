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
        ("grid_levels", 21),
        ("grid_levels", 2),
        ("max_active_pairs", 11),
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


# ------------------------------------------------------------------ Phase 2 settings
@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"TD_DB_PASSWORD": ""}, "TD_DB_PASSWORD is missing"),
        ({"TD_DB_PASSWORD": "CHANGE_ME_" + "a" * 40}, "placeholder"),
        ({"TD_DB_PASSWORD": "short"}, "shorter"),
        ({"TD_DB_USER": "tradingdots"}, "database owner"),
        ({"TD_DB_USER": "postgres"}, "database owner"),
    ],
)
def test_production_requires_a_real_least_privilege_database_login(
    prod_env: dict[str, str], override: dict[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError, match=fragment):
        load_settings({**prod_env, **override})


def test_production_database_password_is_required(prod_env: dict[str, str]) -> None:
    del prod_env["TD_DB_PASSWORD"]
    with pytest.raises(ConfigError, match="TD_DB_PASSWORD"):
        load_settings(prod_env)


@pytest.mark.parametrize(
    ("key", "value"),
    [("argon2_memory_kib", 8192), ("argon2_time_cost", 1), ("password_min_length", 10)],
)
def test_production_enforces_argon2_and_password_policy_minimums(
    prod_env: dict[str, str], config_copy: Path, key: str, value: int
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["auth"].__setitem__(key, value))
    with pytest.raises(ConfigError, match="argon2|password_min_length"):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})


def test_the_same_weak_parameters_are_fine_outside_production(
    test_env: dict[str, str], config_copy: Path
) -> None:
    _edit(
        config_copy / "base.yaml",
        lambda d: d["auth"].update(argon2_memory_kib=8, argon2_time_cost=1),
    )
    assert (
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)}).auth.argon2_memory_kib == 8
    )


def test_production_cookie_names_need_the_host_prefix(
    prod_env: dict[str, str], config_copy: Path
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["cookie"].update(name="td_session"))
    with pytest.raises(ConfigError, match="__Host-"):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})


def test_host_prefixed_cookies_cannot_be_downgraded(
    test_env: dict[str, str], config_copy: Path
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["cookie"].update(path="/app"))
    with pytest.raises(ConfigError, match="__Host-"):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


def test_session_and_login_cookie_names_must_differ(
    test_env: dict[str, str], config_copy: Path
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["cookie"].update(login_name="__Host-td_session"))
    with pytest.raises(ConfigError):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("idle_timeout_seconds", 99999),
        ("absolute_timeout_seconds", 10),
        ("max_sessions_per_user", 0),
        ("login_pair_max_failures", 0),
        ("unknown_key", 1),
    ],
)
def test_auth_settings_are_bounded_and_strict(
    test_env: dict[str, str], config_copy: Path, key: str, value: int
) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["auth"].__setitem__(key, value))
    with pytest.raises(ConfigError):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


def test_idle_timeout_cannot_exceed_the_absolute_timeout(
    test_env: dict[str, str], config_copy: Path
) -> None:
    _edit(
        config_copy / "base.yaml",
        lambda d: d["auth"].update(idle_timeout_seconds=3600, absolute_timeout_seconds=600),
    )
    with pytest.raises(ConfigError, match="idle timeout"):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


def test_trusted_proxies_must_be_networks(test_env: dict[str, str], config_copy: Path) -> None:
    _edit(config_copy / "base.yaml", lambda d: d.update(trusted_proxies=["not-a-network"]))
    with pytest.raises(ConfigError, match="trusted_proxies"):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})
    assert load_settings(test_env).trusted_proxies == ("172.29.10.0/24",)


@pytest.mark.parametrize("host", ["bad host", "a;b", "x" * 300, ""])
def test_database_host_is_validated(test_env: dict[str, str], config_copy: Path, host: str) -> None:
    _edit(config_copy / "base.yaml", lambda d: d["database"].update(host=host))
    with pytest.raises(ConfigError):
        load_settings({**test_env, "TD_CONFIG_DIR": str(config_copy)})


def test_database_password_never_appears_in_settings_output(prod_env: dict[str, str]) -> None:
    settings = load_settings(prod_env)
    assert prod_env["TD_DB_PASSWORD"] not in repr(settings) + settings.model_dump_json()


def test_database_role_passwords_for_the_migrate_command_are_accepted_but_not_stored(
    test_env: dict[str, str],
) -> None:
    settings = load_settings(
        {
            **test_env,
            "TD_DB_OWNER_PASSWORD": "o" * 40,
            "TD_DB_APP_PASSWORD": "a" * 40,
            "TD_DB_CTL_PASSWORD": "c" * 40,
        }
    )
    assert "o" * 40 not in repr(settings) and "a" * 40 not in repr(settings)
