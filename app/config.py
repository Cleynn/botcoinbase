"""Configuration loading and validation. Fails closed on unknown settings, LIVE, money floats."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app import constants

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
PROFILES = ("backtest", "paper")

# Environment variables that map onto settings. Any other TD_* variable is rejected.
_ENV_FIELDS = {
    "TD_ENVIRONMENT": "environment",
    "TD_DEBUG": "debug",
    "TD_MODE": "mode",
    "TD_APP_HOSTNAME": "app_hostname",
    "TD_GRAFANA_HOSTNAME": "grafana_hostname",
    "TD_LOG_LEVEL": "log_level",
    "TD_SECRET_KEY": "secret_key",
}
_ENV_CONTROL = {"TD_PROFILE", "TD_CONFIG_DIR", "TD_SECRET_KEY_FILE"}
_KNOWN_ENV = set(_ENV_FIELDS) | _ENV_CONTROL

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)


class ConfigError(ValueError):
    """Raised for any invalid configuration. Messages never contain secret values."""


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("floats are not allowed for money or limits; use a string or integer")
    return value


class CookieSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    secure: bool
    httponly: bool
    samesite: Literal["strict", "lax", "none"]
    path: str = "/"


class PairPolicy(BaseModel):
    """Static pair/capital policy. Values may only tighten the hard ceilings in app.constants."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_currencies: tuple[str, ...]
    watchlist: tuple[str, ...]
    total_capital: Decimal
    min_reserve: Decimal
    max_deployment: Decimal
    grid_levels: int
    max_active_pairs: int
    regridding_enabled: bool = False
    capital_growth_enabled: bool = False

    @field_validator("total_capital", "min_reserve", "max_deployment", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        return _reject_float(value)

    @model_validator(mode="after")
    def _ceilings(self) -> PairPolicy:
        if self.quote_currencies != ("USDC",):
            raise ValueError("only USDC-quoted products are permitted")
        if any(not re.fullmatch(r"[A-Z0-9]+-USDC", p) or len(p) > 24 for p in self.watchlist):
            raise ValueError("watchlist entries must look like BASE-USDC")
        if self.total_capital > constants.POLICY_TOTAL_CAPITAL:
            raise ValueError("total_capital exceeds the hard ceiling")
        if self.min_reserve < constants.POLICY_MIN_RESERVE:
            raise ValueError("min_reserve is below the hard floor")
        if self.max_deployment > constants.POLICY_MAX_DEPLOYMENT:
            raise ValueError("max_deployment exceeds the hard ceiling")
        if self.max_deployment + self.min_reserve > self.total_capital:
            raise ValueError("reserve plus deployment exceeds total capital")
        if not constants.GRID_MIN_LEVELS <= self.grid_levels <= constants.GRID_MAX_LEVELS:
            raise ValueError("grid_levels outside the allowed range")
        if self.max_active_pairs != constants.MAX_ACTIVE_PAIRS:
            raise ValueError("exactly one active pair is permitted")
        if self.regridding_enabled or self.capital_growth_enabled:
            raise ValueError("regridding and capital growth must stay disabled")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    mode: str = constants.DEFAULT_MODE
    app_hostname: str | None = None
    grafana_hostname: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cookie: CookieSettings | None = None
    secret_key: SecretStr | None = None
    pair_policy: PairPolicy

    @field_validator("mode")
    @classmethod
    def _mode_allowed(cls, value: str) -> str:
        if value not in constants.ALLOWED_MODES:
            raise ValueError("mode must be BACKTEST or PAPER; LIVE is not representable")
        return value


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker.lower() in lowered for marker in constants.PLACEHOLDER_MARKERS)


def secret_problem(name: str, value: str | None) -> str | None:
    """Describe why a secret is unacceptable, without echoing it."""
    if not value:
        return f"{name} is missing"
    if is_placeholder(value):
        return f"{name} is a placeholder value"
    if len(value) < constants.MIN_SECRET_LENGTH:
        return f"{name} is shorter than {constants.MIN_SECRET_LENGTH} characters"
    return None


def valid_hostname(value: str | None) -> bool:
    return bool(value) and _HOSTNAME_RE.fullmatch(value or "") is not None


def production_problems(settings: Settings) -> list[str]:
    """Return every reason the settings are unsafe for production (empty list means acceptable)."""
    problems: list[str] = []
    if settings.environment != "production":
        return problems
    if settings.debug:
        problems.append("debug mode is enabled")
    if settings.mode != constants.DEFAULT_MODE:
        problems.append("only BACKTEST mode is permitted in production during Phase 1")
    key = settings.secret_key.get_secret_value() if settings.secret_key else None
    found = secret_problem("TD_SECRET_KEY", key)
    if found:
        problems.append(found)
    cookie = settings.cookie
    if cookie is None:
        problems.append("production cookie settings are absent")
    else:
        if not cookie.secure:
            problems.append("cookie.secure must be true")
        if not cookie.httponly:
            problems.append("cookie.httponly must be true")
        if cookie.samesite != "strict":
            problems.append("cookie.samesite must be strict")
        if cookie.path != "/":
            problems.append("cookie.path must be /")
    if not valid_hostname(settings.app_hostname):
        problems.append("TD_APP_HOSTNAME is missing or invalid")
    if not valid_hostname(settings.grafana_hostname):
        problems.append("TD_GRAFANA_HOSTNAME is missing or invalid")
    if valid_hostname(settings.app_hostname) and settings.app_hostname == settings.grafana_hostname:
        problems.append("app and Grafana hostnames must differ")
    return problems


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration file {path.name}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"configuration file {path.name} must contain a mapping")
    return data


def _deep_merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load YAML profile plus whitelisted environment overrides, then validate for production."""
    env = os.environ if env is None else env
    unknown = sorted(k for k in env if k.startswith("TD_") and k not in _KNOWN_ENV)
    if unknown:
        raise ConfigError(f"unknown settings rejected: {', '.join(unknown)}")

    profile = env.get("TD_PROFILE", "backtest").strip().lower()
    if profile not in PROFILES:
        raise ConfigError("TD_PROFILE must be one of: " + ", ".join(PROFILES))
    config_dir = Path(env.get("TD_CONFIG_DIR") or DEFAULT_CONFIG_DIR)

    data = _read_yaml(config_dir / "base.yaml")
    data = _deep_merge(data, _read_yaml(config_dir / f"{profile}.yaml"))
    data["pair_policy"] = _read_yaml(config_dir / "pair-policy.yaml")

    for var, field in _ENV_FIELDS.items():
        if var in env:
            data[field] = _parse_bool(var, env[var]) if field == "debug" else env[var]

    key_file = env.get("TD_SECRET_KEY_FILE")
    if key_file:
        try:
            data["secret_key"] = Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError("cannot read TD_SECRET_KEY_FILE") from exc

    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        # Pydantic error text can echo input values; report locations and reasons only.
        summary = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(summary or "invalid configuration") from None

    problems = production_problems(settings)
    if problems:
        raise ConfigError("unsafe production configuration: " + "; ".join(problems))
    return settings
