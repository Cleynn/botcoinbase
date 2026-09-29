"""Enumerations shared across layers. String values are stored in the database (CHECK lists)."""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


class ActorRole(StrEnum):
    """Who performed an audited action. HOST_CLI is the local interactive host CLI."""

    ADMIN = "ADMIN"
    VIEWER = "VIEWER"
    HOST_CLI = "HOST_CLI"


class AuditResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


class LoginScope(StrEnum):
    PAIR = "PAIR"  # one account from one client
    CLIENT = "CLIENT"  # one client across accounts
    ACCOUNT = "ACCOUNT"  # one account from any client


class SessionEndReason(StrEnum):
    LOGOUT = "LOGOUT"
    IDLE_TIMEOUT = "IDLE_TIMEOUT"
    ABSOLUTE_EXPIRY = "ABSOLUTE_EXPIRY"
    ADMIN_REVOKED = "ADMIN_REVOKED"
    SELF_REVOKED = "SELF_REVOKED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    ROTATED = "ROTATED"
    SESSION_LIMIT = "SESSION_LIMIT"
    USER_DISABLED = "USER_DISABLED"


class AuditEventType(StrEnum):
    """The audit event catalogue. Adding a code here is the only way to emit a new event."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGIN_THROTTLED = "auth.login.throttled"
    LOGOUT = "auth.logout"
    SESSION_EXPIRED = "session.expired"
    SESSION_REVOKED = "session.revoked"
    SESSION_REJECTED = "session.rejected"
    PASSWORD_CHANGED = "auth.password.changed"
    PASSWORD_CHANGE_FAILED = "auth.password.change_failed"
    REAUTH_SUCCESS = "auth.reauth.success"
    REAUTH_FAILURE = "auth.reauth.failure"
    REAUTH_THROTTLED = "auth.reauth.throttled"
    AUTHZ_DENIED = "authz.denied"
    CSRF_REJECTED = "csrf.rejected"
    ADMIN_SESSIONS_REVOKED = "admin.sessions_revoked"
    ADMIN_SESSIONS_REVOKE_DENIED = "admin.sessions_revoke_denied"
    ADMIN_CREATED = "admin.created"
    ADMIN_PASSWORD_ROTATED = "admin.password_rotated"
