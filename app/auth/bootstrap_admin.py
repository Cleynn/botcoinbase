"""Host-CLI account operations: create the first ADMIN, rotate a password.

These run only from an interactive terminal on the host/container (see scripts/). There is no web
route for either: no signup, no invitation, no password reset.
"""

from __future__ import annotations

import sys
from uuid import uuid4

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.auth.password import (
    POLICY_MESSAGES,
    PasswordPolicyError,
    PasswordService,
    validate_new_password,
)
from app.auth.session import USERNAME_RE
from app.config import AuthSettings
from app.domain.enums import AuditEventType, AuditResult, Role, SessionEndReason
from app.domain.models import Clock, User
from app.storage.database import Storage

CLI_TAG = "host_cli"


class BootstrapError(Exception):
    """A user-facing, secret-free reason the operation was refused."""


def require_tty() -> None:
    """Refuse to run unless both stdin and stdout are terminals (no piping, no automation)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise BootstrapError(
            "an interactive terminal (TTY) is required; run with `docker compose run --rm -it ...`"
        )


def _checked_password(password: str, username: str, settings: AuthSettings) -> None:
    try:
        validate_new_password(password, username, settings)
    except PasswordPolicyError as exc:
        raise BootstrapError(" ".join(POLICY_MESSAGES.get(c, c) for c in exc.codes)) from exc


def create_first_admin(
    *,
    storage: Storage,
    passwords: PasswordService,
    settings: AuthSettings,
    clock: Clock,
    username: str,
    password: str,
) -> User:
    name = username.strip().lower()
    if not USERNAME_RE.fullmatch(name):
        raise BootstrapError(
            "username must be 3-64 characters: a-z, 0-9, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    _checked_password(password, name, settings)
    password_hash = passwords.hash(password)
    now = clock.now()
    user = User(uuid4(), name, Role.ADMIN, password_hash, now, now, None)
    with storage.tx() as repos:
        repos.users.lock_bootstrap()
        if repos.users.any_admin():
            raise BootstrapError(
                "an ADMIN already exists; this command only creates the first ADMIN"
            )
        repos.users.insert(user)
        AuditWriter(clock).record(
            repos,
            AuditEventType.ADMIN_CREATED,
            AuditResult.SUCCESS,
            actor=HOST_CLI_ACTOR,
            target_type="user",
            target_id=user.id,
            client_tag=CLI_TAG,
            request_id="cli",
            detail={"role": Role.ADMIN.value},
        )
    return user


def rotate_password(
    *,
    storage: Storage,
    passwords: PasswordService,
    settings: AuthSettings,
    clock: Clock,
    username: str,
    new_password: str,
) -> int:
    """Set a new password and end all of that user's sessions. Returns how many were ended."""
    name = username.strip().lower()
    _checked_password(new_password, name, settings)
    password_hash = passwords.hash(new_password)
    now = clock.now()
    with storage.tx() as repos:
        user = repos.users.get_by_username(name)
        if user is None:
            raise BootstrapError("unknown user")
        repos.users.update_password(user.id, password_hash, now)
        ended = repos.sessions.revoke_all_for_user(user.id, SessionEndReason.PASSWORD_CHANGED, now)
        AuditWriter(clock).record(
            repos,
            AuditEventType.ADMIN_PASSWORD_ROTATED,
            AuditResult.SUCCESS,
            actor=HOST_CLI_ACTOR,
            target_type="user",
            target_id=user.id,
            client_tag=CLI_TAG,
            request_id="cli",
            detail={"sessions_ended": ended},
        )
    return ended
