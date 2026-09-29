"""Proposal use cases for the web tier (ADMIN only; the route enforces the permission).

Every proposal is UNTRUSTED ADVISORY INPUT. Nothing here executes, evaluates, renders or applies
proposal content: import stores opaque bytes after byte-level checks; approval creates a tracked
MANUAL change request record; the later steps are ADMIN attestations about work done elsewhere and
verify only what the database can (that cited reports exist and are the right kind). Nothing here
touches a pair, order, ledger, strategy, risk, configuration or live setting.

Chains: import-enable, import and change-request creation need CSRF (app-wide) + fresh single-use
reauthentication + an exact typed phrase; the attestation steps need CSRF + fresh reauthentication;
disable, review and close need CSRF. A wrong phrase never consumes the reauthentication, and every
refusal is audited (`proposal.denied`).
"""

from __future__ import annotations

import hmac
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import psycopg

from app.auth.audit import AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import AuthContext, Clock
from app.pairs.transitions import Actor
from app.proposals import intake
from app.proposals.store import ProposalStore
from app.storage.database import Storage
from app.storage.proposal_repositories import (
    AttestationRow,
    ChangeRequestRow,
    HistoryRow,
    ProposalRow,
    ProposalSettingsRow,
)
from app.storage.repositories import Repos

Evt = AuditEventType
Res = AuditResult

PHRASE_ENABLE: Final = "ENABLE UNTRUSTED PROPOSAL IMPORT"
PHRASE_IMPORT: Final = "IMPORT UNTRUSTED LLM PROPOSAL"
CHANGE_TYPES: Final = (
    "PARAMETER_CHANGE",
    "CODE_CHANGE",
    "CONFIG_RELEASE",
    "DOCUMENTATION",
    "MONITORING",
    "RESEARCH_ONLY",
)
CLOSE_REASONS: Final = ("COMPLETED", "NOT_PURSUED", "SUPERSEDED", "DUPLICATE", "UNSAFE", "OTHER")
ATTEST_KINDS: Final = ("IMPLEMENTED", "BACKTESTED", "PAPER_VALIDATED")
PAPER_MIN_SPAN_DAYS: Final = 7
MAX_PER_HOUR: Final = 10
MAX_PER_DAY: Final = 20
MAX_STORED_BYTES: Final = 200 * 1024 * 1024
_RELEASE_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@-]{2,63}")
_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def phrase_change_request(proposal_id: UUID | str) -> str:
    return f"CREATE MANUAL CHANGE REQUEST FOR PROPOSAL {proposal_id}"


Kind = Literal[
    "ok",
    "not_found",
    "invalid",
    "phrase_mismatch",
    "reauth_required",
    "conflict",
    "disabled",
    "limit",
    "not_allowed",
    "rejected_input",
]


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    proposal_id: UUID | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Overview:
    settings: ProposalSettingsRow
    proposals: tuple[ProposalRow, ...]
    counts: dict[str, int]
    import_blockers: tuple[str, ...]
    reauth_active: bool = False


@dataclass(frozen=True)
class Detail:
    proposal: ProposalRow
    history: tuple[HistoryRow, ...]
    change_request: ChangeRequestRow | None
    attestations: dict[str, AttestationRow]
    import_enabled: bool
    reauth_active: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _same(typed: str, expected: str) -> bool:
    return typed.isascii() and hmac.compare_digest(typed.encode("ascii"), expected.encode("ascii"))


def clean_note(value: str, *, lo: int, hi: int) -> str | None:
    """Plain text for an ADMIN note: bounded, no control or hidden characters. None if unusable."""
    if not lo <= len(value.strip()) or len(value) > hi:
        return None
    for ch in value:
        category = unicodedata.category(ch)
        if (category == "Cc" and ch not in "\n") or category in (
            "Cf",
            "Cs",
            "Co",
            "Cn",
            "Zl",
            "Zp",
        ):
            return None
    return value.strip()


