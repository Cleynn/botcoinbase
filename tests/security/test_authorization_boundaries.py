from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.api.dependencies import public, require_permission
from app.auth.authorization import declared_access
from app.domain.models import ClientIdentity
from app.domain.permissions import Permission
from tests.conftest import UNSAFE_METHODS, Account, csrf_from, walk_routes

# The complete, reviewed access map. A new route without an entry here fails the inventory test.
EXPECTED: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "public",
    ("GET", "/login"): "public",
    ("POST", "/login"): "public",
    ("POST", "/logout"): "logout",
    ("GET", "/"): "view_dashboard",
    ("GET", "/partials/status"): "view_dashboard",
    ("GET", "/security"): "view_own_security",
    ("POST", "/security/password"): "change_own_password",
    ("POST", "/security/reauth"): "reauthenticate",
    ("POST", "/security/sessions/revoke"): "revoke_own_sessions",
    ("POST", "/security/sessions/revoke-others"): "revoke_own_sessions",
    ("POST", "/security/users/revoke-sessions"): "revoke_user_sessions",
    ("GET", "/audit"): "view_audit",
    ("GET", "/reports"): "view_reports",
    ("GET", "/reports/{report_id}"): "view_reports",
    ("GET", "/reports/{report_id}/json"): "view_reports",
    ("GET", "/reports/{report_id}/md"): "view_reports",
    ("GET", "/pairs"): "view_pairs",
    ("GET", "/pairs/products"): "view_pairs",
    ("GET", "/pairs/{pair_id}"): "view_pairs",
    ("POST", "/pairs/candidates"): "manage_pairs",
    ("POST", "/pairs/{pair_id}/validate"): "manage_pairs",
    ("POST", "/pairs/{pair_id}/pause"): "manage_pairs",
    ("POST", "/pairs/{pair_id}/deactivate"): "manage_pairs",
    ("GET", "/pairs/{pair_id}/{action}/request"): "manage_pairs",
    ("POST", "/pairs/{pair_id}/{action}/reauth"): "manage_pairs",
    ("POST", "/pairs/{pair_id}/{action}/confirm"): "manage_pairs",
    ("GET", "/review"): "manage_review_packages",
    ("GET", "/review/enable/request"): "manage_review_packages",
    ("POST", "/review/enable/reauth"): "manage_review_packages",
    ("POST", "/review/enable/confirm"): "manage_review_packages",
    ("GET", "/review/disable/request"): "manage_review_packages",
    ("POST", "/review/disable/reauth"): "manage_review_packages",
    ("POST", "/review/disable/confirm"): "manage_review_packages",
    ("GET", "/review/packages/create/request"): "manage_review_packages",
    ("POST", "/review/packages/create/reauth"): "manage_review_packages",
    ("POST", "/review/packages/create/confirm"): "manage_review_packages",
    ("GET", "/review/packages/{package_id}"): "manage_review_packages",
    ("POST", "/review/packages/{package_id}/verify"): "manage_review_packages",
    ("POST", "/review/packages/{package_id}/download"): "manage_review_packages",
    ("GET", "/bot"): "view_bot",
    ("GET", "/bot/{slug}/request"): "manage_bot",
    ("POST", "/bot/{slug}/reauth"): "manage_bot",
    ("POST", "/bot/{slug}/confirm"): "manage_bot",
    ("GET", "/review/proposals"): "manage_proposals",
    ("GET", "/review/proposals/import-enable/request"): "manage_proposals",
    ("POST", "/review/proposals/import-enable/reauth"): "manage_proposals",
    ("POST", "/review/proposals/import-enable/confirm"): "manage_proposals",
    ("POST", "/review/proposals/import-disable"): "manage_proposals",
    ("GET", "/review/proposals/import/request"): "manage_proposals",
    ("POST", "/review/proposals/import/reauth"): "manage_proposals",
    ("POST", "/review/proposals/import/confirm"): "manage_proposals",
    ("GET", "/review/proposals/{proposal_id}"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/review"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/close"): "manage_proposals",
    ("GET", "/review/proposals/{proposal_id}/change-request/request"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/change-request/reauth"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/change-request/confirm"): "manage_proposals",
    ("GET", "/review/proposals/{proposal_id}/attest/{kind}/request"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/attest/{kind}/reauth"): "manage_proposals",
    ("POST", "/review/proposals/{proposal_id}/attest/{kind}/confirm"): "manage_proposals",
}
PUBLIC = {key for key, value in EXPECTED.items() if value == "public"}
ADMIN_ONLY = {"/audit", "/security/users/revoke-sessions"}


