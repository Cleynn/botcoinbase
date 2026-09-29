from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import Account, FakeClock

SESSION = "__Host-td_session"


def test_dashboard_shows_banner_and_only_known_values(
    admin_client: TestClient, admin: Account
) -> None:
    body = admin_client.get("/").text
    assert "MODE: BACKTEST" in body and "LIVE TRADING: BLOCKED" in body
    assert f"{admin.username} (ADMIN)" in body
    known = body.split("Known values")[1].split("Not available yet")[0]
    assert (
        "BACKTEST" in known
        and "BLOCKED" in known
        and "Not available" not in known
        and "Unknown" not in known
    )
    unavailable = body.split("Not available yet")[1]
    for label in (
        "Bot state",
        "Active pair",
        "Protected reserve",
        "Deployed capital",
        "Data freshness",
        "Reconciliation status",
        "Circuit breaker state",
        "Kill switch state",
        "Alerts",
    ):
        assert label in unavailable
    assert "Not available" in unavailable and "Unknown" in unavailable
    assert not re.search(r"\d+(\.\d+)?\s*USDC", body)  # no invented balances


def test_grafana_is_linked_never_embedded(admin_client: TestClient) -> None:
    body = admin_client.get("/").text
    assert 'href="https://grafana.tradingdots.onthewall.ovh/"' in body
    assert 'rel="noopener noreferrer"' in body
    lowered = body.lower()
    assert not any(tag in lowered for tag in ("<iframe", "<embed", "<object", "<frame"))


def test_no_state_changing_control_on_the_dashboard(admin_client: TestClient) -> None:
    forms = re.findall(r'<form[^>]*action="([^"]+)"', admin_client.get("/").text)
    assert forms == [
        "/logout"
    ]  # sign-out is the only form; nothing touches bot/pair/exchange/config


def test_all_forms_in_the_application_are_on_the_allowlist(admin_client: TestClient) -> None:
    allowed = {
        "/logout",
        "/security/sessions/revoke",
        "/security/sessions/revoke-others",
        "/security/password",
        "/security/reauth",
        "/security/users/revoke-sessions",
        "/audit",
        "/login",
    }
    for path in ("/", "/security", "/audit"):
        actions = set(re.findall(r'<form[^>]*action="([^"]+)"', admin_client.get(path).text))
        assert actions <= allowed, (path, actions - allowed)


def test_navigation_differs_by_role(admin_client: TestClient, viewer_client: TestClient) -> None:
    admin_nav = admin_client.get("/").text
    viewer_nav = viewer_client.get("/").text
    assert 'href="/audit"' in admin_nav and 'href="/audit"' not in viewer_nav
    for nav in (admin_nav, viewer_nav):
        assert 'href="/security"' in nav
        assert 'href="/pairs"' in nav  # Phase 4: a real page for every signed-in user
        for later in ("Bot", "Reports", "LLM Review"):
            assert re.search(rf'aria-disabled="true">{later} ', nav)
        assert 'href="/bot"' not in nav


def test_pages_work_without_javascript_and_load_no_external_assets(
    admin_client: TestClient, client: TestClient
) -> None:
    pages = [
        (admin_client, "/"),
        (admin_client, "/security"),
        (admin_client, "/audit"),
        (client, "/login"),
    ]
    for c, path in pages:
        body = c.get(path).text
        assert not re.search(r"<script(?![^>]*\ssrc=)", body), f"inline script on {path}"
        assert not re.search(r'(?:src|href)="(?:https?:)?//(?!grafana\.)', body), (
            f"external asset on {path}"
        )
        assert 'src="/static/' in body
        assert "<noscript" not in body  # nothing depends on scripting to be usable
    for c, path in ((admin_client, "/security"), (client, "/login")):  # every input has a label
        body = c.get(path).text
        for field_id in re.findall(r'<input id="([^"]+)"', body):
            assert f'<label for="{field_id}"' in body, field_id