class ProposalService:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        audit: AuditWriter,
        consume_reauth: Callable[[Repos, AuthContext], bool],
        reauth_active: Callable[[AuthContext], bool],
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._audit = audit
        self._consume_reauth, self._reauth_active = consume_reauth, reauth_active
        self._store = ProposalStore(settings.proposals.dir)

    # ------------------------------------------------------------------ audit
    def _record(
        self,
        repos: Repos,
        actor: Actor,
        event: Evt,
        result: Res,
        target: UUID | str | None,
        reason: str | None = None,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=actor.audit,
            target_type="proposal" if target else "proposal_feature",
            target_id=target,
            reason=reason,
            client_tag=actor.client_tag,
            request_id=actor.request_id,
            detail=detail,
        )

    def _deny(
        self, repos: Repos, actor: Actor, action: str, reason: str, target: UUID | None = None
    ) -> None:
        self._record(
            repos, actor, Evt.PROPOSAL_DENIED, Res.DENIED, target, reason, {"attempted": action}
        )

    def _chain(
        self,
        repos: Repos,
        ctx: AuthContext,
        actor: Actor,
        action: str,
        typed: str | None,
        expected: str | None,
        target: UUID | None = None,
    ) -> Outcome | None:
        """Phrase (if the action has one), then fresh single-use reauthentication."""
        if expected is not None and not _same(typed or "", expected):
            self._deny(repos, actor, action, "PHRASE_MISMATCH", target)
            return Outcome("phrase_mismatch", target)
        if not self._consume_reauth(repos, ctx):
            self._deny(repos, actor, action, "REAUTH_REQUIRED", target)
            return Outcome("reauth_required", target)
        return None

    def _transition(
        self,
        repos: Repos,
        actor: Actor,
        row: ProposalRow,
        new_state: str,
        event: Evt,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        now = self._clock.now()
        repos.proposals.add_history(row.id, row.state, new_state, "WEB", actor.user_id, None, now)
        self._record(repos, actor, event, Res.SUCCESS, row.id, None, detail)

    # ------------------------------------------------------------------ read models
    def reauth_active(self, ctx: AuthContext) -> bool:
        return self._reauth_active(ctx)

    def overview(self, ctx: AuthContext) -> Overview:
        with self._storage.tx() as repos:
            settings = repos.proposals.settings()
            rows = tuple(repos.proposals.recent(50))
            counts = repos.proposals.counts()
            blockers = self._import_blockers(repos, settings)
        return Overview(settings, rows, counts, blockers, self._reauth_active(ctx))

    def detail(self, ctx: AuthContext, proposal_id: UUID) -> Detail | None:
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id)
            if row is None:
                return None
            return Detail(
                row,
                tuple(repos.proposals.history(proposal_id)),
                repos.proposals.change_request(proposal_id),
                repos.proposals.attestations(proposal_id),
                repos.proposals.settings().import_enabled,
                self._reauth_active(ctx),
            )

    def _import_blockers(self, repos: Repos, settings: ProposalSettingsRow) -> tuple[str, ...]:
        if not settings.import_enabled:
            return ("IMPORT_DISABLED",)
        now = self._clock.now()
        reasons: list[str] = []
        if repos.proposals.imports_since(now - timedelta(hours=1)) >= MAX_PER_HOUR:
            reasons.append("RATE_LIMIT_HOUR")
        if repos.proposals.imports_since(now - timedelta(days=1)) >= MAX_PER_DAY:
            reasons.append("RATE_LIMIT_DAY")
        if repos.proposals.stored_bytes() >= MAX_STORED_BYTES:
            reasons.append("STORAGE_LIMIT")
        return tuple(reasons)

    # ------------------------------------------------------------------ enable / disable import
    def enable_import(self, ctx: AuthContext, actor: Actor, typed: str) -> Outcome:
        with self._storage.tx() as repos:
            if repos.proposals.settings(for_update=True).import_enabled:
                self._deny(repos, actor, "enable_import", "ALREADY_ENABLED")
                return Outcome("conflict", reasons=("ALREADY_ENABLED",))
            refused = self._chain(repos, ctx, actor, "enable_import", typed, PHRASE_ENABLE)
            if refused is not None:
                return refused
            repos.proposals.set_import_enabled(True, self._clock.now())
            self._record(repos, actor, Evt.PROPOSAL_IMPORT_ENABLED, Res.SUCCESS, None)
        return Outcome("ok")

    def disable_import(self, actor: Actor) -> Outcome:
        """Restrictive direction: CSRF and ADMIN only (a fail-safe action is never made harder)."""
        with self._storage.tx() as repos:
            if not repos.proposals.settings(for_update=True).import_enabled:
                self._deny(repos, actor, "disable_import", "ALREADY_DISABLED")
                return Outcome("conflict", reasons=("ALREADY_DISABLED",))
            repos.proposals.set_import_enabled(False, self._clock.now())
            self._record(repos, actor, Evt.PROPOSAL_IMPORT_DISABLED, Res.SUCCESS, None)
        return Outcome("ok")

    # ------------------------------------------------------------------ import
    def import_proposal(
        self,
        ctx: AuthContext,
        actor: Actor,
        typed: str,
        *,
        declared_mime: str | None,
        filename: str | None,
        data: bytes,
    ) -> Outcome:
        """Store one proposal as opaque bytes. Only byte-level checks run here; parsing happens on
        the host. Content checks come BEFORE the chain so a bad file never spends a reauth."""
        limits = self._settings.proposals
        with self._storage.tx() as repos:
            settings = repos.proposals.settings()
            if not settings.import_enabled:
                self._deny(repos, actor, "import", "IMPORT_DISABLED")
                return Outcome("disabled")
            blockers = self._import_blockers(repos, settings)
            if blockers:
                self._deny(repos, actor, "import", blockers[0])
                return Outcome("limit", reasons=blockers)
        try:
            intake.check_filename(filename)
            checked = intake.check_bytes(declared_mime, data, max_bytes=limits.max_bytes)
        except intake.IntakeRejected as exc:
            with self._storage.tx() as repos:
                self._deny(repos, actor, "import", exc.code)
            return Outcome("rejected_input", reasons=(exc.code,))
        proposal_id = uuid4()
        storage_name = f"{uuid4()}.proposal"
        written = False
        try:
            with self._storage.tx() as repos:
                repos.proposals.settings(for_update=True)
                if repos.proposals.sha_is_live(checked.sha256):
                    self._deny(repos, actor, "import", "DUPLICATE")
                    return Outcome("conflict", reasons=("DUPLICATE",))
                refused = self._chain(repos, ctx, actor, "import", typed, PHRASE_IMPORT)
                if refused is not None:
                    return refused
                self._store.write(storage_name, data)
                written = True
                now = self._clock.now()
                repos.proposals.insert(
                    proposal_id=proposal_id,
                    storage_name=storage_name,
                    size_bytes=checked.size,
                    sha256=checked.sha256,
                    declared_mime=checked.mime,
                    imported_by=ctx.user.id,
                    now=now,
                )
                repos.proposals.add_history(
                    proposal_id, None, "IMPORTED", "WEB", actor.user_id, None, now
                )
                self._record(
                    repos,
                    actor,
                    Evt.PROPOSAL_IMPORTED,
                    Res.SUCCESS,
                    proposal_id,
                    None,
                    {"bytes": checked.size, "mime": checked.mime},
                )
        except psycopg.errors.IntegrityConstraintViolation:
            if written:
                self._store.remove(storage_name)
            with self._storage.tx() as repos:
                self._deny(repos, actor, "import", "DATABASE_REFUSED")
            return Outcome("limit", reasons=("DATABASE_REFUSED",))
        except BaseException:
            if written:
                self._store.remove(storage_name)
            raise
        if not written:  # a refusal inside the transaction returned early
            return Outcome("conflict")
        return Outcome("ok", proposal_id)

    # ------------------------------------------------------------------ review
    def review(self, actor: Actor, proposal_id: UUID, notes: str) -> Outcome:
        cleaned = clean_note(notes, lo=1, hi=1000)
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id, for_update=True)
            if row is None:
                return Outcome("not_found")
            if row.state != "VALIDATED":
                self._deny(repos, actor, "review", f"STATE_{row.state}", proposal_id)
                return Outcome("not_allowed", proposal_id, (row.state,))
            if cleaned is None:
                self._deny(repos, actor, "review", "NOTES_INVALID", proposal_id)
                return Outcome("invalid", proposal_id, ("NOTES",))
            repos.proposals.mark_reviewed(
                proposal_id, cleaned, actor.user_id or UUID(int=0), self._clock.now()
            )
            self._transition(repos, actor, row, "REVIEWED", Evt.PROPOSAL_REVIEWED)
        return Outcome("ok", proposal_id)

    # ------------------------------------------------------------------ manual change request
    def create_change_request(
        self,
        ctx: AuthContext,
        actor: Actor,
        proposal_id: UUID,
        typed: str,
        *,
        change_type: str,
        impact: str,
        ceilings_unaffected: bool,
    ) -> Outcome:
        cleaned = clean_note(impact, lo=20, hi=2000)
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id, for_update=True)
            if row is None:
                return Outcome("not_found")
            if row.state != "REVIEWED":
                self._deny(repos, actor, "change_request", f"STATE_{row.state}", proposal_id)
                return Outcome("not_allowed", proposal_id, (row.state,))
            problems = []
            if change_type not in CHANGE_TYPES:
                problems.append("CHANGE_TYPE")
            if cleaned is None:
                problems.append("IMPACT_ASSESSMENT")
            if change_type == "PARAMETER_CHANGE" and not ceilings_unaffected:
                problems.append("CEILINGS_ATTESTATION")
            if problems:
                self._deny(repos, actor, "change_request", problems[0], proposal_id)
                return Outcome("invalid", proposal_id, tuple(problems))
            refused = self._chain(
                repos,
                ctx,
                actor,
                "change_request",
                typed,
                phrase_change_request(proposal_id),
                proposal_id,
            )
            if refused is not None:
                return refused
            assert cleaned is not None  # noqa: S101 - narrowed above
            repos.proposals.set_state(proposal_id, "CHANGE_REQUEST_CREATED")
            self._transition(
                repos,
                actor,
                row,
                "CHANGE_REQUEST_CREATED",
                Evt.PROPOSAL_CHANGE_REQUEST_CREATED,
                {"change_type": change_type},
            )
            repos.proposals.add_change_request(
                uuid4(),
                proposal_id,
                change_type,
                cleaned,
                ceilings_unaffected,
                ctx.user.id,
                self._clock.now(),
            )
        return Outcome("ok", proposal_id)

    # ------------------------------------------------------------------ attestations
    def attest(
        self,
        ctx: AuthContext,
        actor: Actor,
        proposal_id: UUID,
        kind: str,
        *,
        reference: str | None = None,
        report_ids: list[str] | None = None,
    ) -> Outcome:
        """IMPLEMENTED, BACKTESTED or PAPER_VALIDATED: an ADMIN statement about work done elsewhere.
        Nothing is verified except what the database holds (that cited reports exist and fit)."""
        if kind not in ATTEST_KINDS:
            return Outcome("invalid", proposal_id, ("KIND",))
        needs = {
            "IMPLEMENTED": "CHANGE_REQUEST_CREATED",
            "BACKTESTED": "IMPLEMENTED",
            "PAPER_VALIDATED": "BACKTESTED",
        }[kind]
        event = {
            "IMPLEMENTED": Evt.PROPOSAL_IMPLEMENTED,
            "BACKTESTED": Evt.PROPOSAL_BACKTESTED,
            "PAPER_VALIDATED": Evt.PROPOSAL_PAPER_VALIDATED,
        }[kind]
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id, for_update=True)
            if row is None:
                return Outcome("not_found")
            if row.state != needs:
                self._deny(repos, actor, kind.lower(), f"STATE_{row.state}", proposal_id)
                return Outcome("not_allowed", proposal_id, (row.state,))
            ids: list[UUID] = []
            problems: list[str] = []
            if kind == "IMPLEMENTED":
                if reference is None or not _RELEASE_REF.fullmatch(reference):
                    problems.append("RELEASE_REFERENCE")
            else:
                problems, ids = self._check_reports(repos, kind, proposal_id, report_ids or [])
            if problems:
                self._deny(repos, actor, kind.lower(), problems[0], proposal_id)
                return Outcome("invalid", proposal_id, tuple(problems))
            refused = self._chain(repos, ctx, actor, kind.lower(), None, None, proposal_id)
            if refused is not None:
                return refused
            repos.proposals.set_state(proposal_id, kind)
            self._transition(repos, actor, row, kind, event)
            repos.proposals.add_attestation(
                proposal_id,
                kind,
                reference if kind == "IMPLEMENTED" else None,
                ids if kind != "IMPLEMENTED" else None,
                ctx.user.id,
                self._clock.now(),
            )
        return Outcome("ok", proposal_id)

    def _check_reports(
        self, repos: Repos, kind: str, proposal_id: UUID, raw: list[str]
    ) -> tuple[list[str], list[UUID]]:
        if not 1 <= len(raw) <= 5 or len(set(raw)) != len(raw):
            return ["REPORT_IDS"], []
        if not all(_UUID.fullmatch(r) for r in raw):
            return ["REPORT_IDS"], []
        ids = [UUID(r) for r in raw]
        found = repos.proposals.report_kinds(ids)
        if len(found) != len(ids):
            return ["REPORT_UNKNOWN"], []
        allowed = {"BACKTEST", "WALK_FORWARD"} if kind == "BACKTESTED" else {"PAPER_DAILY"}
        if any(found[i][0] not in allowed for i in ids):
            return ["REPORT_KIND"], []
        cr = repos.proposals.change_request(proposal_id)
        attestations = repos.proposals.attestations(proposal_id)
        floor = cr.created_at if kind == "BACKTESTED" and cr else None
        if kind == "PAPER_VALIDATED" and "BACKTESTED" in attestations:
            floor = attestations["BACKTESTED"].attested_at
        if floor is not None and any(found[i][1] < floor for i in ids):
            return ["REPORT_TOO_OLD"], []
        if kind == "PAPER_VALIDATED":
            times = sorted(found[i][1] for i in ids)
            if len(times) < 2 or times[-1] - times[0] < timedelta(days=PAPER_MIN_SPAN_DAYS):
                return ["PAPER_DURATION"], []
        return [], ids

    # ------------------------------------------------------------------ close
    def close(self, actor: Actor, proposal_id: UUID, reason: str) -> Outcome:
        if reason not in CLOSE_REASONS:
            return Outcome("invalid", proposal_id, ("CLOSE_REASON",))
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id, for_update=True)
            if row is None:
                return Outcome("not_found")
            if row.state not in (
                "PAPER_VALIDATED",
                "REJECTED",
                "REVIEWED",
                "CHANGE_REQUEST_CREATED",
            ):
                self._deny(repos, actor, "close", f"STATE_{row.state}", proposal_id)
                return Outcome("not_allowed", proposal_id, (row.state,))
            repos.proposals.mark_closed(proposal_id, reason, self._clock.now())
            self._transition(repos, actor, row, "CLOSED", Evt.PROPOSAL_CLOSED, {"reason": reason})
        return Outcome("ok", proposal_id)
