"""Pair lifecycle: states, the single transition table, typed phrases and immutable records.

The transition table below is the one source of truth. Migration 0002 seeds the same rows into
`allowed_transitions` and a database trigger refuses any update that is not in that table, so the
application cannot drift from it (a test compares the two).

Not representable (there is no enum member and the database CHECK excludes them): LIVE_ELIGIBLE
and LIVE_ACTIVE. DISCOVERED is not a stored pair state: a discovered product simply has no pair row
yet (baseline CI-13), and the UI renders it as DISCOVERED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.domain.enums import AuditEventType

DISCOVERED_LABEL: Final = "DISCOVERED"
UNREPRESENTABLE_STATES: Final = ("LIVE_ELIGIBLE", "LIVE_ACTIVE")
MACHINE: Final = "pair"


class PairState(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    PAPER_ACTIVE = "PAPER_ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"
    ARCHIVED = "ARCHIVED"


ACTIVE_STATES: Final = frozenset({PairState.PAPER_ACTIVE})
# States that may hold exposure or are about to. Used for the "another pair is in play" guard.
EXPOSURE_STATES: Final = frozenset({PairState.PAPER_ACTIVE, PairState.PAUSED})
TERMINAL_STATES: Final = frozenset({PairState.ARCHIVED})


class ActorClass(StrEnum):
    WEB = "WEB"  # td_app, an authenticated ADMIN
    HOST = "HOST"  # td_ctl, the local host CLI (also performs the worker duties in this phase)


class Chain(StrEnum):
    NONE = "NONE"  # runner transitions, no human involved
    CSRF = "CSRF"  # ADMIN + CSRF
    RESTRICTIVE = "RESTRICTIVE"  # ADMIN + CSRF, makes the system safer, no typed phrase
    FULL = "FULL"  # ADMIN + CSRF + fresh reauthentication + exact typed phrase + audit


class PairAction(StrEnum):
    VALIDATE = "validate"
    PAUSE = "pause"
    DEACTIVATE = "deactivate"
    ACTIVATE = "activate"
    RESUME = "resume"
    DISABLE = "disable"
    REENABLE = "reenable"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class ActionSpec:
    """A web action: which states it may start from and where it leads."""

    action: PairAction
    from_states: frozenset[PairState]
    to_state: PairState
    chain: Chain
    event: AuditEventType
    phrase: str | None  # template with {product_id}
    transition_no: int
    also_host: bool = False


_S = PairState
_NON_ACTIVE_OPEN = frozenset(
    {_S.PROPOSED, _S.VALIDATING, _S.RESEARCH_ONLY, _S.PAPER_ELIGIBLE, _S.PAUSED}
)

ACTIONS: Final[dict[PairAction, ActionSpec]] = {
    spec.action: spec
    for spec in (
        ActionSpec(
            PairAction.VALIDATE,
            frozenset({_S.PROPOSED, _S.RESEARCH_ONLY, _S.PAUSED}),
            _S.VALIDATING,
            Chain.CSRF,
            AuditEventType.PAIR_VALIDATION_STARTED,
            None,
            2,
            also_host=True,
        ),
        ActionSpec(
            PairAction.PAUSE,
            frozenset({_S.PAPER_ACTIVE}),
            _S.PAUSED,
            Chain.RESTRICTIVE,
            AuditEventType.PAIR_PAUSED,
            None,
            8,
            also_host=True,
        ),
        ActionSpec(
            PairAction.DEACTIVATE,
            frozenset({_S.PAUSED}),
            _S.PAPER_ELIGIBLE,
            Chain.CSRF,
            AuditEventType.PAIR_DEACTIVATED,
            None,
            10,
        ),
        ActionSpec(
            PairAction.ACTIVATE,
            frozenset({_S.PAPER_ELIGIBLE}),
            _S.PAPER_ACTIVE,
            Chain.FULL,
            AuditEventType.PAIR_ACTIVATED_PAPER,
            "ACTIVATE PAPER PAIR {product_id}",
            7,
        ),
        ActionSpec(
            PairAction.RESUME,
            frozenset({_S.PAUSED}),
            _S.PAPER_ACTIVE,
            Chain.FULL,
            AuditEventType.PAIR_RESUMED_PAPER,
            "RESUME PAPER PAIR {product_id}",
            9,
        ),
        ActionSpec(
            PairAction.DISABLE,
            _NON_ACTIVE_OPEN,
            _S.DISABLED,
            Chain.FULL,
            AuditEventType.PAIR_DISABLED,
            "DISABLE PAIR {product_id}",
            12,
        ),
        ActionSpec(
            PairAction.REENABLE,
            frozenset({_S.DISABLED}),
            _S.VALIDATING,
            Chain.FULL,
            AuditEventType.PAIR_REENABLED,
            "REENABLE PAIR {product_id}",
            13,
        ),
        ActionSpec(
            PairAction.ARCHIVE,
            _NON_ACTIVE_OPEN | {_S.DISABLED},
            _S.ARCHIVED,
            Chain.FULL,
            AuditEventType.PAIR_ARCHIVED,
            "ARCHIVE PAIR {product_id}",
            14,
        ),
    )
}


@dataclass(frozen=True)
class Transition:
    """One row of the transition table (also seeded into `allowed_transitions`)."""

    no: int
    from_state: PairState
    to_state: PairState
    actor: ActorClass
    chain: Chain
    event: AuditEventType


# Runner-only transitions (baseline rows 3, 4 and 6). The runner is the host CLI in this phase.
_RUNNER: Final = (
    Transition(
        3,
        _S.VALIDATING,
        _S.RESEARCH_ONLY,
        ActorClass.HOST,
        Chain.NONE,
        AuditEventType.PAIR_RESEARCH_ONLY,
    ),
    Transition(
        4,
        _S.VALIDATING,
        _S.PAPER_ELIGIBLE,
        ActorClass.HOST,
        Chain.NONE,
        AuditEventType.PAIR_PAPER_ELIGIBLE,
    ),
    Transition(
        6,
        _S.PAPER_ELIGIBLE,
        _S.VALIDATING,
        ActorClass.HOST,
        Chain.NONE,
        AuditEventType.PAIR_ELIGIBILITY_EXPIRED,
    ),
)


def all_transitions() -> tuple[Transition, ...]:
    rows: list[Transition] = list(_RUNNER)
    for spec in ACTIONS.values():
        actors = [ActorClass.WEB] + ([ActorClass.HOST] if spec.also_host else [])
        for actor in actors:
            for origin in sorted(spec.from_states):
                rows.append(
                    Transition(
                        spec.transition_no, origin, spec.to_state, actor, spec.chain, spec.event
                    )
                )
    return tuple(sorted(rows, key=lambda r: (r.no, r.from_state, r.to_state, r.actor)))


def phrase_for(action: PairAction, product_id: str) -> str | None:
    template = ACTIONS[action].phrase
    return template.format(product_id=product_id) if template else None


def web_transition_allowed(action: PairAction, state: PairState) -> bool:
    return state in ACTIONS[action].from_states


# ---------------------------------------------------------------------------- records
@dataclass(frozen=True)
class ProductMetadata:
    """Static product rules only (no price, volume or spread). All numbers are Decimal."""

    product_id: str
    base_currency: str
    quote_currency: str
    product_type: str
    venue: str
    status: str
    is_disabled: bool | None
    trading_disabled: bool | None
    cancel_only: bool | None
    limit_only: bool | None
    post_only: bool | None
    auction_mode: bool | None
    base_increment: Decimal | None
    quote_increment: Decimal | None
    price_increment: Decimal | None
    base_min_size: Decimal | None
    base_max_size: Decimal | None
    quote_min_size: Decimal | None
    quote_max_size: Decimal | None
    alias: str | None
    alias_to: tuple[str, ...]
    malformed: tuple[str, ...] = ()  # fixed vocabulary of field names that could not be parsed


@dataclass(frozen=True)
class ProductRecord:
    id: UUID
    metadata: ProductMetadata
    snapshot_id: UUID
    snapshot_sha256: str
    discovered_rank: int | None
    first_seen_at: datetime
    last_seen_at: datetime
    last_verified_at: datetime
    server_time_offset_ms: int | None


@dataclass(frozen=True)
class PairRecord:
    id: UUID
    product_uuid: UUID
    product_id: str
    state: PairState
    version: int
    order_product_id: str
    data_product_id: str | None
    data_basis: str
    proposed_via: str
    proposed_by: UUID | None
    proposed_at: datetime
    state_changed_at: datetime
    ever_active: bool
    eligible_run_id: UUID | None
    successor_of: UUID | None


@dataclass(frozen=True)
class CheckResult:
    code: str
    status: str  # PASS | FAIL | INCONCLUSIVE
    reason: str  # fixed reason code
    message: str  # fixed text from our own templates, never external content
    observed: dict[str, str]  # scalar strings only


@dataclass(frozen=True)
class ValidationRun:
    id: UUID
    pair_id: UUID
    pair_version: int
    snapshot_id: UUID
    started_at: datetime
    finished_at: datetime
    outcome: str
    checks: tuple[CheckResult, ...]
    thresholds_sha256: str
    expires_at: datetime


@dataclass(frozen=True)
class HistoryRow:
    version_after: int
    state_before: str | None
    state_after: str
    actor_class: str
    actor_username: str | None
    transition_no: int
    reason_code: str | None
    occurred_at: datetime
    audit_seq: int | None