def test_pages_are_accessible_and_mobile_friendly(admin_client: TestClient) -> None:
    for path in ("/", "/security", "/audit"):
        body = admin_client.get(path).text
        assert '<html lang="en">' in body and 'name="viewport"' in body
        assert 'class="skip-link"' in body and "<main" in body and 'aria-label="Main"' in body
        assert body.count("<h1") == 1
        assert 'aria-current="page"' in body or path == "/"


def test_partial_status_is_a_fragment_and_does_not_extend_the_session(
    admin_client: TestClient, clock: FakeClock, sql: Callable[..., Any]
) -> None:
    before = sql("SELECT last_seen_at FROM sessions")[0]["last_seen_at"]
    clock.advance(600)
    response = admin_client.get("/partials/status")
    assert response.status_code == 200 and "<html" not in response.text and "UTC" in response.text
    assert sql("SELECT last_seen_at FROM sessions")[0]["last_seen_at"] == before


# ------------------------------------------------------------------ security page
def test_security_page_for_admin_includes_user_management(
    admin_client: TestClient, viewer: Account
) -> None:
    body = admin_client.get("/security").text
    assert "Users and session revocation" in body
    assert f"REVOKE SESSIONS FOR {viewer.username.upper()}" in body
    assert 'action="/security/users/revoke-sessions"' in body


def test_security_page_for_viewer_has_no_admin_section(viewer_client: TestClient) -> None:
    body = viewer_client.get("/security").text
    assert "Users and session revocation" not in body
    assert "/security/users/revoke-sessions" not in body
    assert "Change password" in body and "Confirm your password" in body


def test_security_page_never_shows_tokens_or_hashes(
    admin_client: TestClient, sql: Callable[..., Any]
) -> None:
    body = admin_client.get("/security").text
    token = admin_client.cookies.get(SESSION) or ""
    stored = sql("SELECT token_hash FROM sessions")[0]["token_hash"]
    assert token not in body and stored not in body and "$argon2" not in body


def test_unknown_flash_codes_are_ignored(admin_client: TestClient) -> None:
    body = admin_client.get("/security?msg=<script>alert(1)</script>").text
    assert "<script>alert" not in body


# ------------------------------------------------------------------ audit page
def test_audit_page_lists_events_and_verifies_the_chain(admin_client: TestClient) -> None:
    body = admin_client.get("/audit").text
    assert "auth.login.success" in body
    assert "Audit chain verified (1 events)" in body
    assert "aria-label" not in body.split("<table")[1].split("</table>")[0]


def test_audit_filter_and_pagination(
    admin_client: TestClient,
    viewer_client: TestClient,
    login: Callable[..., Any],
    make_client: Callable[..., TestClient],
    admin: Account,
) -> None:
    for i in range(3):  # generate failures
        login(make_client(peer=f"10.9.0.{i + 1}"), "nobody", "wrong password 123")
    filtered = admin_client.get("/audit?code=auth.login.failure").text
    assert filtered.count("auth.login.failure") >= 3 + 1  # rows plus the <option> label
    assert "<td>auth.login.success</td>" not in filtered
    page = admin_client.get("/audit").text
    seqs = [int(s) for s in re.findall(r"<tr>\s*<td>(\d+)</td>", page)]
    assert seqs == sorted(seqs, reverse=True)
    older = admin_client.get(f"/audit?before={seqs[1]}").text
    assert f"<td>{seqs[1]}</td>" not in older and f"<td>{seqs[-1]}</td>" in older


def test_audit_rejects_unknown_filter_values_without_reflecting_them(
    admin_client: TestClient,
) -> None:
    for value in ("<script>alert(1)</script>", "auth.login.nothing", "x" * 80):
        response = admin_client.get("/audit", params={"code": value})
        assert response.status_code in (400, 422) or "The request was invalid." in response.text
        assert value not in response.text
    assert admin_client.get("/audit?before=abc").status_code == 400
    assert admin_client.get("/audit?before=-5").status_code == 400
