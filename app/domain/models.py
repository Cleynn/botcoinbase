"""Immutable domain records passed between layers. Contains no secrets except password hashes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.enums import ActorRole, AuditEventType, AuditResult, Role


class Clock(Protocol):
    """Injectable time source so expiry logic is testable without sleeping."""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, repr=False)
class User:
    id: UUID
    username: str
    role: Role
    password_hash: str
    password_changed_at: datetime
    created_at: datetime
    disabled_at: datetime | None

    def __repr__(self) -> str:  # never render the hash
        return f"User(id={self.id}, username={self.username!r}, role={self.role})"


@dataclass(frozen=True)
class SessionRecord:
    id: UUID
    user_id: UUID
    token_hash: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    reauth_at: datetime | None


@dataclass(frozen=True)
class ClientIdentity:
    """Keyed hash of the client address. The address itself is never stored or logged."""

    key: str  # full HMAC hex, used for throttling
    tag: str  # short prefix, safe to show in audit records

    @classmethod
    def from_key(cls, key: str) -> ClientIdentity:
        return cls(key=key, tag=key[:12])


@dataclass(frozen=True, repr=False)
class AuthContext:
    user: User
    session: SessionRecord
    csrf_token: str

    def __repr__(self) -> str:  # never render the CSRF token
        return f"AuthContext(user={self.user!r}, session={self.session.id})"


@dataclass(frozen=True)
class AuditActor:
    user_id: UUID | None
    role: ActorRole | None


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    event_id: UUID
    occurred_at: datetime
    event_code: AuditEventType
    result: AuditResult
    actor_user_id: UUID | None
    actor_username: str | None
    actor_role: ActorRole | None
    target_type: str | None
    target_id: str | None
    reason_code: str | None
    client_tag: str | None
    request_id: str | None
    detail: dict[str, Any]
    prev_hash: str
    event_hash: str


@dataclass(frozen=True)
class SessionSummary:
    id: UUID
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    is_current: bool


@dataclass(frozen=True)
class UserSummary:
    id: UUID
    username: str
    role: Role
    disabled: bool
    active_sessions: int


@dataclass(frozen=True)
class ChainStatus:
    """Result of recomputing the audit hash chain."""

    ok: bool
    events_checked: int
    complete: bool  # False when the scan was capped before reaching the head
    broken_at: int | None = None
