"""Configuration loading and validation. Fails closed on unknown settings, LIVE, money floats."""

from __future__ import annotations

import ipaddress
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
    Field,
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
    "TD_DB_USER": "database.user",
    "TD_DB_PASSWORD": "database.password",
    "TD_METRICS_BIND": "monitoring.bind_address",
}
# Accepted but not mapped onto Settings (used by profile selection, the migrate command and
# environment-file validation).
_ENV_CONTROL = {
    "TD_PROFILE",
    "TD_CONFIG_DIR",
    "TD_SECRET_KEY_FILE",
    "TD_DB_OWNER_PASSWORD",
    "TD_DB_APP_PASSWORD",
    "TD_DB_CTL_PASSWORD",
}
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


_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_DB_IDENT_RE = re.compile(r"^[A-Za-z0-9_]{1,63}$")
_DB_HOST_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,255}$")


class CookieSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    secure: bool
    httponly: bool
    samesite: Literal["strict", "lax", "none"]
    path: str = "/"
    name: str = "__Host-td_session"
    login_name: str = "__Host-td_login"

    @model_validator(mode="after")
    def _names(self) -> CookieSettings:
        for value in (self.name, self.login_name):
            if not _COOKIE_NAME_RE.fullmatch(value):
                raise ValueError("cookie names may only use letters, digits, '_' and '-'")
            if value.startswith("__Host-") and not (self.secure and self.path == "/"):
                raise ValueError("a __Host- cookie requires secure=true and path=/")
        if self.name == self.login_name:
            raise ValueError("session and login cookie names must differ")
        return self


class AuthSettings(BaseModel):
    """Session, password and login-throttle parameters (production minimums checked separately)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idle_timeout_seconds: int = Field(1800, ge=60, le=3600)
    absolute_timeout_seconds: int = Field(43200, ge=300, le=86400)
    max_sessions_per_user: int = Field(5, ge=1, le=10)
    reauth_window_seconds: int = Field(120, ge=30, le=600)
    touch_interval_seconds: int = Field(60, ge=1, le=600)
    argon2_memory_kib: int = Field(47104, ge=8)
    argon2_time_cost: int = Field(2, ge=1, le=10)
    argon2_parallelism: int = Field(1, ge=1, le=8)
    password_min_length: int = Field(14, ge=8, le=64)
    password_max_length: int = Field(128, ge=64, le=1024)
    login_window_seconds: int = Field(900, ge=60, le=86400)
    login_pair_max_failures: int = Field(5, ge=1, le=100)
    login_client_max_failures: int = Field(20, ge=1, le=1000)
    login_account_max_failures: int = Field(30, ge=1, le=1000)

    @model_validator(mode="after")
    def _ordering(self) -> AuthSettings:
        if self.idle_timeout_seconds > self.absolute_timeout_seconds:
            raise ValueError("idle timeout cannot exceed the absolute timeout")
        return self


class MonitoringSettings(BaseModel):
    """Internal metrics listener. It is never routed by Caddy and never published on the host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    bind_address: str = "127.0.0.1"
    port: int = Field(9464, ge=1024, le=65535)
    allowed_scrapers: tuple[str, ...] = ("127.0.0.1/32", "172.29.20.0/24")
    cache_seconds: int = Field(10, ge=1, le=60)
    chain_verify_interval_seconds: int = Field(300, ge=30, le=3600)

    @model_validator(mode="after")
    def _addresses(self) -> MonitoringSettings:
        try:
            ipaddress.ip_address(self.bind_address)
        except ValueError as exc:
            raise ValueError("monitoring.bind_address must be an IP address") from exc
        for item in self.allowed_scrapers:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError("monitoring.allowed_scrapers must be IP networks") from exc
        return self


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "postgres"
    port: int = Field(5432, ge=1, le=65535)
    name: str = "tradingdots"
    user: str = "td_app"
    owner_user: str = "tradingdots"
    password: SecretStr | None = None

    @model_validator(mode="after")
    def _shape(self) -> DatabaseSettings:
        if not _DB_HOST_RE.fullmatch(self.host):
            raise ValueError("database host contains invalid characters")
        for value in (self.name, self.user, self.owner_user):
            if not _DB_IDENT_RE.fullmatch(value):
                raise ValueError("database identifiers may only use letters, digits and '_'")
        return self


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
    trusted_proxies: tuple[str, ...] = ()
    auth: AuthSettings = AuthSettings()
    database: DatabaseSettings = DatabaseSettings()
    monitoring: MonitoringSettings = MonitoringSettings()
    pair_policy: PairPolicy

    @field_validator("trusted_proxies")
    @classmethod
    def _proxies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError("trusted_proxies must be IP addresses or CIDR networks") from exc
        return value

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


# Explicit list: Python's is_private also accepts reserved documentation ranges
# (e.g. 203.0.113.0/24), which must not count as internal.
_INTERNAL_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128")
)


def _internal_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address.version == n.version and address in n for n in _INTERNAL_NETS)


def _internal_network(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    return any(net.version == n.version and net.subnet_of(n) for n in _INTERNAL_NETS)  # type: ignore[arg-type]


def monitoring_problems(monitoring: MonitoringSettings) -> list[str]:
    """The metrics listener must sit on one specific private address, reachable only by scrapers."""
    problems: list[str] = []
    bind = ipaddress.ip_address(monitoring.bind_address)
    if bind.is_unspecified:
        problems.append("monitoring.bind_address must be a specific address, not a wildcard")
    elif not _internal_address(bind):
        problems.append("monitoring.bind_address must be a private or loopback address")
    for item in monitoring.allowed_scrapers:
        net = ipaddress.ip_network(item, strict=False)
        minimum = 16 if net.version == 4 else 48
        if net.prefixlen < minimum or not _internal_network(net):
            problems.append("monitoring.allowed_scrapers must be small private networks")
            break
    return problems


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
    db_password = (
        settings.database.password.get_secret_value() if settings.database.password else None
    )
    found = secret_problem("TD_DB_PASSWORD", db_password)
    if found:
        problems.append(found)
    if settings.database.user in {settings.database.owner_user, "postgres"}:
        problems.append("the application must not connect as the database owner or superuser")
    auth = settings.auth
    if auth.argon2_memory_kib < 19456 or auth.argon2_time_cost < 2:
        problems.append("argon2 parameters are below the production minimum (19456 KiB, t=2)")
    if auth.password_min_length < 14:
        problems.append("password_min_length must be at least 14 in production")
    problems.extend(monitoring_problems(settings.monitoring))
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
        if not (cookie.name.startswith("__Host-") and cookie.login_name.startswith("__Host-")):
            problems.append("cookie names must use the __Host- prefix")
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


def _set_path(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    current = data
    for part in parents:
        nested = current.setdefault(part, {})
        if not isinstance(nested, dict):
            raise ConfigError(f"configuration key '{part}' must be a mapping")
        current = nested
    current[leaf] = value


def load_database_target(env: Mapping[str, str] | None = None) -> DatabaseSettings:
    """Load only the non-secret database block (used by the migrate command)."""
    env = os.environ if env is None else env
    config_dir = Path(env.get("TD_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    data = _read_yaml(config_dir / "base.yaml")
    try:
        return DatabaseSettings.model_validate(data.get("database", {}))
    except ValidationError as exc:
        raise ConfigError("invalid database configuration") from exc


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
            _set_path(data, field, _parse_bool(var, env[var]) if field == "debug" else env[var])

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
