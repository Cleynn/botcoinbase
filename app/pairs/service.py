"""Pair use cases for the web tier and the host CLI: propose, queue validation, lifecycle actions.

No method here contacts Coinbase, places or cancels an order, or touches any account: this module
only reads and writes the pair tables through the storage layer. Activation is a separate,
guarded action (never a side effect of creating or validating a pair).

Sensitive actions follow the full chain: ADMIN permission (route) -> CSRF (app-wide) -> stale-view
check -> legality -> exact typed phrase -> external guards -> fresh single-use reauthentication ->
state change + history + audit in one transaction. A wrong phrase never consumes the
reauthentication, and every refusal is itself audited (`pair.transition_denied`).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

import psycopg

from app.auth.audit import AuditWriter
from app.config import PairPolicy
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import AuditRecord, AuthContext, Clock
from app.domain.pairs import (
    ACTIONS,
    UNREPRESENTABLE_STATES,
    Chain,
    HistoryRow,
    PairAction,
    PairRecord,
    PairState,
    ProductRecord,
    ValidationRun,
    phrase_for,
)
from app.pairs import policy as pair_policy
from app.pairs.transitions import Actor, VersionConflict, apply_transition, record_denial
from app.storage.database import Storage
from app.storage.repositories import Repos

Kind = Literal[
    "ok",
    "not_found",
    "stale",
    "not_allowed",
    "phrase_mismatch",
    "reauth_required",
    "blocked",
    "cap_reached",
    "duplicate",
    "conflict",
]


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    pair_id: UUID | None = None
    reasons: tuple[str, ...] = ()


class RuntimeGate(Protocol):
    """State that lives outside the pair tables (bot, mode, reconciliation).

    Neither exists in this build, so the default gate refuses; a later phase supplies the real
    one. Tests inject an open gate to exercise the rest of the chain.
    """

    def activation_blockers(self, repos: Repos, pair: PairRecord) -> tuple[str, ...]: ...

    def is_clean(self, repos: Repos, pair: PairRecord) -> tuple[bool, tuple[str, ...]]: ...


class UnavailableRuntimeGate:
    """Default: paper activation needs a PAUSED bot in PAPER mode, and neither exists yet."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def activation_blockers(self, repos: Repos, pair: PairRecord) -> tuple[str, ...]:
        reasons = ["BOT_STATE_UNAVAILABLE"]
        if self._mode != "PAPER":
            reasons.insert(0, "MODE_NOT_PAPER")
        return tuple(reasons)

    def is_clean(self, repos: Repos, pair: PairRecord) -> tuple[bool, tuple[str, ...]]:
        return False, ("RECONCILIATION_UNAVAILABLE",)


@dataclass(frozen=True)
class PairSummary:
    pair: PairRecord
    product: ProductRecord | None
    latest_run: ValidationRun | None


@dataclass(frozen=True)
class PairsOverview:
    pairs: tuple[PairSummary, ...]
    counts: dict[str, int]
    open_count: int
    max_pairs: int
    active: PairRecord | None


@dataclass(frozen=True)
class ProductRow:
    product: ProductRecord
    default_candidate: bool
    existing_pair: PairRecord | None
    verdict_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProductPage:
    rows: tuple[ProductRow, ...]
    total: int
    page: int
    page_size: int
    default_only: bool


@dataclass(frozen=True)
class PairDetail:
    pair: PairRecord
    product: ProductRecord
    runs: tuple[ValidationRun, ...]
    history: tuple[HistoryRow, ...]
    audit: tuple[AuditRecord, ...]
    blockers: dict[PairAction, tuple[str, ...]]
    now: datetime
    is_valid: bool


@dataclass(frozen=True)
class Confirmation:
    pair: PairRecord
    action: PairAction
    phrase: str
    blockers: tuple[str, ...]
    reauth_active: bool


@dataclass
class _Attempt:
    """Working state for one guarded action."""

    reasons: list[str] = field(default_factory=list)


def _same_text(typed: str, expected: str) -> bool:
    return typed.isascii() and hmac.compare_digest(typed.encode("ascii"), expected.encode("ascii"))


