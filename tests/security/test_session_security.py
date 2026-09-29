from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.domain.enums import Role
from tests.conftest import Account, FakeClock, csrf_from, plant_cookie

SESSION = "__Host-td_session"


def token_of(client: TestClient) -> str:
    value = client.cookies.get(SESSION)
    assert value
    return str(value)


def test_cookie_is_host_prefixed_secure_httponly_strict_and_host_only(
    client: TestClient, admin: Account, login: Callable[..., Any]
) -> None:
    response = login(client, admin.username, admin.password)
    line = next(h for h in response.headers.get_list("set-cookie") if h.startswith(SESSION))
    parts = [p.strip().lower() for p in line.split(";")]
    assert {"secure", "httponly", "samesite=strict", "path=/"} <= set(parts)
    assert not any(p.startswith(("domain=", "max-age=", "expires=")) for p in parts)


def test_production_settings_require_the_hardened_cookie(prod_env: dict[str, str]) -> None:
    settings = load_settings(prod_env)
    cookie = settings.cookie
    assert cookie is not None
    assert (cookie.secure, cookie.httponly, cookie.samesite, cookie.path) == (
        True,
        True,
        "strict",
        "/",
    )
    assert cookie.name.startswith("__Host-") and cookie.login_name.startswith("__Host-")


def test_only_a_hash_of_the_token_is_stored(
    admin_client: TestClient, sql: Callable[..., Any]
) -> None:
    token = token_of(admin_client)
    row = sql("SELECT * FROM sessions")[0]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    dump = (
        repr(sql("SELECT * FROM sessions"))
        + repr(sql("SELECT * FROM audit_events"))
        + repr(sql("SELECT * FROM login_attempts"))
    )
    assert token not in dump


def test_tokens_are_long_random_and_unique(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    clock: FakeClock,
) -> None:
    seen = set()
    for i in range(12):
        client = make_client(peer=f"10.20.0.{i + 1}")
        login(client, admin.username, admin.password)
        token = token_of(client)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)  # 256 bits, URL-safe
        seen.add(token)
        clock.advance(1)
    assert len(seen) == 12