def actual_map(app: Any) -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for route in walk_routes(app):
        declaration = declared_access(route)
        label = (
            "UNDECLARED"
            if declaration is None
            else ("public" if declaration.public else str(declaration.permission))
        )
        for method in route.methods - {"HEAD", "OPTIONS"}:
            found[(method, route.path)] = label
    return found


def test_route_inventory_matches_the_reviewed_access_map(app: Any) -> None:
    assert actual_map(app) == EXPECTED


def test_every_route_has_a_declaration(app: Any) -> None:
    assert "UNDECLARED" not in actual_map(app).values()


def test_only_login_and_health_are_reachable_without_a_session(app: Any) -> None:
    assert PUBLIC == {("GET", "/healthz"), ("GET", "/login"), ("POST", "/login")}


def test_anonymous_visitors_cannot_use_any_protected_route(app: Any, client: TestClient) -> None:
    for (method, template), level in EXPECTED.items():
        if level == "public":
            continue
        path = template.format(
            pair_id=uuid4(),
            report_id=uuid4(),
            package_id=uuid4(),
            proposal_id=uuid4(),
            kind="implemented",
            action="archive",
            slug="pause",
        )
        if method == "GET":
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/login", path
        else:
            response = client.post(path, data={"csrf_token": "x" * 43}, follow_redirects=False)
            assert response.status_code == 403, path  # refused at the CSRF gate, before any handler


def test_viewer_is_refused_on_admin_routes_and_the_denial_is_audited(
    viewer_client: TestClient, viewer: Account, sql: Callable[..., Any]
) -> None:
    assert viewer_client.get("/audit").status_code == 403
    token = csrf_from(viewer_client.get("/security").text)
    response = viewer_client.post(
        "/security/users/revoke-sessions",
        data={"csrf_token": token, "user_id": str(uuid4()), "confirmation": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    denials = sql("SELECT * FROM audit_events WHERE event_code = 'authz.denied' ORDER BY seq")
    assert {d["target_id"] for d in denials} == {"/audit", "/security/users/revoke-sessions"}
    assert all(d["result"] == "DENIED" and d["actor_role"] == "VIEWER" for d in denials)
    assert all(str(viewer.user.id) == str(d["actor_user_id"]) for d in denials)


def test_denial_pages_reveal_nothing_about_the_resource(viewer_client: TestClient) -> None:
    body = viewer_client.get("/audit").text
    assert "You do not have permission" in body
    assert "auth.login" not in body and "Audit chain" not in body


def test_admin_can_use_admin_routes(admin_client: TestClient) -> None:
    assert admin_client.get("/audit").status_code == 200
    assert admin_client.get("/security").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "//audit",
        "/audit/",
        "/AUDIT",
        "/%61udit",
        "/audit%2f",
        "/audit;x",
        "/./audit",
        "/static/../audit",
    ],
)
def test_path_tricks_do_not_reach_admin_pages_as_a_viewer(
    viewer_client: TestClient, path: str
) -> None:
    response = viewer_client.get(path, follow_redirects=True)
    assert response.status_code != 200 or "Audit events, newest first" not in response.text


def test_static_mount_does_not_expose_application_files(client: TestClient) -> None:
    for path in (
        "/static/../app/config.py",
        "/static/templates/base.html",
        "/static/%2e%2e/config/base.yaml",
        "/static/..%2f..%2fpyproject.toml",
    ):
        assert client.get(path).status_code == 404


def test_a_route_without_a_declaration_fails_closed(app: Any, admin_client: TestClient) -> None:
    calls: list[str] = []

    def undeclared() -> dict[str, str]:  # pragma: no cover - must never run
        calls.append("ran")
        return {"secret": "data"}

    app.add_api_route("/undeclared", undeclared, methods=["GET"])

    response = admin_client.get("/undeclared")
    assert response.status_code == 403 and "secret" not in response.text
    assert calls == []


def test_conflicting_declarations_fail_closed(app: Any, admin_client: TestClient) -> None:
    calls: list[str] = []

    def conflicted() -> dict[str, str]:  # pragma: no cover - must never run
        calls.append("ran")
        return {}

    app.add_api_route(
        "/conflicted",
        conflicted,
        methods=["GET"],
        dependencies=[Depends(public), Depends(require_permission(Permission.VIEW_AUDIT))],
    )

    assert admin_client.get("/conflicted").status_code == 403
    assert calls == []


def test_a_new_unsafe_route_is_csrf_protected_automatically(
    app: Any, admin_client: TestClient
) -> None:
    calls: list[str] = []

    def future() -> dict[str, str]:
        calls.append("ran")
        return {}

    app.add_api_route(
        "/future-action",
        future,
        methods=["POST"],
        dependencies=[Depends(require_permission(Permission.VIEW_DASHBOARD))],
    )

    assert admin_client.post("/future-action", data={"csrf_token": "bad"}).status_code == 403
    assert calls == []
    token = csrf_from(admin_client.get("/").text)
    assert admin_client.post("/future-action", data={"csrf_token": token}).status_code == 200
    assert calls == ["ran"]


