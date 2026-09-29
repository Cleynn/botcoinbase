"""Audit recording: catalogue-checked, secret-free, size-bounded, optionally throttled.

Writes join the caller's transaction, so a state change and its audit event commit together.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from uuid import UUID

from app.domain.enums import ActorRole, AuditEventType, AuditResult
from app.domain.models import AuditActor, Clock, User
from app.storage.repositories import Repos

HOST_CLI_ACTOR = AuditActor(user_id=None, role=ActorRole.HOST_CLI)
_FORBIDDEN_KEY = re.compile(r"pass|secret|token|cookie|hash|key|auth|session_id", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_VALUE = 200


def actor_for(user: User | None) -> AuditActor | None:
    return AuditActor(user_id=user.id, role=ActorRole(user.role.value)) if user else None


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _CONTROL.sub(" ", value)[:_MAX_VALUE]
    raise ValueError("audit detail values must be str, int, bool or None")


def _field(value: str | UUID | None, limit: int) -> str | None:
    """Bound and clean a string column: no control characters (NUL breaks PostgreSQL)."""
    if value is None:
        return None
    return _CONTROL.sub(" ", str(value))[:limit]


def sanitise_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if _FORBIDDEN_KEY.search(key):
            raise ValueError(f"audit detail key '{key}' is not permitted")
        cleaned[key] = _clean(value)
    return cleaned


class AuditWriter:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def record(
        self,
        repos: Repos,
        event: AuditEventType,
        result: AuditResult,
        *,
        actor: AuditActor | None = None,
        target_type: str | None = None,
        target_id: str | UUID | None = None,
        reason: str | None = None,
        client_tag: str | None = None,
        request_id: str | None = None,
        detail: Mapping[str, Any] | None = None,
        throttle_seconds: int = 0,
    ) -> bool:
        """Append an event. Returns False if it was suppressed by the per-client throttle."""
        now = self._clock.now()
        target_type_clean = _field(target_type, 64)
        target_id_clean = _field(target_id, 128)
        client_tag_clean = _field(client_tag, 32)
        if throttle_seconds and client_tag_clean:
            since = now - timedelta(seconds=throttle_seconds)
            # One event per (event type, client, target) per window: floods are bounded, but
            # denials on different resources are all still recorded.
            if repos.audit.exists_recent(event, client_tag_clean, since, target_id_clean):
                return False
        repos.audit.append(
            occurred_at=now,
            event_code=event,
            result=result,
            actor_user_id=actor.user_id if actor else None,
            actor_role=actor.role if actor else None,
            target_type=target_type_clean,
            target_id=target_id_clean,
            reason_code=_field(reason, 64),
            client_tag=client_tag_clean,
            request_id=_field(request_id, 64),
            detail=sanitise_detail(detail),
        )
        return True
