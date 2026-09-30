"""Shared fixtures. Tests run against a real PostgreSQL (pgserver, or TD_TEST_PG_URI).

Secrets and passwords are generated at runtime; none are committed. Each test gets its own database
cloned from a migrated template, so tests are isolated and the append-only audit log needs
no cleanup.
"""

from __future__ import annotations

import importlib.util
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.api.app import create_app
from app.auth.password import PasswordService
from app.config import AuthSettings, DatabaseSettings, FeePolicy, Settings, load_settings
from app.domain.enums import Role
from app.domain.models import User
from app.storage.database import OwnerTarget, Storage, migrate

ROOT = Path(__file__).resolve().parent.parent

# Phase 1.0 failing skeletons for later phases are parked here and not collected.
collect_ignore_glob = ["pending/*"]

FAST_AUTH = AuthSettings(argon2_memory_kib=8, argon2_time_cost=1, argon2_parallelism=1)
GOOD_PASSWORD = "correct horse battery staple 42"
ORIGIN = "https://testserver"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
        self.hooks: list[
            Callable[[datetime], None]
        ] = []  # e.g. keep the database test clock in step

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        for hook in self.hooks:
            hook(self._now)


@dataclass(frozen=True)
class PgTarget:
    host: str
    port: int
    user: str
    password: str | None

    def conninfo(self, dbname: str) -> str:
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=dbname,
            connect_timeout=10,
        )


@dataclass(frozen=True)
class TestDb:
    __test__ = False
    name: str
    target: PgTarget
    app_password: str
    ctl_password: str

    def owner_conninfo(self) -> str:
        return self.target.conninfo(self.name)

    def owner_target(self) -> OwnerTarget:
        t = self.target
        return OwnerTarget(t.host, t.port, self.name, t.user, t.password)

    def settings_for(self, role: str) -> DatabaseSettings:
        from pydantic import SecretStr

        password = self.app_password if role == "td_app" else self.ctl_password
        return DatabaseSettings(
            host=self.target.host,
            port=self.target.port,
            name=self.name,
            user=role,
            password=SecretStr(password),
        )


@dataclass(frozen=True)
class Account:
    user: User
    password: str

    @property
    def username(self) -> str:
        return self.user.username


@pytest.fixture(scope="session")
def secret_key() -> str:
    return secrets.token_urlsafe(48)


@pytest.fixture(scope="session")
def role_passwords() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


@pytest.fixture(scope="session")
def pg_target(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgTarget]:
    uri = os.environ.get("TD_TEST_PG_URI")
    server: Any = None
    if not uri:
        try:
            import pgserver
        except ImportError:
            pytest.fail(
                "PostgreSQL is required: install dev dependencies (pgserver) or set TD_TEST_PG_URI"
            )
        server = pgserver.get_server(  # type: ignore[attr-defined]
            tmp_path_factory.mktemp("pg"), cleanup_mode="stop"
        )
        uri = server.get_uri()
    info = conninfo_to_dict(uri)
    yield PgTarget(
        str(info.get("host", "localhost")),
        int(str(info.get("port", 5432))),
        str(info.get("user", "postgres")),
        str(info["password"]) if info.get("password") else None,
    )
    if server is not None:
        server.cleanup()


