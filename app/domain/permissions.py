"""Role to permission mapping. Default deny: anything not listed is refused."""

from enum import StrEnum
from types import MappingProxyType

from app.domain.enums import Role


class Permission(StrEnum):
    VIEW_DASHBOARD = "view_dashboard"
    VIEW_OWN_SECURITY = "view_own_security"
    CHANGE_OWN_PASSWORD = "change_own_password"
    REAUTHENTICATE = "reauthenticate"
    REVOKE_OWN_SESSIONS = "revoke_own_sessions"
    LOGOUT = "logout"
    VIEW_USER_DIRECTORY = "view_user_directory"
    REVOKE_USER_SESSIONS = "revoke_user_sessions"
    VIEW_AUDIT = "view_audit"
    VIEW_PAIRS = "view_pairs"
    MANAGE_PAIRS = "manage_pairs"
    VIEW_REPORTS = "view_reports"


_VIEWER = frozenset(
    {
        Permission.VIEW_DASHBOARD,
        Permission.VIEW_OWN_SECURITY,
        Permission.CHANGE_OWN_PASSWORD,
        Permission.REAUTHENTICATE,
        Permission.REVOKE_OWN_SESSIONS,
        Permission.LOGOUT,
        Permission.VIEW_PAIRS,
        Permission.VIEW_REPORTS,
    }
)
_ADMIN = _VIEWER | frozenset(
    {
        Permission.VIEW_USER_DIRECTORY,
        Permission.REVOKE_USER_SESSIONS,
        Permission.VIEW_AUDIT,
        Permission.MANAGE_PAIRS,
    }
)

ROLE_PERMISSIONS = MappingProxyType({Role.VIEWER: _VIEWER, Role.ADMIN: _ADMIN})


def has_permission(role: Role | str | None, permission: Permission) -> bool:
    """True only if the role is known and explicitly granted the permission."""
    if role is None:
        return False
    try:
        known = Role(role)
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS.get(known, frozenset())
