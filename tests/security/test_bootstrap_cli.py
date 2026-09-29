"""Host CLI: TTY-only, first ADMIN only, no secrets from argv/env, least-privilege role."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest

from app.auth.bootstrap_admin import (
    BootstrapError,
    create_first_admin,
    require_tty,
    rotate_password,
)
from app.auth.password import PasswordService
from app.domain.models import ClientIdentity, SystemClock
from app.storage.database import Storage
from tests.conftest import FAST_AUTH, GOOD_PASSWORD, Account, FakeClock, TestDb, walk_routes

ROOT = Path(__file__).resolve().parents[2]
BARE_ENV = {"PATH": "/usr/bin:/bin", "TD_ENVIRONMENT": "test"}


@pytest.fixture
def ctl_storage(db: TestDb) -> Storage:
    return Storage(db.settings_for("td_ctl"))


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make(
    storage: Storage,
    clock: FakeClock,
    passwords: PasswordService,
    username: str = "root-admin",
    password: str = GOOD_PASSWORD,
) -> Any:
    return create_first_admin(
        storage=storage,
        passwords=passwords,
        settings=FAST_AUTH,
        clock=clock,
        username=username,
        password=password,
    )


# ------------------------------------------------------------------ TTY requirement
def test_tty_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for stdin, stdout in ((False, True), (True, False), (False, False)):
        monkeypatch.setattr(sys.stdin, "isatty", lambda value=stdin: value)
        monkeypatch.setattr(sys.stdout, "isatty", lambda value=stdout: value)
        with pytest.raises(BootstrapError, match="TTY"):
            require_tty()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    require_tty()


@pytest.mark.parametrize("script", ["create_admin.py", "rotate_admin_password.py"])
def test_scripts_refuse_to_run_without_a_terminal(script: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / script)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        env=BARE_ENV,
        check=False,
    )
    assert result.returncode == 1 and "TTY" in result.stderr and "Traceback" not in result.stderr


def test_piped_input_cannot_feed_a_password() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "create_admin.py")],
        input="admin\nSecretPassword123!\nSecretPassword123!\n",
        capture_output=True,
        text=True,
        timeout=60,
        env=BARE_ENV,
        check=False,
    )
    assert result.returncode == 1 and "TTY" in result.stderr


@pytest.mark.parametrize("script", ["create_admin.py", "rotate_admin_password.py"])
def test_scripts_take_no_arguments_so_passwords_never_appear_in_argv(script: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / script), "--password", "hunter2hunter2"],
        capture_output=True,
        text=True,
        timeout=60,
        env=BARE_ENV,
        check=False,
    )
    assert result.returncode == 2 and "no arguments" in result.stderr
    assert "hunter2hunter2" not in result.stdout + result.stderr


def _interactive(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    settings: Any,
    db: TestDb,
    username: str,
    passwords: list[str],
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: settings.model_copy(update={"database": db.settings_for("td_ctl")}),
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": username)
    answers = iter(passwords)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt="": next(answers))


def test_interactive_flow_creates_the_admin_via_the_restricted_role(
    monkeypatch: pytest.MonkeyPatch,
    db: TestDb,
    settings: Any,
    capsys: pytest.CaptureFixture[str],
    sql: Callable[..., Any],
) -> None:
    module = load_script("create_admin.py")
    monkeypatch.setattr(
        module, "PasswordService", PasswordService
    )  # real service, fast params below
    _interactive(
        monkeypatch,
        module,
        settings.model_copy(update={"auth": FAST_AUTH}),
        db,
        "  Root-Admin ",
        [GOOD_PASSWORD, GOOD_PASSWORD],
    )
    assert module.main([]) == 0
    out = capsys.readouterr()
    assert "root-admin" in out.out and GOOD_PASSWORD not in out.out + out.err
    row = sql("SELECT username, role, password_hash FROM users")[0]
    assert (row["username"], row["role"]) == ("root-admin", "ADMIN") and row[
        "password_hash"
    ].startswith("$argon2id$")


def test_mismatched_confirmation_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, db: TestDb, settings: Any, sql: Callable[..., Any]
) -> None:
    module = load_script("create_admin.py")
    _interactive(
        monkeypatch, module, settings, db, "root-admin", [GOOD_PASSWORD, GOOD_PASSWORD + "x"]
    )
    assert module.main([]) == 1
    assert sql("SELECT count(*) AS n FROM users")[0]["n"] == 0


# ------------------------------------------------------------------ first ADMIN only
def test_creates_exactly_one_first_admin_and_audits_it(
    ctl_storage: Storage, clock: FakeClock, passwords: PasswordService, sql: Callable[..., Any]
) -> None:
    user = make(ctl_storage, clock, passwords)
    assert user.username == "root-admin" and user.role.value == "ADMIN"
    event = sql("SELECT * FROM audit_events")[0]
    assert (event["event_code"], event["actor_role"], event["client_tag"]) == (
        "admin.created",
        "HOST_CLI",
        "host_cli",
    )
    assert str(event["target_id"]) == str(user.id) and event["actor_user_id"] is None
    assert GOOD_PASSWORD not in repr(sql("SELECT * FROM audit_events")) + repr(
        sql("SELECT * FROM users")
    )


def test_a_second_admin_cannot_be_created(
    ctl_storage: Storage, clock: FakeClock, passwords: PasswordService
) -> None:
    make(ctl_storage, clock, passwords)
    with pytest.raises(BootstrapError, match="already exists"):
        make(ctl_storage, clock, passwords, username="another-admin")


def test_concurrent_first_admin_creation_yields_exactly_one(
    ctl_storage: Storage, clock: FakeClock, passwords: PasswordService, sql: Callable[..., Any]
) -> None:
    results: list[str] = []

    def attempt(name: str) -> None:
        try:
            make(ctl_storage, clock, passwords, username=name)
            results.append("ok")
        except BootstrapError:
            results.append("refused")

    threads = [threading.Thread(target=attempt, args=(f"admin-{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok"] + ["refused"] * 4
    assert sql("SELECT count(*) AS n FROM users WHERE role = 'ADMIN'")[0]["n"] == 1


@pytest.mark.parametrize(
    "username", ["", "ab", "Has Space", "bad/name", "-leading", "x" * 65, "ünïcode"]
)
def test_invalid_usernames_are_refused(
    ctl_storage: Storage, clock: FakeClock, passwords: PasswordService, username: str
) -> None:
    with pytest.raises(BootstrapError, match="username"):
        make(ctl_storage, clock, passwords, username=username)


@pytest.mark.parametrize(
    "password",
    ["short", "passwordpassword", "root-admin-is-my-name", "aaaaaaaaaaaaaaaa", "x" * 200],
)
def test_password_policy_applies_to_the_cli(
    ctl_storage: Storage,
    clock: FakeClock,
    passwords: PasswordService,
    password: str,
    sql: Callable[..., Any],
) -> None:
    with pytest.raises(BootstrapError):
        make(ctl_storage, clock, passwords, password=password)
    assert sql("SELECT count(*) AS n FROM users")[0]["n"] == 0


def test_the_created_admin_can_sign_in_over_http(
    ctl_storage: Storage,
    clock: FakeClock,
    passwords: PasswordService,
    client: Any,
    login: Callable[..., Any],
) -> None:
    make(ctl_storage, clock, passwords)
    assert login(client, "root-admin", GOOD_PASSWORD).status_code == 303


# ------------------------------------------------------------------ password rotation
def test_rotation_ends_all_sessions_replaces_the_password_and_audits(
    ctl_storage: Storage,
    clock: FakeClock,
    passwords: PasswordService,
    services: Any,
    admin: Account,
    sql: Callable[..., Any],
) -> None:
    client = ClientIdentity.from_key("f" * 64)
    tokens = [
        services.auth.login(admin.username, admin.password, client, "rid").token for _ in range(2)
    ]
    ended = rotate_password(
        storage=ctl_storage,
        passwords=passwords,
        settings=FAST_AUTH,
        clock=clock,
        username=admin.username,
        new_password="a rotated passphrase 2026",
    )
    assert ended == 2
    assert all(
        services.auth.validate(t, touch=False, client=client, request_id="r") is None
        for t in tokens
    )
    assert services.auth.login(admin.username, admin.password, client, "rid").status == "invalid"
    assert (
        services.auth.login(admin.username, "a rotated passphrase 2026", client, "rid").status
        == "ok"
    )
    event = sql("SELECT * FROM audit_events WHERE event_code = 'admin.password_rotated'")[0]
    assert event["actor_role"] == "HOST_CLI" and "rotated passphrase" not in repr(
        sql("SELECT * FROM audit_events")
    )


def test_rotation_rejects_unknown_users_and_weak_passwords(
    ctl_storage: Storage, clock: FakeClock, passwords: PasswordService, admin: Account
) -> None:
    with pytest.raises(BootstrapError, match="unknown user"):
        rotate_password(
            storage=ctl_storage,
            passwords=passwords,
            settings=FAST_AUTH,
            clock=clock,
            username="ghost",
            new_password="a rotated passphrase 2026",
        )
    with pytest.raises(BootstrapError):
        rotate_password(
            storage=ctl_storage,
            passwords=passwords,
            settings=FAST_AUTH,
            clock=clock,
            username=admin.username,
            new_password="short",
        )


# ------------------------------------------------------------------ no web path
def test_no_web_route_can_create_users(app: Any) -> None:
    assert not [r.path for r in walk_routes(app) if "user" in r.path and "revoke" not in r.path]


def test_the_web_role_cannot_perform_bootstrap(
    db: TestDb, clock: FakeClock, passwords: PasswordService
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        make(Storage(db.settings_for("td_app")), clock, passwords)


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None