# ------------------------------------------------------------------ object-level access (IDOR)
def test_a_viewer_cannot_revoke_someone_elses_session(
    admin_client: TestClient, viewer_client: TestClient, sql: Callable[..., Any], admin: Account
) -> None:
    admin_session = str(
        sql("SELECT id FROM sessions WHERE user_id = %s", (admin.user.id,))[0]["id"]
    )
    token = csrf_from(viewer_client.get("/security").text)
    response = viewer_client.post(
        "/security/sessions/revoke",
        data={"csrf_token": token, "session_id": admin_session},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_a_session_cannot_be_revoked_through_the_single_session_form_for_itself_or_unknown_ids(
    admin_client: TestClient, sql: Callable[..., Any], admin: Account
) -> None:
    own = str(sql("SELECT id FROM sessions WHERE user_id = %s", (admin.user.id,))[0]["id"])
    for session_id in (own, str(uuid4())):
        token = csrf_from(admin_client.get("/security").text)
        response = admin_client.post(
            "/security/sessions/revoke",
            data={"csrf_token": token, "session_id": session_id},
            follow_redirects=False,
        )
        assert response.status_code == 400
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_a_viewers_security_page_only_lists_their_own_sessions(
    admin_client: TestClient,
    viewer_client: TestClient,
    sql: Callable[..., Any],
    admin: Account,
    viewer: Account,
) -> None:
    body = viewer_client.get("/security").text
    admin_session = str(
        sql("SELECT id FROM sessions WHERE user_id = %s", (admin.user.id,))[0]["id"]
    )
    assert admin_session not in body and admin.username not in body.split("Your active sessions")[1]
    assert body.count("This session") == 1


def test_privilege_escalation_through_extra_form_fields_is_rejected(
    viewer_client: TestClient, viewer: Account, sql: Callable[..., Any]
) -> None:
    token = csrf_from(viewer_client.get("/security").text)
    response = viewer_client.post(
        "/security/password",
        data={
            "csrf_token": token,
            "current_password": viewer.password,
            "new_password": "a brand new passphrase 99",
            "confirm_password": "a brand new passphrase 99",
            "role": "ADMIN",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert (
        sql("SELECT role FROM users WHERE username = %s", (viewer.username,))[0]["role"] == "VIEWER"
    )


def test_service_layer_enforces_permissions_independently_of_the_route(
    services: Any, viewer: Account
) -> None:
    client = ClientIdentity.from_key("d" * 64)
    result = services.auth.login(viewer.username, viewer.password, client, "rid")
    ctx = services.auth.validate(result.token, touch=False, client=client, request_id="rid")
    with pytest.raises(PermissionError):
        services.auth.admin_revoke_user_sessions(
            ctx, uuid4(), "REVOKE SESSIONS FOR X", client, "rid"
        )


def test_method_override_headers_are_ignored(admin_client: TestClient) -> None:
    response = admin_client.get(
        "/logout", headers={"x-http-method-override": "POST", "x-method-override": "POST"}
    )
    assert response.status_code == 405
    assert admin_client.get("/", follow_redirects=False).status_code == 200


def test_no_page_exposes_a_bot_pair_exchange_or_config_control(admin_client: TestClient) -> None:
    for path in ("/", "/security", "/audit"):
        body = admin_client.get(path).text.lower()
        assert not re.search(
            r'action="/(bot|pairs?|exchange|orders?|config|kill|mode|reports?|review|proposals?)',
            body,
        )
        for word in (
            "place order",
            "cancel order",
            "enable live",
            "activate pair",
            "resume bot",
            "pause bot",
        ):
            assert word not in body
    # Phase 8 added /bot (status for everyone, four confirmed controls for ADMIN); it has no order,
    # exchange or config control. The rest do not exist.
    bot = admin_client.get("/bot").text.lower()
    assert re.findall(r'<form[^>]*action="([^"]+)"', bot) == ["/logout"]
    for path in ("/orders", "/exchange", "/config"):
        assert admin_client.get(path).status_code == 404
        assert admin_client.post(path, data={"csrf_token": "x"}).status_code in (403, 404, 405)


def test_unsafe_methods_constant_covers_what_the_app_uses(app: Any) -> None:
    used = {m for r in walk_routes(app) for m in r.methods} - {"GET", "HEAD", "OPTIONS"}
    assert used <= UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
