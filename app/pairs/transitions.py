"""The one place a pair changes state: compare-and-set, history row and audit event together.

Every caller runs inside a storage transaction and has already locked the pair row. The database
trigger (migration 0002) independently refuses any change that is not in `allowed_transitions`
for the acting role, so a bug here cannot produce an illegal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.auth.audit import AuditWriter
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import AuditActor
from app.domain.pairs import ActorClass, PairRecord, PairState
from app.storage.repositories import Repos


class VersionConflict(Exception):
    """The pair changed since the caller read it."""


@dataclass(frozen=True)
class Actor:
    """Who is acting: the audit identity, the database actor class and a non-identifying tag."""

    audit: AuditActor
    cls: ActorClass
    client_tag: str
    request_id: str | None

    @property
    def user_id(self) -> UUID | None:
        return self.audit.user_id


def apply_transition(
    repos: Repos,
    audit: AuditWriter,
    *,
    pair: PairRecord,
    to_state: PairState,
    actor: Actor,
    transition_no: int,
    event: AuditEventType,
    reason: str,
    now: datetime,
    detail: dict[str, str | int | bool | None] | None = None,
    ever_active: bool | None = None,
    eligible_run_id: UUID | None = None,
) -> PairRecord:
    if not repos.pairs.transition(
        pair, to_state, now, ever_active=ever_active, eligible_run_id=eligible_run_id
    ):
        raise VersionConflict
    version_after = pair.version + 1
    audit.record(
        repos,
        event,
        AuditResult.SUCCESS,
        actor=actor.audit,
        target_type="pair",
        target_id=pair.id,
        reason=reason,
        client_tag=actor.client_tag,
        request_id=actor.request_id,
        detail={
            "product": pair.product_id,
            "from": pair.state.value,
            "to": to_state.value,
            "version": version_after,
            **(detail or {}),
        },
    )
    repos.pairs.add_history(
        pair_id=pair.id,
        version_after=version_after,
        state_before=pair.state.value,
        state_after=to_state.value,
        actor_class=actor.cls.value,
        actor_user_id=actor.user_id,
        transition_no=transition_no,
        reason_code=reason,
        occurred_at=now,
        request_id=actor.request_id,
        audit_seq=repos.monitoring.audit_last_seq(),
    )
    updated = repos.pairs.get(pair.id)
    if updated is None:  # pragma: no cover  (the row was just updated in this transaction)
        raise VersionConflict
    return updated


def record_denial(
    repos: Repos,
    audit: AuditWriter,
    *,
    pair_id: UUID | str | None,
    product: str | None,
    actor: Actor,
    attempted: str,
    reason: str,
    reasons: tuple[str, ...] = (),
) -> None:
    """A refused attempt is itself audited (pair.transition_denied)."""
    audit.record(
        repos,
        AuditEventType.PAIR_TRANSITION_DENIED,
        AuditResult.DENIED,
        actor=actor.audit,
        target_type="pair" if pair_id else "product",
        target_id=pair_id or product,
        reason=reason,
        client_tag=actor.client_tag,
        request_id=actor.request_id,
        detail={"attempted": attempted, "product": product, "reasons": ",".join(reasons)},
    )