@pytest.fixture(scope="session")
def template_db(pg_target: PgTarget, role_passwords: tuple[str, str]) -> str:
    name = "td_template"
    with psycopg.connect(pg_target.conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    migrate(
        OwnerTarget(pg_target.host, pg_target.port, name, pg_target.user, pg_target.password),
        app_password=role_passwords[0],
        ctl_password=role_passwords[1],
    )
    return name


@pytest.fixture
def db(pg_target: PgTarget, template_db: str, role_passwords: tuple[str, str]) -> Iterator[TestDb]:
    name = f"td_t_{uuid4().hex[:12]}"
    with psycopg.connect(pg_target.conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template_db}")
    yield TestDb(name, pg_target, role_passwords[0], role_passwords[1])
    with psycopg.connect(pg_target.conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def test_env(secret_key: str) -> dict[str, str]:
    return {"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": secret_key}


@pytest.fixture
def prod_env() -> dict[str, str]:
    return {
        "TD_ENVIRONMENT": "production",
        "TD_SECRET_KEY": secrets.token_urlsafe(48),
        "TD_DB_PASSWORD": secrets.token_urlsafe(48),
        "TD_APP_HOSTNAME": "app.test-host.net",
        "TD_GRAFANA_HOSTNAME": "grafana.test-host.net",
    }


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(ROOT / "config", target)
    return target


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(test_env: dict[str, str], db: TestDb) -> Settings:
    base = load_settings(test_env)
    configured = base.model_copy(
        update={
            "database": db.settings_for("td_app"),
            "auth": FAST_AUTH,
            "trusted_proxies": ("10.0.0.0/8",),
        }
    )
    return with_fees(configured, ATTESTED_FEES)


ATTESTED_FEES = FeePolicy(operator_maker_rate="0.002", attested_on=date(2026, 9, 20))


def with_fees(settings: Settings, fees: FeePolicy) -> Settings:
    policy = settings.pair_policy.model_copy(update={"fees": fees})
    return settings.model_copy(update={"pair_policy": policy})


@pytest.fixture
def storage(settings: Settings) -> Storage:
    return Storage(settings.database)


@pytest.fixture
def passwords() -> PasswordService:
    return PasswordService.create(FAST_AUTH)


@pytest.fixture
def app(settings: Settings, storage: Storage, clock: FakeClock) -> Any:
    return create_app(settings, storage=storage, clock=clock)


@pytest.fixture
def make_client(app: Any) -> Callable[..., TestClient]:
    def build(*, origin: str | None = ORIGIN, peer: str = "10.0.0.5") -> TestClient:
        headers = {"origin": origin} if origin else {}
        return TestClient(
            app,
            base_url="https://testserver",
            raise_server_exceptions=False,
            client=(peer, 50000),
            headers=headers,
        )

    return build


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def make_user(db: TestDb, passwords: PasswordService, clock: FakeClock) -> Callable[..., Account]:
    def build(username: str, role: Role = Role.VIEWER, password: str = GOOD_PASSWORD) -> Account:
        now = clock.now()
        user = User(uuid4(), username, role, passwords.hash(password), now, now, None)
        with psycopg.connect(db.owner_conninfo()) as conn:
            conn.execute(
                "INSERT INTO users (id, username, role, password_hash, "
                "password_changed_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user.id, user.username, user.role.value, user.password_hash, now, now),
            )
        return Account(user, password)

    return build


@pytest.fixture
def admin(make_user: Callable[..., Account]) -> Account:
    return make_user("alice", Role.ADMIN)


@pytest.fixture
def viewer(make_user: Callable[..., Account]) -> Account:
    return make_user("victor", Role.VIEWER)


def csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "no csrf_token field found in page"
    return match.group(1)


@pytest.fixture
def login() -> Callable[..., Any]:
    def do(client: TestClient, username: str, password: str) -> Any:
        page = client.get("/login")
        assert page.status_code == 200, page.text
        return client.post(
            "/login",
            data={"csrf_token": csrf_from(page.text), "username": username, "password": password},
            follow_redirects=False,
        )

    return do


@pytest.fixture
def admin_client(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> TestClient:
    client = make_client()
    response = login(client, admin.username, admin.password)
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def viewer_client(
    make_client: Callable[..., TestClient], login: Callable[..., Any], viewer: Account
) -> TestClient:
    client = make_client(peer="10.0.0.6")
    response = login(client, viewer.username, viewer.password)
    assert response.status_code == 303, response.text
    return client


@pytest.fixture(scope="session")
def verify() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_security_config", ROOT / "scripts" / "verify_security_config.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded


@pytest.fixture
def caddyfile() -> str:
    return (ROOT / "infra" / "caddy" / "Caddyfile").read_text()


@pytest.fixture
def sql(db: TestDb) -> Callable[..., list[dict[str, Any]]]:
    """Run SQL as the database owner (bypasses grants; used to arrange and inspect state)."""
    from psycopg.rows import dict_row

    def run(query: str, params: Any = None) -> list[dict[str, Any]]:
        with psycopg.connect(db.owner_conninfo(), row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return list(cur.fetchall()) if cur.description else []

    return run


@pytest.fixture
def services(app: Any) -> Any:
    return app.state.services


@pytest.fixture
def audit_codes(sql: Callable[..., list[dict[str, Any]]]) -> Callable[[], list[str]]:
    def read() -> list[str]:
        return [r["event_code"] for r in sql("SELECT event_code FROM audit_events ORDER BY seq")]

    return read


def walk_routes(app: Any) -> list[Any]:
    """Every effective APIRoute of the app, flattening lazily included routers."""

    def walk(routes: list[Any]) -> list[Any]:
        found: list[Any] = []
        for route in routes:
            inner = getattr(route, "original_router", None)
            found.extend(walk(inner.routes) if inner is not None else [route])
        return found

    from fastapi.routing import APIRoute

    return [r for r in walk(app.router.routes) if isinstance(r, APIRoute)]


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


COOKIE_DOMAIN = "testserver.local"  # the domain the HTTP client's jar assigns to dotless hosts


def plant_cookie(client: TestClient, name: str, value: str) -> None:
    """Set a cookie exactly as a browser would hold it, replacing any existing one."""
    for existing in [ck for ck in client.cookies.jar if ck.name == name]:
        client.cookies.jar.clear(existing.domain, existing.path, existing.name)
    client.cookies.set(name, value, domain=COOKIE_DOMAIN, path="/")


# ---------------------------------------------------------------- Phase 4: pairs
@pytest.fixture
def coinbase(clock: FakeClock) -> Any:
    from tests.coinbase_fakes import FakeCoinbase

    return FakeCoinbase(clock.now)


@pytest.fixture
def ctl_storage(settings: Settings, db: TestDb) -> Storage:
    """Storage connected as td_ctl, the host CLI role that runs discovery and validation."""
    return Storage(settings.model_copy(update={"database": db.settings_for("td_ctl")}).database)


@pytest.fixture
def runner(settings: Settings, ctl_storage: Storage, clock: FakeClock, coinbase: Any) -> Any:
    from app.pairs.runner import PairRunner

    return PairRunner(storage=ctl_storage, clock=clock, settings=settings, client=coinbase.client())


@pytest.fixture
def open_gate() -> Any:
    """A runtime gate that permits activation and reports pairs clean (tests only)."""
    from app.storage.repositories import Repos

    class OpenGate:
        clean = True

        def activation_blockers(self, repos: Repos, pair: Any) -> tuple[str, ...]:
            return ()

        def is_clean(self, repos: Repos, pair: Any) -> tuple[bool, tuple[str, ...]]:
            return (True, ()) if self.clean else (False, ("NOT_CLEAN",))

    return OpenGate()


@pytest.fixture
def open_app(settings: Settings, storage: Storage, clock: FakeClock, open_gate: Any) -> Any:
    return create_app(settings, storage=storage, clock=clock, pair_gate=open_gate)


@pytest.fixture
def env(
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    settings: Settings,
    coinbase: Any,
    admin: Account,
    open_gate: Any,
) -> Any:
    """Service-level harness with an OPEN runtime gate (tests only)."""
    from tests.pair_env import build_env

    return build_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=settings,
        coinbase=coinbase,
        admin=admin,
        gate=open_gate,
    )


@pytest.fixture
def closed_env(
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    settings: Settings,
    coinbase: Any,
    admin: Account,
) -> Any:
    """Like `env` but with the production default gate (no bot state: activation refused)."""
    from app.pairs.service import UnavailableRuntimeGate
    from tests.pair_env import build_env

    return build_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=settings,
        coinbase=coinbase,
        admin=admin,
        gate=UnavailableRuntimeGate(settings.mode),
    )
