from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.errors import MAX_BODY_BYTES
from tests.conftest import UNSAFE_METHODS, Account, csrf_from, walk_routes

SESSION = "__Host-td_session"
REJECTED = "could not be verified"


def unsafe_paths(app: Any) -> list[str]:
    return sorted({r.path for r in walk_routes(app) if set(r.methods) & UNSAFE_METHODS})


def test_every_state_changing_route_is_covered(app: Any) -> None:
    assert unsafe_paths(app) == [
        "/bot/{slug}/confirm",
        "/bot/{slug}/reauth",
        "/login",
        "/logout",
        "/pairs/candidates",
        "/pairs/{pair_id}/deactivate",
        "/pairs/{pair_id}/pause",
        "/pairs/{pair_id}/validate",
        "/pairs/{pair_id}/{action}/confirm",
        "/pairs/{pair_id}/{action}/reauth",
        "/review/disable/confirm",
        "/review/disable/reauth",
        "/review/enable/confirm",
        "/review/enable/reauth",
        "/review/packages/create/confirm",
        "/review/packages/create/reauth",
        "/review/packages/{package_id}/download",
        "/review/packages/{package_id}/verify",
        "/review/proposals/import-disable",
        "/review/proposals/import-enable/confirm",
        "/review/proposals/import-enable/reauth",
        "/review/proposals/import/confirm",
        "/review/proposals/import/reauth",
        "/review/proposals/{proposal_id}/attest/{kind}/confirm",
        "/review/proposals/{proposal_id}/attest/{kind}/reauth",
        "/review/proposals/{proposal_id}/change-request/confirm",
        "/review/proposals/{proposal_id}/change-request/reauth",
        "/review/proposals/{proposal_id}/close",
        "/review/proposals/{proposal_id}/review",
        "/security/password",
        "/security/reauth",
        "/security/sessions/revoke",
        "/security/sessions/revoke-others",
        "/security/users/revoke-sessions",
    ]


def test_every_state_changing_route_rejects_a_missing_token(
    app: Any, admin_client: TestClient, client: TestClient
) -> None:
    for path in unsafe_paths(app):
        actor = client if path == "/login" else admin_client
        response = actor.post(path, data={"anything": "x"}, follow_redirects=False)
        assert response.status_code == 403 and REJECTED in response.text, path


def test_every_state_changing_route_rejects_a_wrong_token(
    app: Any, admin_client: TestClient, client: TestClient
) -> None:
    for path in unsafe_paths(app):
        actor = client if path == "/login" else admin_client
        if path == "/login":
            actor.get("/login")  # obtain the pre-session cookie
        response = actor.post(path, data={"csrf_token": "x" * 43}, follow_redirects=False)
        assert response.status_code == 403, path


def test_a_rejected_request_changes_nothing(
    admin_client: TestClient, admin: Account, sql: Callable[..., Any]
) -> None:
    before = sql("SELECT password_hash FROM users")[0]["password_hash"]
    response = admin_client.post(
        "/security/password",
        data={
            "csrf_token": "bad",
            "current_password": admin.password,
            "new_password": "a brand new passphrase 99",
            "confirm_password": "a brand new passphrase 99",
        },
    )
    assert response.status_code == 403
    assert sql("SELECT password_hash FROM users")[0]["password_hash"] == before
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_tokens_are_bound_to_the_session(
    make_client: Callable[..., TestClient],
    login: Callable[..., Any],
    admin: Account,
    viewer: Account,
) -> None:
    a, b = make_client(peer="10.30.0.1"), make_client(peer="10.30.0.2")
    login(a, admin.username, admin.password)
    login(b, viewer.username, viewer.password)
    other = csrf_from(a.get("/").text)
    assert b.post("/logout", data={"csrf_token": other}, follow_redirects=False).status_code == 403
    assert b.get("/", follow_redirects=False).status_code == 200  # still signed in


def test_tokens_die_with_the_session(
    admin_client: TestClient, admin: Account, login: Callable[..., Any]
) -> None:
    old = csrf_from(admin_client.get("/").text)
    admin_client.post("/logout", data={"csrf_token": old}, follow_redirects=False)
    admin_client.cookies.clear()
    login(admin_client, admin.username, admin.password)
    assert (
        admin_client.post("/logout", data={"csrf_token": old}, follow_redirects=False).status_code
        == 403
    )


def test_login_and_session_tokens_are_not_interchangeable(
    admin_client: TestClient, client: TestClient
) -> None:
    session_token = csrf_from(admin_client.get("/").text)
    login_page = client.get("/login")
    login_token = csrf_from(login_page.text)
    assert (
        admin_client.post(
            "/login", data={"csrf_token": session_token, "username": "a", "password": "b"}
        ).status_code
        == 403
    )
    admin_client.cookies.set("__Host-td_login", client.cookies.get("__Host-td_login") or "")
    assert (
        admin_client.post(
            "/logout", data={"csrf_token": login_token}, follow_redirects=False
        ).status_code
        == 403
    )


def test_login_form_is_csrf_protected(client: TestClient, admin: Account) -> None:
    assert (
        client.post(
            "/login", data={"username": admin.username, "password": admin.password}
        ).status_code
        == 403
    )
    other = TestClient(
        client.app,
        base_url="https://testserver",
        client=("10.30.1.1", 1),
        headers={"origin": "https://testserver"},
    )
    foreign_token = csrf_from(other.get("/login").text)  # token minted for a different browser
    client.get("/login")
    response = client.post(
        "/login",
        data={"csrf_token": foreign_token, "username": admin.username, "password": admin.password},
    )
    assert response.status_code == 403
    assert client.cookies.get(SESSION) is None


