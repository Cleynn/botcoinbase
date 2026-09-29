from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import Role
from tests.conftest import GOOD_PASSWORD, Account, FakeClock, csrf_from

BAD = "definitely the wrong password"
SESSION = "__Host-td_session"


def fail(login: Callable[..., Any], client: TestClient, username: str, times: int) -> list[int]:
    return [login(client, username, BAD).status_code for _ in range(times)]


def normalise(text: str) -> str:
    return re.sub(r'value="[^"]+"', "", text)


def test_pair_limit_blocks_even_the_correct_password_then_expires(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    clock: FakeClock,
) -> None:
    client = make_client(peer="10.5.0.1")
    assert fail(login, client, admin.username, 5) == [401] * 5
    blocked = login(client, admin.username, admin.password)
    assert blocked.status_code == 429
    assert "Too many sign-in attempts" in blocked.text
    assert SESSION not in "".join(blocked.headers.get_list("set-cookie"))
    clock.advance(901)
    assert login(client, admin.username, admin.password).status_code == 303


def test_limit_is_per_client_not_global_for_the_account(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    attacker, owner = make_client(peer="10.5.1.1"), make_client(peer="10.5.1.2")
    fail(login, attacker, admin.username, 5)
    assert login(attacker, admin.username, admin.password).status_code == 429
    assert login(owner, admin.username, admin.password).status_code == 303


def test_pair_limit_does_not_block_other_accounts_from_the_same_client(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    viewer: Account,
) -> None:
    client = make_client(peer="10.5.2.1")
    fail(login, client, admin.username, 5)
    assert login(client, admin.username, admin.password).status_code == 429
    assert login(client, viewer.username, viewer.password).status_code == 303


def test_client_wide_limit_stops_credential_stuffing_across_accounts(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    client = make_client(peer="10.5.3.1")
    for i in range(20):  # 20 different accounts, each below the pair limit
        assert login(client, f"user-{i:03d}", BAD).status_code == 401
    assert login(client, admin.username, admin.password).status_code == 429


def test_account_wide_limit_stops_distributed_guessing(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    for i in range(30):  # 30 different clients, each below the pair and client limits
        assert (
            login(
                make_client(peer=f"10.6.{i // 200}.{i % 200 + 1}"), admin.username, BAD
            ).status_code
            == 401
        )
    fresh = make_client(peer="10.7.0.1")
    assert login(fresh, admin.username, admin.password).status_code == 429


def test_success_resets_the_pair_counter(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    client = make_client(peer="10.5.4.1")
    fail(login, client, admin.username, 4)
    assert login(client, admin.username, admin.password).status_code == 303
    client.cookies.clear()  # a signed-in browser is redirected away from /login
    assert fail(login, client, admin.username, 5) == [401] * 5  # counter restarted at zero
    assert login(client, admin.username, admin.password).status_code == 429


def test_throttling_does_not_reveal_which_accounts_exist(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    real, ghost = make_client(peer="10.5.5.1"), make_client(peer="10.5.5.2")
    fail(login, real, admin.username, 5)
    fail(login, ghost, "no-such-account", 5)
    a = login(real, admin.username, BAD)
    b = login(ghost, "no-such-account", BAD)
    assert (
        (a.status_code, normalise(a.text))
        == (b.status_code, normalise(b.text))
        == (429, normalise(a.text))
    )
    assert a.headers["retry-after"] == b.headers["retry-after"]


def test_blocked_attempts_are_not_recorded_so_they_cannot_extend_the_lock(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    clock: FakeClock,
    sql: Callable[..., Any],
) -> None:
    client = make_client(peer="10.5.6.1")
    for _ in range(5):
        login(client, admin.username, BAD)
        clock.advance(100)  # failures at t=0,100,...,400; now t=500
    rows = sql("SELECT count(*) AS n FROM login_attempts")[0]["n"]
    blocked = login(client, admin.username, admin.password)
    assert blocked.status_code == 429
    assert (
        int(blocked.headers["retry-after"]) == 400
    )  # oldest failure (t=0) leaves the window at t=900
    for _ in range(10):
        assert login(client, admin.username, BAD).status_code == 429
    assert (
        sql("SELECT count(*) AS n FROM login_attempts")[0]["n"] == rows
    )  # hammering added nothing
    clock.advance(401)  # t=901: first failure aged out, four remain
    assert login(client, admin.username, admin.password).status_code == 303


def test_retry_after_is_always_positive(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    client = make_client(peer="10.5.7.1")
    fail(login, client, admin.username, 5)
    assert int(login(client, admin.username, BAD).headers["retry-after"]) >= 1


def test_throttle_and_failure_events_are_audited_without_flooding(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    audit_codes: Callable[[], list[str]],
) -> None:
    client = make_client(peer="10.5.8.1")
    fail(login, client, admin.username, 5)
    for _ in range(10):
        login(client, admin.username, admin.password)  # blocked
    codes = audit_codes()
    assert codes.count("auth.login.failure") == 5
    assert codes.count("auth.login.throttled") == 1  # one per client per minute


def test_stored_throttle_and_audit_data_contain_no_usernames_or_addresses(
    make_client: Callable[..., TestClient], login: Callable[..., Any], sql: Callable[..., Any]
) -> None:
    client = make_client(peer="10.5.9.77")
    login(client, "very-secret-username", "some wrong password")
    blob = repr(sql("SELECT * FROM login_attempts")) + repr(sql("SELECT * FROM audit_events"))
    assert "very-secret-username" not in blob
    assert "10.5.9.77" not in blob and "10.5.9" not in blob
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", r["key_hmac"])
        for r in sql("SELECT key_hmac FROM login_attempts")
    )


def test_failed_logins_are_recorded_with_a_reason_but_the_response_is_generic(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    sql: Callable[..., Any],
) -> None:
    login(make_client(peer="10.5.10.1"), admin.username, BAD)
    login(make_client(peer="10.5.10.2"), "ghost-user", BAD)
    reasons = {
        r["reason_code"]
        for r in sql("SELECT reason_code FROM audit_events WHERE event_code = 'auth.login.failure'")
    }
    assert reasons == {"BAD_PASSWORD", "UNKNOWN_USER"}


# ------------------------------------------------------------------ guessing via other forms
def test_current_password_guessing_via_password_change_is_throttled(
    admin_client: TestClient, admin: Account
) -> None:
    def attempt(current: str) -> int:
        token = csrf_from(admin_client.get("/security").text)
        return int(
            admin_client.post(
                "/security/password",
                data={
                    "csrf_token": token,
                    "current_password": current,
                    "new_password": "a brand new passphrase 99",
                    "confirm_password": "a brand new passphrase 99",
                },
                follow_redirects=False,
            ).status_code
        )

    assert [attempt(BAD) for _ in range(5)] == [400] * 5
    assert attempt(admin.password) == 429  # correct password no longer helps
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_reauth_guessing_is_throttled(admin_client: TestClient, admin: Account) -> None:
    def attempt(password: str) -> int:
        token = csrf_from(admin_client.get("/security").text)
        return int(
            admin_client.post(
                "/security/reauth",
                data={"csrf_token": token, "password": password},
                follow_redirects=False,
            ).status_code
        )

    assert [attempt(BAD) for _ in range(5)] == [400] * 5
    assert attempt(admin.password) == 429


# ------------------------------------------------------------------ client identity
def test_forwarded_for_from_an_untrusted_peer_cannot_evade_the_limit(
    make_client: Callable[..., TestClient], admin: Account
) -> None:
    client = make_client(peer="192.0.2.9")  # not a trusted proxy
    statuses = []
    for i in range(7):
        page = client.get("/login", headers={"x-forwarded-for": f"198.51.100.{i}"})
        statuses.append(
            client.post(
                "/login",
                data={
                    "csrf_token": csrf_from(page.text),
                    "username": admin.username,
                    "password": BAD,
                },
                headers={"x-forwarded-for": f"198.51.100.{i}"},
                follow_redirects=False,
            ).status_code
        )
    assert statuses == [401] * 5 + [429] * 2


def test_forwarded_for_from_a_trusted_proxy_identifies_distinct_clients(
    make_client: Callable[..., TestClient], admin: Account
) -> None:
    proxy = make_client(peer="10.0.0.5")  # trusted proxy address

    def attempt(forwarded: str, password: str) -> int:
        page = proxy.get("/login", headers={"x-forwarded-for": forwarded})
        return int(
            proxy.post(
                "/login",
                data={
                    "csrf_token": csrf_from(page.text),
                    "username": admin.username,
                    "password": password,
                },
                headers={"x-forwarded-for": forwarded},
                follow_redirects=False,
            ).status_code
        )

    for _ in range(5):
        assert attempt("203.0.113.1", BAD) == 401
    assert attempt("203.0.113.1", admin.password) == 429  # that client is blocked...
    assert (
        attempt("203.0.113.2", admin.password) == 303
    )  # ...another client behind the proxy is not


def test_only_the_rightmost_forwarded_address_counts(
    make_client: Callable[..., TestClient], admin: Account
) -> None:
    proxy = make_client(peer="10.0.0.5")

    def attempt(forwarded: str) -> int:
        page = proxy.get("/login")
        return int(
            proxy.post(
                "/login",
                data={
                    "csrf_token": csrf_from(page.text),
                    "username": admin.username,
                    "password": BAD,
                },
                headers={"x-forwarded-for": forwarded},
                follow_redirects=False,
            ).status_code
        )

    codes = [
        attempt(f"{i}.{i}.{i}.{i}, 203.0.113.50") for i in range(1, 8)
    ]  # spoofed left part varies
    assert codes == [401] * 5 + [429] * 2


@pytest.mark.parametrize("bad_forwarded", ["not-an-ip", "", "999.1.1.1", "1.2.3.4:80"])
def test_invalid_forwarded_values_fall_back_to_the_peer(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    bad_forwarded: str,
    admin: Account,
) -> None:
    client = make_client(peer="10.0.0.5")
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_from(page.text),
            "username": admin.username,
            "password": BAD,
        },
        headers={"x-forwarded-for": bad_forwarded},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_viewer_role_accounts_are_throttled_identically(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    make_user: Callable[..., Account],
) -> None:
    make_user("vera", Role.VIEWER)
    client = make_client(peer="10.5.11.1")
    fail(login, client, "vera", 5)
    assert login(client, "vera", GOOD_PASSWORD).status_code == 429
