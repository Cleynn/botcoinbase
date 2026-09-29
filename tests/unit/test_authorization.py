from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute

from app.api.dependencies import public, require_permission
from app.auth.authorization import AccessDeclaration, authorize, declared_access
from app.domain.enums import Role
from app.domain.permissions import ROLE_PERMISSIONS, Permission, has_permission

VIEWER_ALLOWED = {
    Permission.VIEW_DASHBOARD,
    Permission.VIEW_OWN_SECURITY,
    Permission.CHANGE_OWN_PASSWORD,
    Permission.REAUTHENTICATE,
    Permission.REVOKE_OWN_SESSIONS,
    Permission.LOGOUT,
    Permission.VIEW_PAIRS,
    Permission.VIEW_REPORTS,
}
ADMIN_ONLY = {
    Permission.VIEW_USER_DIRECTORY,
    Permission.REVOKE_USER_SESSIONS,
    Permission.VIEW_AUDIT,
    Permission.MANAGE_PAIRS,
    Permission.MANAGE_REVIEW_PACKAGES,
}


@pytest.mark.parametrize("permission", list(Permission))
def test_permission_matrix(permission: Permission) -> None:
    assert has_permission(Role.ADMIN, permission)
    assert has_permission(Role.VIEWER, permission) is (permission in VIEWER_ALLOWED)


def test_every_permission_is_classified_exactly_once() -> None:
    assert VIEWER_ALLOWED | ADMIN_ONLY == set(Permission)
    assert not VIEWER_ALLOWED & ADMIN_ONLY


def test_admin_is_a_superset_of_viewer() -> None:
    assert ROLE_PERMISSIONS[Role.VIEWER] <= ROLE_PERMISSIONS[Role.ADMIN]


@pytest.mark.parametrize("role", [None, "", "admin", "ROOT", "SUPERUSER", "viewer "])
def test_default_deny_for_missing_or_unknown_roles(role: str | None) -> None:
    for permission in Permission:
        assert not has_permission(role, permission)
        assert not authorize(role, permission)


def test_string_roles_from_the_database_work() -> None:
    assert has_permission("ADMIN", Permission.VIEW_AUDIT)
    assert not has_permission("VIEWER", Permission.VIEW_AUDIT)


def _route(dependencies: list[Any]) -> APIRoute:
    router = APIRouter()

    @router.get("/x", dependencies=dependencies)
    def handler() -> None: ...

    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == "/x")
    return route


def test_declared_access_reads_public_and_permission_markers() -> None:
    assert declared_access(_route([Depends(public)])) == AccessDeclaration(public=True)
    guarded = declared_access(_route([Depends(require_permission(Permission.VIEW_AUDIT))]))
    assert guarded == AccessDeclaration(public=False, permission=Permission.VIEW_AUDIT)


def test_undeclared_and_conflicting_routes_have_no_declaration() -> None:
    assert declared_access(_route([])) is None
    both = _route([Depends(public), Depends(require_permission(Permission.VIEW_AUDIT))])
    assert declared_access(both) is None