def test_session_fixation_the_server_never_adopts_a_client_supplied_token(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    sql: Callable[..., Any],
) -> None:
    attacker = make_client(peer="10.21.0.1")
    login(attacker, admin.username, admin.password)  # the attacker holds a valid token...
    planted = token_of(attacker)
    victim = make_client(peer="10.21.0.2")
    page = victim.get("/login")  # ...and plants it in the victim's browser before they sign in
    plant_cookie(victim, SESSION, planted)
    response = victim.post(
        "/login",
        data={
            "csrf_token": csrf_from(page.text),
            "username": admin.username,
            "password": admin.password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    fresh = token_of(victim)
    assert fresh != planted  # a new identifier is always issued at login
    assert (
        attacker.get("/", follow_redirects=False).status_code == 303
    )  # the planted session was ended
    assert victim.get("/", follow_redirects=False).status_code == 200
    reasons = {
        r["revoked_reason"]
        for r in sql("SELECT revoked_reason FROM sessions WHERE revoked_at IS NOT NULL")
    }
    assert reasons == {"ROTATED"}


def test_arbitrary_cookie_values_never_become_sessions(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    client = make_client()
    supplied = "A" * 43
    plant_cookie(client, SESSION, supplied)
    login(client, admin.username, admin.password)
    assert token_of(client) != supplied


def test_idle_timeout_over_http_is_audited_once_and_clears_the_cookie(
    admin_client: TestClient, clock: FakeClock, audit_codes: Callable[[], list[str]]
) -> None:
    clock.advance(1799)
    assert admin_client.get("/").status_code == 200
    clock.advance(1800)
    expired = admin_client.get("/", follow_redirects=False)
    assert expired.status_code == 303 and expired.headers["location"] == "/login"
    assert "max-age=0" in "".join(expired.headers.get_list("set-cookie")).lower()
    admin_client.get("/", follow_redirects=False)
    assert audit_codes().count("session.expired") == 1


def test_absolute_expiry_holds_despite_constant_activity(
    admin_client: TestClient, clock: FakeClock, sql: Callable[..., Any]
) -> None:
    for _ in range(28):  # 28 x 25 min = 11 h 40 min of steady use
        clock.advance(1500)
        assert admin_client.get("/").status_code == 200
    clock.advance(1500)  # crosses 12 h
    assert admin_client.get("/", follow_redirects=False).status_code == 303
    assert sql("SELECT revoked_reason FROM sessions")[0]["revoked_reason"] == "ABSOLUTE_EXPIRY"


def test_revoked_cookies_cannot_be_replayed_from_another_browser(
    admin_client: TestClient, make_client: Callable[..., TestClient]
) -> None:
    stolen = token_of(admin_client)
    thief = make_client(peer="203.0.113.99")
    plant_cookie(thief, SESSION, stolen)
    assert (
        thief.get("/", follow_redirects=False).status_code == 200
    )  # control: the planted cookie is sent
    admin_client.post(
        "/logout",
        data={"csrf_token": csrf_from(admin_client.get("/").text)},
        follow_redirects=False,
    )
    assert thief.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x",
        "A" * 42,
        "A" * 44,
        "!" * 43,
        "A" * 10_000,
        "' OR 1=1 --",
        "%00" * 20,
        "../../etc/passwd",
    ],
)
def test_forged_and_malformed_cookies_are_ignored_safely(
    make_client: Callable[..., TestClient], value: str, audit_codes: Callable[[], list[str]]
) -> None:
    client = make_client()
    plant_cookie(client, SESSION, value)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert set(audit_codes()) <= {"session.rejected"}


def test_random_valid_looking_tokens_do_not_flood_the_audit_log(
    make_client: Callable[..., TestClient], audit_codes: Callable[[], list[str]]
) -> None:
    client = make_client()
    for i in range(25):
        plant_cookie(client, SESSION, f"{i:043d}")
        assert client.get("/", follow_redirects=False).status_code == 303
    assert audit_codes().count("session.rejected") == 1  # throttled per client per minute


def test_session_cap_over_http(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    clock: FakeClock,
) -> None:
    clients = []
    for i in range(6):
        c = make_client(peer=f"10.22.0.{i + 1}")
        login(c, admin.username, admin.password)
        clients.append(c)
        clock.advance(5)
    assert clients[0].get("/", follow_redirects=False).status_code == 303
    assert all(c.get("/", follow_redirects=False).status_code == 200 for c in clients[1:])


def test_disabling_a_user_ends_their_sessions_immediately(
    admin_client: TestClient, sql: Callable[..., Any]
) -> None:
    sql("UPDATE users SET disabled_at = now()")
    assert admin_client.get("/", follow_redirects=False).status_code == 303


def test_session_material_never_appears_in_pages_urls_or_redirects(
    admin_client: TestClient, admin: Account
) -> None:
    token = token_of(admin_client)
    for path in ("/", "/security", "/audit"):
        response = admin_client.get(path)
        assert token not in response.text and token not in str(response.url)
        assert token not in "".join(response.headers.values())
    csrf_value = csrf_from(admin_client.get("/").text)
    assert (
        csrf_value != token
        and hashlib.sha256(token.encode()).hexdigest() not in admin_client.get("/").text
    )


def test_csrf_token_changes_with_every_login(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    c1, c2 = make_client(peer="10.23.0.1"), make_client(peer="10.23.0.2")
    login(c1, admin.username, admin.password)
    login(c2, admin.username, admin.password)
    assert csrf_from(c1.get("/").text) != csrf_from(c2.get("/").text)


def test_session_lifecycle_events_are_all_audited(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    viewer: Account,
    clock: FakeClock,
    audit_codes: Callable[[], list[str]],
) -> None:
    c = make_client(peer="10.24.0.1")
    login(c, "nobody-known", "wrong password at all")  # failure
    login(c, admin.username, admin.password)  # success
    c.post(
        "/logout", data={"csrf_token": csrf_from(c.get("/").text)}, follow_redirects=False
    )  # logout
    login(c, admin.username, admin.password)
    clock.advance(4000)
    c.get("/", follow_redirects=False)  # expiry
    codes = set(audit_codes())
    assert {"auth.login.failure", "auth.login.success", "auth.logout", "session.expired"} <= codes


def test_viewer_role_sessions_get_the_same_protections(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    make_user: Callable[..., Account],
    clock: FakeClock,
) -> None:
    account = make_user("vivian", Role.VIEWER)
    client = make_client(peer="10.25.0.1")
    login(client, account.username, account.password)
    clock.advance(1801)
    assert client.get("/", follow_redirects=False).status_code == 303
