"""Row-to-domain mapping. Column names live here so repositories stay free of mapping code."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.domain.enums import ActorRole, AuditEventType, AuditResult, Role
from app.domain.models import AuditRecord, SessionRecord, User

USER_COLUMNS = "id, username, role, password_hash, password_changed_at, created_at, disabled_at"
SESSION_COLUMNS = (
    "id, user_id, token_hash, created_at, last_seen_at, absolute_expires_at, "
    "revoked_at, revoked_reason, reauth_at"
)


def user_from_row(row: Mapping[str, Any]) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        role=Role(row["role"]),
        password_hash=row["password_hash"],
        password_changed_at=row["password_changed_at"],
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def session_from_row(row: Mapping[str, Any]) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        absolute_expires_at=row["absolute_expires_at"],
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
        reauth_at=row["reauth_at"],
    )


def audit_from_row(row: Mapping[str, Any]) -> AuditRecord:
    return AuditRecord(
        seq=row["seq"],
        event_id=row["event_id"],
        occurred_at=row["occurred_at"],
        event_code=AuditEventType(row["event_code"]),
        result=AuditResult(row["result"]),
        actor_user_id=row["actor_user_id"],
        actor_username=row.get("actor_username"),
        actor_role=ActorRole(row["actor_role"]) if row["actor_role"] else None,
        target_type=row["target_type"],
        target_id=row["target_id"],
        reason_code=row["reason_code"],
        client_tag=row["client_tag"],
        request_id=row["request_id"],
        detail=json.loads(row["detail"]),
        prev_hash=row["prev_hash"],
        event_hash=row["event_hash"],
    )