def test_login_csrf_needs_the_pre_session_cookie(client: TestClient, admin: Account) -> None:
    token = csrf_from(client.get("/login").text)
    client.cookies.clear()
    assert (
        client.post(
            "/login",
            data={"csrf_token": token, "username": admin.username, "password": admin.password},
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ Origin / Fetch Metadata
@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"origin": "https://testserver"}, True),
        ({"origin": "https://evil.example"}, False),
        ({"origin": "https://testserver.evil.example"}, False),
        ({"origin": "null"}, False),
        (
            {"origin": "http://testserver"},
            True,
        ),  # http origins are tolerated outside production only
    ],
)
def test_origin_header_is_enforced(
    admin_client: TestClient, headers: dict[str, str], allowed: bool
) -> None:
    token = csrf_from(admin_client.get("/").text)
    response = admin_client.post(
        "/security/sessions/revoke-others",
        data={"csrf_token": token},
        headers=headers,
        follow_redirects=False,
    )
    assert (response.status_code == 303) is allowed
    if not allowed:
        assert response.status_code == 403


def test_missing_origin_fails_closed_unless_fetch_metadata_says_same_origin(
    make_client: Callable[..., TestClient], login: Callable[..., Any], admin: Account
) -> None:
    client = make_client(origin=None)
    login_page = client.get("/login")
    body = {
        "csrf_token": csrf_from(login_page.text),
        "username": admin.username,
        "password": admin.password,
    }
    assert (
        client.post("/login", data=body, follow_redirects=False).status_code == 403
    )  # no signal at all
    assert (
        client.post(
            "/login", data=body, headers={"sec-fetch-site": "cross-site"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/login", data=body, headers={"sec-fetch-site": "same-site"}, follow_redirects=False
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/login", data=body, headers={"sec-fetch-site": "same-origin"}, follow_redirects=False
        ).status_code
        == 303
    )


def test_a_valid_token_is_not_enough_from_a_foreign_origin(admin_client: TestClient) -> None:
    token = csrf_from(admin_client.get("/").text)
    response = admin_client.post(
        "/logout",
        data={"csrf_token": token},
        headers={"origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_token_header_is_accepted_and_form_field_is_optional_then(admin_client: TestClient) -> None:
    token = csrf_from(admin_client.get("/").text)
    response = admin_client.post(
        "/security/sessions/revoke-others",
        headers={"x-csrf-token": token},
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        admin_client.post(
            "/security/sessions/revoke-others",
            headers={"x-csrf-token": "wrong"},
            follow_redirects=False,
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    ("content_type", "body"),
    [("application/json", '{"csrf_token":"x"}'), ("text/plain", "csrf_token=x")],
)
def test_non_form_bodies_cannot_carry_a_token(
    admin_client: TestClient, content_type: str, body: str
) -> None:
    token = csrf_from(admin_client.get("/").text)
    response = admin_client.post(
        "/logout",
        content=body.replace("x", token),
        headers={"content-type": content_type},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_cookieless_posts_are_rejected_before_authentication_is_considered(
    client: TestClient,
) -> None:
    response = client.post("/logout", data={"csrf_token": "x" * 43}, follow_redirects=False)
    assert response.status_code == 403  # not a redirect to /login


def test_safe_methods_never_change_state(admin_client: TestClient, sql: Callable[..., Any]) -> None:
    def snapshot() -> tuple[Any, Any, Any]:
        return (
            sql("SELECT count(*) AS n FROM sessions WHERE revoked_at IS NOT NULL")[0]["n"],
            sql("SELECT password_hash FROM users")[0]["password_hash"],
            sql("SELECT count(*) AS n FROM audit_events")[0]["n"],
        )

    before = snapshot()
    for path in (
        "/logout",
        "/security/password",
        "/security/reauth",
        "/security/sessions/revoke",
        "/security/sessions/revoke-others",
        "/security/users/revoke-sessions",
    ):
        assert admin_client.get(path).status_code == 405
        assert admin_client.get(path, headers={"x-http-method-override": "POST"}).status_code == 405
    assert snapshot() == before


def test_rejections_are_audited_without_flooding(
    admin_client: TestClient, audit_codes: Callable[[], list[str]]
) -> None:
    for _ in range(8):
        admin_client.post("/logout", data={"csrf_token": "bad"}, follow_redirects=False)
    assert audit_codes().count("csrf.rejected") == 1


def test_no_cors_headers_are_ever_emitted(admin_client: TestClient) -> None:
    for method in ("GET", "OPTIONS"):
        response = admin_client.request(method, "/", headers={"origin": "https://attacker.example"})
        assert not any(name.lower().startswith("access-control-") for name in response.headers)


# ------------------------------------------------------------------ body size
def test_oversized_bodies_are_refused_before_parsing(
    admin_client: TestClient, client: TestClient
) -> None:
    big = "x" * (MAX_BODY_BYTES + 1)
    declared = client.post(
        "/login", content=big, headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert declared.status_code == 413

    def chunks() -> Any:
        for _ in range(4):
            yield b"a=" + b"x" * (MAX_BODY_BYTES // 2)

    streamed = client.post(
        "/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert streamed.status_code == 413


def test_normal_sized_forms_are_unaffected(admin_client: TestClient) -> None:
    token = csrf_from(admin_client.get("/").text)
    assert (
        admin_client.post(
            "/security/sessions/revoke-others", data={"csrf_token": token}, follow_redirects=False
        ).status_code
        == 303
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
