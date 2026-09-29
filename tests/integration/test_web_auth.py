from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.domain.enums import Role
from tests.conftest import GOOD_PASSWORD, Account, csrf_from, plant_cookie

SESSION = "__Host-td_session"
LOGIN = "__Host-td_login"
NEW_PASSWORD = "a brand new passphrase 99"


def set_cookie(response: Response, name: str) -> str:
    for line in response.headers.get_list("set-cookie"):
        if line.startswith(f"{name}="):
            return line
    raise AssertionError(f"no Set-Cookie for {name}: {response.headers.get_list('set-cookie')}")


def page_token(client: TestClient, path: str = "/security") -> str:
    return csrf_from(client.get(path).text)


def post(
    client: TestClient, path: str, data: dict[str, str], *, token: str | None = None
) -> Response:
    data = {"csrf_token": token or page_token(client), **data}
    response: Response = client.post(path, data=data, follow_redirects=False)
    return response


# ------------------------------------------------------------------ login page and cookies
def test_login_page_has_form_csrf_and_hardened_pre_session_cookie(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert '<form method="post" action="/login"' in response.text
    assert 'type="password"' in response.text
    csrf_from(response.text)
    cookie = set_cookie(response, LOGIN).lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/"):
        assert attribute in cookie
    assert "domain" not in cookie


def test_successful_login_sets_a_hardened_session_cookie(
    client: TestClient, admin: Account, login: Callable[..., Any]
) -> None:
    response = login(client, admin.username, admin.password)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = set_cookie(response, SESSION)
    lowered = cookie.lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/"):
        assert attribute in lowered
    assert "domain" not in lowered
    assert "max-age" not in lowered and "expires" not in lowered  # browser-session cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    assert "max-age=0" in set_cookie(response, LOGIN).lower()  # pre-session cookie is discarded


def test_login_never_honours_a_next_parameter(client: TestClient, admin: Account) -> None:
    page = client.get("/login?next=https://evil.example")
    response = client.post(
        "/login?next=https://evil.example",
        data={
            "csrf_token": csrf_from(page.text),
            "username": admin.username,
            "password": admin.password,
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_username_is_case_insensitive_and_trimmed(
    client: TestClient, admin: Account, login: Callable[..., Any]
) -> None:
    assert login(client, f"  {admin.username.upper()} ", admin.password).status_code == 303


def test_signed_in_users_skip_the_login_page(admin_client: TestClient) -> None:
    response = admin_client.get("/login", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"


# ------------------------------------------------------------------ generic failure
def test_all_login_failures_are_indistinguishable(
    make_client: Callable[..., TestClient],
    admin: Account,
    make_user: Callable[..., Account],
    sql: Callable[..., Any],
    login: Callable[..., Any],
) -> None:
    make_user("disabled-dan", Role.VIEWER)
    sql("UPDATE users SET disabled_at = now() WHERE username = 'disabled-dan'")
    attempts = [
        (admin.username, "wrong password entirely"),
        ("nobody-here", "wrong password entirely"),
        ("disabled-dan", GOOD_PASSWORD),
        ("x", "wrong password entirely"),  # invalid username shape
        ("../../etc/passwd", "wrong password entirely"),
    ]
    seen = []
    for index, (username, password) in enumerate(attempts):
        client = make_client(
            peer=f"10.1.0.{index + 1}"
        )  # separate clients: no throttling interplay
        response = login(client, username, password)
        seen.append((response.status_code, re.sub(r'value="[^"]+"', "", response.text)))
        assert SESSION not in "".join(response.headers.get_list("set-cookie"))
    assert {code for code, _ in seen} == {401}
    assert len({body for _, body in seen}) == 1
    assert "Invalid username or password." in seen[0][1]
    assert not any(name in seen[0][1] for name in (admin.username, "disabled"))


def test_failed_login_does_not_reflect_input(client: TestClient, login: Callable[..., Any]) -> None:
    response = login(client, "<script>alert(1)</script>", "pw-canary-<b>")
    assert response.status_code == 401
    assert "<script>alert(1)</script>" not in response.text and "pw-canary" not in response.text


@pytest.mark.parametrize(
    "data",
    [{}, {"username": "a"}, {"password": "b"}, {"username": "a", "password": "b", "extra": "x"}],
)
def test_malformed_login_forms_get_a_generic_400(client: TestClient, data: dict[str, str]) -> None:
    page = client.get("/login")
    response = client.post(
        "/login", data={"csrf_token": csrf_from(page.text), **data}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "The request was invalid." in response.text


# ------------------------------------------------------------------ no signup / invitation / reset
@pytest.mark.parametrize(
    "path",
    [
        "/signup",
        "/register",
        "/invite",
        "/invitation",
        "/reset-password",
        "/forgot-password",
        "/password/reset",
        "/password-reset",
        "/users",
        "/users/new",
        "/account/create",
        "/admin/users",
    ],
)
def test_no_signup_invitation_or_reset_routes(
    client: TestClient, admin_client: TestClient, path: str
) -> None:
    for c in (client, admin_client):
        assert c.get(path, follow_redirects=False).status_code == 404
        assert c.post(path, data={"csrf_token": "x"}, follow_redirects=False).status_code in (
            403,
            404,
            405,
        )


def test_no_route_can_create_users_or_reset_passwords(app: Any) -> None:
    def walk(routes: list[Any]) -> list[Any]:
        found = []
        for r in routes:
            inner = getattr(r, "original_router", None)
            found.extend(walk(inner.routes) if inner is not None else [r])
        return found

    paths = {getattr(r, "path", "") for r in walk(app.router.routes)}
    assert not {p for p in paths if re.search(r"sign-?up|regist|invit|reset|forgot|users/new", p)}


# ------------------------------------------------------------------ logout
def test_logout_revokes_clears_and_cannot_be_replayed(
    admin_client: TestClient, make_client: Callable[..., TestClient]
) -> None:
    token = admin_client.cookies.get(SESSION)
    assert token
    response = post(admin_client, "/logout", {}, token=page_token(admin_client, "/"))
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "max-age=0" in set_cookie(response, SESSION).lower()
    assert (
        '"cookies"' in response.headers["clear-site-data"]
        and '"storage"' in response.headers["clear-site-data"]
    )
    replay = make_client()
    plant_cookie(replay, SESSION, token)
    assert replay.get("/", follow_redirects=False).status_code == 303


def test_logout_is_post_only(admin_client: TestClient) -> None:
    assert admin_client.get("/logout").status_code == 405
    assert admin_client.get("/").status_code == 200


# ------------------------------------------------------------------ protected pages redirect
@pytest.mark.parametrize("path", ["/", "/security", "/audit", "/partials/status"])
def test_protected_pages_redirect_anonymous_visitors(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_htmx_polling_after_expiry_gets_hx_redirect(client: TestClient) -> None:
    response = client.get(
        "/partials/status", headers={"hx-request": "true"}, follow_redirects=False
    )
    assert response.status_code == 200 and response.headers["hx-redirect"] == "/login"


# ------------------------------------------------------------------ password change
def test_password_change_over_http_rotates_the_cookie_and_ends_other_sessions(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    first, second = make_client(peer="10.2.0.1"), make_client(peer="10.2.0.2")
    login(first, admin.username, admin.password)
    login(second, admin.username, admin.password)
    old_token = first.cookies.get(SESSION)
    response = post(
        first,
        "/security/password",
        {
            "current_password": admin.password,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        },
    )
    assert (
        response.status_code == 303
        and response.headers["location"] == "/security?msg=password_changed"
    )
    new_token = set_cookie(response, SESSION).split(";")[0].split("=", 1)[1]
    assert new_token != old_token
    assert second.get("/", follow_redirects=False).status_code == 303  # other session ended
    plant_cookie(first, SESSION, old_token)  # the pre-change cookie is dead
    assert first.get("/", follow_redirects=False).status_code == 303
    plant_cookie(first, SESSION, new_token)
    assert "Password changed" in first.get("/security?msg=password_changed").text


@pytest.mark.parametrize(
    ("data", "fragment", "status"),
    [
        (
            {
                "current_password": "wrong password 123",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            "current password is incorrect",
            400,
        ),
        (
            {
                "current_password": GOOD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": "does not match 99",
            },
            "do not match",
            400,
        ),
        (
            {
                "current_password": GOOD_PASSWORD,
                "new_password": "short",
                "confirm_password": "short",
            },
            "too short",
            400,
        ),
        (
            {
                "current_password": GOOD_PASSWORD,
                "new_password": GOOD_PASSWORD,
                "confirm_password": GOOD_PASSWORD,
            },
            "must differ",
            400,
        ),
    ],
)
def test_password_change_errors_are_specific_and_leave_the_session(
    admin_client: TestClient, data: dict[str, str], fragment: str, status: int
) -> None:
    response = post(admin_client, "/security/password", data)
    assert response.status_code == status
    assert fragment in response.text
    assert admin_client.get("/", follow_redirects=False).status_code == 200


# ------------------------------------------------------------------ sessions and reauthentication
def test_security_page_lists_sessions_and_revocation_works(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    main, other = make_client(peer="10.3.0.1"), make_client(peer="10.3.0.2")
    login(main, admin.username, admin.password)
    login(other, admin.username, admin.password)
    page = main.get("/security").text
    assert page.count("This session") == 1 and page.count('action="/security/sessions/revoke"') == 1
    session_id = re.search(r'name="session_id" value="([0-9a-f-]{36})"', page)
    assert session_id
    response = post(main, "/security/sessions/revoke", {"session_id": session_id.group(1)})
    assert response.status_code == 303
    assert other.get("/", follow_redirects=False).status_code == 303
    assert main.get("/", follow_redirects=False).status_code == 200


def test_sign_out_all_other_sessions(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    main = make_client(peer="10.3.1.1")
    others = [make_client(peer=f"10.3.1.{i}") for i in (2, 3)]
    for c in (main, *others):
        login(c, admin.username, admin.password)
    assert post(main, "/security/sessions/revoke-others", {}).status_code == 303
    assert all(c.get("/", follow_redirects=False).status_code == 303 for c in others)
    assert main.get("/", follow_redirects=False).status_code == 200


def test_reauth_form_reports_state(admin_client: TestClient, admin: Account) -> None:
    assert "not confirmed" in admin_client.get("/security").text
    bad = post(admin_client, "/security/reauth", {"password": "wrong password 123"})
    assert bad.status_code == 400 and "incorrect" in bad.text
    ok = post(admin_client, "/security/reauth", {"password": admin.password})
    assert ok.status_code == 303 and ok.headers["location"] == "/security?msg=reauth_ok"
    page = admin_client.get("/security?msg=reauth_ok").text
    assert "Password confirmed" in page and "<strong>confirmed</strong>" in page


def test_admin_revokes_a_users_sessions_only_with_phrase_and_single_use_reauth(
    admin_client: TestClient, viewer_client: TestClient, admin: Account, viewer: Account
) -> None:
    def revoke(phrase: str) -> Response:
        return post(
            admin_client,
            "/security/users/revoke-sessions",
            {"user_id": str(viewer.user.id), "confirmation": phrase},
        )

    phrase = f"REVOKE SESSIONS FOR {viewer.username.upper()}"
    assert "Confirm your password first" in revoke(phrase).text  # no reauth yet
    post(admin_client, "/security/reauth", {"password": admin.password})
    wrong = revoke("revoke sessions for victor")  # exact match required
    assert wrong.status_code == 400 and "phrase did not match" in wrong.text
    assert viewer_client.get("/", follow_redirects=False).status_code == 200  # nothing happened
    ok = revoke(phrase)  # the wrong phrase did not consume the reauthentication
    assert ok.status_code == 303 and ok.headers["location"] == "/security?msg=user_sessions_revoked"
    assert viewer_client.get("/", follow_redirects=False).status_code == 303
    again = revoke(phrase)  # reauthentication was single-use
    assert again.status_code == 400 and "Confirm your password first" in again.text


def test_unknown_target_user_is_a_404_without_details(
    admin_client: TestClient, admin: Account
) -> None:
    post(admin_client, "/security/reauth", {"password": admin.password})
    response = post(
        admin_client,
        "/security/users/revoke-sessions",
        {"user_id": "00000000-0000-4000-8000-000000000000", "confirmation": "x"},
    )
    assert response.status_code == 404