class PairService:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        policy: PairPolicy,
        audit: AuditWriter,
        gate: RuntimeGate,
        consume_reauth: Callable[[Repos, AuthContext], bool],
        reauth_active: Callable[[AuthContext], bool],
    ) -> None:
        self._storage, self._clock, self._policy = storage, clock, policy
        self._audit, self._gate = audit, gate
        self._consume_reauth, self._reauth_active = consume_reauth, reauth_active

    # ------------------------------------------------------------------ read models
    def overview(self) -> PairsOverview:
        with self._storage.tx() as repos:
            pairs = repos.pairs.list_all()
            summaries = tuple(
                PairSummary(p, repos.products.get(p.product_uuid), repos.pairs.latest_run(p.id))
                for p in pairs
            )
            counts = repos.pairs.state_counts()
            active = next((p for p in pairs if p.state is PairState.PAPER_ACTIVE), None)
            open_count = sum(1 for p in pairs if p.state is not PairState.ARCHIVED)
        return PairsOverview(summaries, counts, open_count, self._policy.max_pairs, active)

    def products(self, *, default_only: bool, page: int, page_size: int) -> ProductPage:
        with self._storage.tx() as repos:
            products = repos.products.list_all()
            open_pairs = {
                p.product_uuid: p
                for p in repos.pairs.list_all()
                if p.state is not PairState.ARCHIVED
            }
        rows: list[ProductRow] = []
        for product in products:
            verdict = pair_policy.assess_product(product.metadata)
            is_default = verdict.ok and not pair_policy.is_stable_base(product.metadata)
            if default_only and not is_default:
                continue
            reasons = verdict.failures + verdict.inconclusive
            if pair_policy.is_stable_base(product.metadata):
                reasons += ("STABLE_BASE",)
            rows.append(ProductRow(product, is_default, open_pairs.get(product.id), reasons))
        start = max(page - 1, 0) * page_size
        return ProductPage(
            tuple(rows[start : start + page_size]), len(rows), page, page_size, default_only
        )

    def detail(self, pair_id: UUID, *, include_audit: bool) -> PairDetail | None:
        with self._storage.tx() as repos:
            pair = repos.pairs.get(pair_id)
            if pair is None:
                return None
            product = repos.products.get(pair.product_uuid)
            if product is None:  # pragma: no cover  (foreign key guarantees it)
                return None
            runs = tuple(repos.pairs.runs(pair.id, limit=5))
            history = tuple(repos.pairs.history(pair.id))
            audit = tuple(repos.pairs.audit_timeline(pair.id)) if include_audit else ()
            now = self._clock.now()
            blockers = {
                action: self._blockers(repos, pair, product, action, now)
                for action in PairAction
                if pair.state in ACTIONS[action].from_states
            }
            valid = not self._validity_problems(pair, product, runs[0] if runs else None, now)
        return PairDetail(pair, product, runs, history, audit, blockers, now, valid)

    def confirmation(
        self, ctx: AuthContext, pair_id: UUID, action: PairAction
    ) -> Confirmation | None:
        spec = ACTIONS[action]
        if spec.chain is not Chain.FULL or spec.phrase is None:
            return None
        with self._storage.tx() as repos:
            pair = repos.pairs.get(pair_id)
            if pair is None or pair.state not in spec.from_states:
                return None
            product = repos.products.get(pair.product_uuid)
            blockers = (
                self._blockers(repos, pair, product, action, self._clock.now()) if product else ()
            )
        return Confirmation(
            pair,
            action,
            phrase_for(action, pair.product_id) or "",
            blockers,
            self._reauth_active(ctx),
        )

    # ------------------------------------------------------------------ guards
    def _validity_problems(
        self,
        pair: PairRecord,
        product: ProductRecord,
        run: ValidationRun | None,
        now: datetime,
    ) -> tuple[str, ...]:
        """VALID(pair): a PASS run, unexpired, on the metadata that is current now."""
        problems: list[str] = []
        eligible = pair.eligible_run_id
        if run is None or run.outcome != "PASS":
            problems.append("NO_PASS_VALIDATION")
        elif eligible is not None and run.id != eligible:
            problems.append("VALIDATION_SUPERSEDED")
        elif run.expires_at <= now:
            problems.append("VALIDATION_EXPIRED")
        elif run.snapshot_id != product.snapshot_id:
            problems.append("METADATA_CHANGED")
        limit = timedelta(seconds=self._policy.validation.max_metadata_age_seconds)
        if now - product.last_verified_at > limit:
            problems.append("METADATA_STALE")
        if not pair_policy.assess_product(product.metadata).ok:
            problems.append("PRODUCT_NOT_OK")
        return tuple(problems)

    def _blockers(
        self,
        repos: Repos,
        pair: PairRecord,
        product: ProductRecord | None,
        action: PairAction,
        now: datetime,
    ) -> tuple[str, ...]:
        """Reasons the action is refused right now (shown to the operator, re-checked on submit)."""
        reasons: list[str] = []
        if action in (PairAction.ACTIVATE, PairAction.RESUME):
            if product is None:
                return ("PRODUCT_MISSING",)
            run = repos.pairs.latest_run(pair.id)
            reasons.extend(self._validity_problems(pair, product, run, now))
            others = repos.pairs.others_in_states(
                pair.id, frozenset({PairState.PAPER_ACTIVE, PairState.PAUSED})
            )
            max_pairs = (
                repos.safety.active_trading_config().max_pairs
            )  # the database enforces it too
            active_others = sum(1 for o in others if o.state is PairState.PAPER_ACTIVE)
            if active_others >= max_pairs:
                reasons.append("ANOTHER_PAIR_ACTIVE")
            if max_pairs == 1 and any(o.state is PairState.PAUSED for o in others):
                reasons.append("ANOTHER_PAIR_PAUSED")
            reasons.extend(self._gate.activation_blockers(repos, pair))
        if action in (PairAction.DISABLE, PairAction.ARCHIVE) and pair.ever_active:
            clean, why = self._gate.is_clean(repos, pair)
            if not clean:
                reasons.extend(why or ("NOT_CLEAN",))
        return tuple(dict.fromkeys(reasons))

    # ------------------------------------------------------------------ propose
    def propose(self, actor: Actor, product_uuid: UUID) -> Outcome:
        now = self._clock.now()
        with self._storage.tx() as repos:
            repos.pairs.lock_capacity()
            product = repos.products.get(product_uuid)
            if product is None:
                return Outcome("not_found")
            meta = product.metadata
            if repos.pairs.count_open() >= self._policy.max_pairs:
                record_denial(
                    repos,
                    self._audit,
                    pair_id=None,
                    product=meta.product_id,
                    actor=actor,
                    attempted="propose",
                    reason="PAIR_CAP_REACHED",
                )
                return Outcome("cap_reached")
            if repos.pairs.open_for_product(product.id) is not None:
                record_denial(
                    repos,
                    self._audit,
                    pair_id=None,
                    product=meta.product_id,
                    actor=actor,
                    attempted="propose",
                    reason="ALREADY_A_CANDIDATE",
                )
                return Outcome("duplicate")
            data_product, basis = pair_policy.derive_data_basis(meta)
            pair_id = uuid4()
            repos.pairs.insert(
                pair_id=pair_id,
                product_uuid=product.id,
                order_product_id=meta.product_id,
                data_product_id=data_product,
                data_basis=basis,
                proposed_via=actor.cls.value,
                proposed_by=actor.user_id,
                now=now,
                successor_of=repos.pairs.latest_archived_for_product(product.id),
            )
            self._audit.record(
                repos,
                AuditEventType.PAIR_PROPOSED,
                AuditResult.SUCCESS,
                actor=actor.audit,
                target_type="pair",
                target_id=pair_id,
                reason="PROPOSED",
                client_tag=actor.client_tag,
                request_id=actor.request_id,
                detail={"product": meta.product_id, "basis": basis, "via": actor.cls.value},
            )
            repos.pairs.add_history(
                pair_id=pair_id,
                version_after=1,
                state_before=None,
                state_after=PairState.PROPOSED.value,
                actor_class=actor.cls.value,
                actor_user_id=actor.user_id,
                transition_no=1,
                reason_code="PROPOSED",
                occurred_at=now,
                request_id=actor.request_id,
                audit_seq=repos.monitoring.audit_last_seq(),
            )
        return Outcome("ok", pair_id)

    # ------------------------------------------------------------------ simple (CSRF-only) actions
    def simple_action(
        self, actor: Actor, pair_id: UUID, action: PairAction, expected_version: int
    ) -> Outcome:
        spec = ACTIONS[action]
        if spec.chain is Chain.FULL:
            raise ValueError("use confirm() for actions that need the full chain")
        now = self._clock.now()
        try:
            with self._storage.tx() as repos:
                pair = repos.pairs.get(pair_id, for_update=True)
                if pair is None:
                    return Outcome("not_found")
                refused = self._refuse(repos, actor, pair, action, expected_version)
                if refused is not None:
                    return refused
                apply_transition(
                    repos,
                    self._audit,
                    pair=pair,
                    to_state=spec.to_state,
                    actor=actor,
                    transition_no=spec.transition_no,
                    event=spec.event,
                    reason=action.name,
                    now=now,
                )
        except (VersionConflict, psycopg.errors.IntegrityConstraintViolation):
            return self._denied_after_error(actor, pair_id, action, "TRANSITION_REJECTED")
        return Outcome("ok", pair_id)

    def _refuse(
        self,
        repos: Repos,
        actor: Actor,
        pair: PairRecord,
        action: PairAction,
        expected_version: int,
    ) -> Outcome | None:
        """Common refusals (stale view, illegal state). Audits the denial."""
        if pair.version != expected_version:
            record_denial(
                repos,
                self._audit,
                pair_id=pair.id,
                product=pair.product_id,
                actor=actor,
                attempted=action.value,
                reason="STALE_VERSION",
            )
            return Outcome("stale", pair.id)
        if (
            pair.state.value in UNREPRESENTABLE_STATES
            or pair.state not in ACTIONS[action].from_states
        ):
            reason = _illegal_reason(pair.state, action)
            record_denial(
                repos,
                self._audit,
                pair_id=pair.id,
                product=pair.product_id,
                actor=actor,
                attempted=action.value,
                reason=reason,
            )
            return Outcome("not_allowed", pair.id, (reason,))
        return None

    def _denied_after_error(
        self, actor: Actor, pair_id: UUID, action: PairAction, reason: str
    ) -> Outcome:
        """A database refusal aborted the transaction; audit it in a fresh one."""
        with self._storage.tx() as repos:
            pair = repos.pairs.get(pair_id)
            record_denial(
                repos,
                self._audit,
                pair_id=pair_id,
                product=pair.product_id if pair else None,
                actor=actor,
                attempted=action.value,
                reason=reason,
            )
        return Outcome("conflict", pair_id, (reason,))

    # ------------------------------------------------------------------ full-chain actions
    def confirm(
        self,
        ctx: AuthContext,
        actor: Actor,
        pair_id: UUID,
        action: PairAction,
        expected_version: int,
        typed: str,
    ) -> Outcome:
        spec = ACTIONS[action]
        if spec.chain is not Chain.FULL or spec.phrase is None:
            raise ValueError("confirm() is for full-chain actions only")
        now = self._clock.now()
        try:
            with self._storage.tx() as repos:
                if action in (PairAction.ACTIVATE, PairAction.RESUME):
                    repos.pairs.lock_capacity()
                pair = repos.pairs.get(pair_id, for_update=True)
                if pair is None:
                    return Outcome("not_found")
                refused = self._refuse(repos, actor, pair, action, expected_version)
                if refused is not None:
                    return refused
                expected = spec.phrase.format(product_id=pair.product_id)
                if not _same_text(typed, expected):
                    record_denial(
                        repos,
                        self._audit,
                        pair_id=pair.id,
                        product=pair.product_id,
                        actor=actor,
                        attempted=action.value,
                        reason="PHRASE_MISMATCH",
                    )
                    return Outcome("phrase_mismatch", pair.id)
                product = repos.products.get(pair.product_uuid)
                blockers = self._blockers(repos, pair, product, action, now)
                if blockers:
                    record_denial(
                        repos,
                        self._audit,
                        pair_id=pair.id,
                        product=pair.product_id,
                        actor=actor,
                        attempted=action.value,
                        reason="GUARD_FAILED",
                        reasons=blockers,
                    )
                    return Outcome("blocked", pair.id, blockers)
                if not self._consume_reauth(repos, ctx):
                    record_denial(
                        repos,
                        self._audit,
                        pair_id=pair.id,
                        product=pair.product_id,
                        actor=actor,
                        attempted=action.value,
                        reason="REAUTH_REQUIRED",
                    )
                    return Outcome("reauth_required", pair.id)
                activating = spec.to_state is PairState.PAPER_ACTIVE
                apply_transition(
                    repos,
                    self._audit,
                    pair=pair,
                    to_state=spec.to_state,
                    actor=actor,
                    transition_no=spec.transition_no,
                    event=spec.event,
                    reason=action.name,
                    now=now,
                    ever_active=True if activating else None,
                )
        except psycopg.errors.UniqueViolation:
            return self._denied_after_error(actor, pair_id, action, "ANOTHER_PAIR_ACTIVE")
        except (VersionConflict, psycopg.errors.IntegrityConstraintViolation):
            return self._denied_after_error(actor, pair_id, action, "TRANSITION_REJECTED")
        return Outcome("ok", pair_id)


def _illegal_reason(state: PairState, action: PairAction) -> str:
    if state is PairState.ARCHIVED:
        return "ARCHIVED_IS_TERMINAL"
    if state is PairState.PAPER_ACTIVE and action in (PairAction.DISABLE, PairAction.ARCHIVE):
        return f"ACTIVE_PAIR_CANNOT_{action.name}"
    return "ILLEGAL_TRANSITION"
