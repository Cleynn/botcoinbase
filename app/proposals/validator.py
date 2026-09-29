"""Host-side validation and purge of proposals (runs as td_ctl in `batch`; never in the web tier).

IMPORTED -> VALIDATING -> VALIDATED | REJECTED. This is the only place proposal bytes are decoded
and parsed. It reads the stored file once, re-checks its SHA-256 and the byte-level rules, parses
it into the strict schema, checks the linked review package and every evidence reference against the
database, applies the policy triage and the risk assessment, and records the result. It writes only
proposal rows, history and audit events: nothing else in the system can change through it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.pairs.runner import HOST_ACTOR
from app.proposals import intake, policy, risk, schema
from app.proposals.store import ProposalStore, StoreError
from app.storage.database import Storage
from app.storage.proposal_repositories import ProposalRow
from app.storage.repositories import Repos
from app.storage.review_repositories import PackageRow as ReviewPackage

Evt = AuditEventType
Res = AuditResult


@dataclass(frozen=True)
class ValidationResult:
    proposal_id: UUID
    state: str
    rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupResult:
    purged: int
    orphans: int


class ProposalValidator:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._store = ProposalStore(settings.proposals.dir)
        self._audit = AuditWriter(clock)

    def _record(
        self,
        repos: Repos,
        event: AuditEventType,
        result: AuditResult,
        target: UUID | str | None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=HOST_CLI_ACTOR,
            target_type="proposal",
            target_id=target,
            reason=reason,
            client_tag=HOST_ACTOR.client_tag,
            request_id=HOST_ACTOR.request_id,
            detail=detail,
        )

    # ------------------------------------------------------------------ validate
    def run(self) -> list[ValidationResult]:
        with self._storage.tx() as repos:
            pending = [p.id for p in repos.proposals.in_state("IMPORTED")]
        return [self._validate_one(pid) for pid in pending]

    def _validate_one(self, proposal_id: UUID) -> ValidationResult:
        now = self._clock.now()
        with self._storage.tx() as repos:
            row = repos.proposals.get(proposal_id, for_update=True)
            if row is None or row.state != "IMPORTED":
                return ValidationResult(proposal_id, row.state if row else "MISSING")
            repos.proposals.mark_validating(proposal_id)
            repos.proposals.add_history(
                proposal_id, "IMPORTED", "VALIDATING", "HOST", None, None, now
            )
            self._record(repos, Evt.PROPOSAL_VALIDATING, Res.SUCCESS, proposal_id)
        try:
            outcome = self._assess(row)
        except Exception:  # noqa: BLE001 - never leave a proposal stuck in VALIDATING
            outcome = _Outcome("REJECTED", ["VALIDATOR_ERROR"], [], None, None, None)
        return self._finish(row, outcome)

    def _assess(self, row: ProposalRow) -> _Outcome:
        limits = self._settings.proposals
        try:
            data = self._store.read(row.storage_name, limits.max_bytes)
        except StoreError as exc:
            return _Outcome("REJECTED", [f"FILE_{exc.code}"], [], None, None, None)
        if hashlib.sha256(data).hexdigest() != row.sha256:
            return _Outcome("REJECTED", ["FILE_CHECKSUM_MISMATCH"], [], None, None, None)
        try:
            checked = intake.check_bytes(row.declared_mime, data, max_bytes=limits.max_bytes)
            parsed = schema.parse_text(checked.text, limits.max_depth)
        except intake.IntakeRejected as exc:
            return _Outcome("REJECTED", [exc.code], [], None, None, None)
        except schema.ProposalRejected as exc:
            return _Outcome("REJECTED", list(exc.rules), [], None, None, None)
        with self._storage.tx() as repos:
            package = repos.review.package(UUID(parsed.linked_package_id))
            link_rules = self._check_links(repos, parsed, package)
        findings = policy.evaluate(parsed)
        rules = link_rules + sorted({f.rule for f in findings})
        assessment = risk.assess(parsed, blocked=bool(rules))
        state = "REJECTED" if rules else "VALIDATED"
        linked = package.id if package is not None else None
        return _Outcome(state, rules, [f.as_json() for f in findings], parsed, assessment, linked)

    def _check_links(
        self, repos: Repos, parsed: schema.Parsed, package: ReviewPackage | None
    ) -> list[str]:
        rules: list[str] = []
        listed: set[str] = {"manifest.json"}
        if package is None:
            rules.append("LINK_PACKAGE_UNKNOWN")
        else:
            if package.package_sha256 != parsed.linked_package_sha256:
                rules.append("LINK_PACKAGE_HASH_MISMATCH")
            if package.state not in ("READY", "EXPIRED"):
                rules.append("LINK_PACKAGE_STATE")
            listed |= {str(f.get("path")) for f in (package.files or [])}
        for ref in parsed.evidence:
            path = schema.EVIDENCE_PATH.fullmatch(ref)
            if path:
                if path.group("path") not in listed and "EVIDENCE_NOT_IN_PACKAGE" not in rules:
                    rules.append("EVIDENCE_NOT_IN_PACKAGE")
                continue
            ident = schema.EVIDENCE_ID.fullmatch(ref)
            if ident:
                table = {
                    "report": "reports",
                    "snapshot": "dataset_snapshots",
                    "backtest_run": "backtest_runs",
                }[ident.group("kind")]
                if not repos.proposals.exists(table, UUID(ident.group("id"))) and (
                    "EVIDENCE_UNKNOWN_ID" not in rules
                ):
                    rules.append("EVIDENCE_UNKNOWN_ID")
        return rules

    def _finish(self, row: ProposalRow, outcome: _Outcome) -> ValidationResult:
        now = self._clock.now()
        parsed = outcome.parsed
        with self._storage.tx() as repos:
            repos.proposals.finish_validation(
                row.id,
                state=outcome.state,
                proposal_ref=parsed.proposal_id if parsed else None,
                category=parsed.category if parsed else None,
                linked_package_id=outcome.linked_id,
                linked_package_sha256=parsed.linked_package_sha256 if parsed else None,
                parsed=parsed.as_json() if parsed else None,
                findings=outcome.findings,
                risk=outcome.risk,
                reject_rules=outcome.rules if outcome.state == "REJECTED" else [],
                now=now,
            )
            repos.proposals.add_history(
                row.id,
                "VALIDATING",
                outcome.state,
                "HOST",
                None,
                (outcome.rules[0].split(":")[0] if outcome.rules else None),
                now,
            )
            event = (
                Evt.PROPOSAL_VALIDATED if outcome.state == "VALIDATED" else Evt.PROPOSAL_REJECTED
            )
            self._record(
                repos,
                event,
                Res.SUCCESS if outcome.state == "VALIDATED" else Res.FAILURE,
                row.id,
                outcome.rules[0].split(":")[0] if outcome.rules else None,
                {"rules": len(outcome.rules), "findings": len(outcome.findings)},
            )
        return ValidationResult(row.id, outcome.state, tuple(outcome.rules))

    # ------------------------------------------------------------------ retention cleanup
    def cleanup(self) -> CleanupResult:
        """Remove the bytes of CLOSED or REJECTED proposals past retention; remove orphan files."""
        cutoff = self._clock.now() - timedelta(days=self._settings.proposals.retention_days)
        with self._storage.tx() as repos:
            due = repos.proposals.purgeable(cutoff)
        purged = 0
        for row in due:
            existed = self._store.remove(row.storage_name)
            with self._storage.tx() as repos:
                repos.proposals.mark_content_removed(row.id, self._clock.now())
                self._record(
                    repos,
                    Evt.PROPOSAL_CLEANUP,
                    Res.SUCCESS,
                    row.id,
                    "CONTENT_REMOVED",
                    {"file_existed": existed},
                )
            purged += 1
        with self._storage.tx() as repos:
            keep = repos.proposals.storage_names()
        orphans = 0
        for name in self._store.stray_files(keep):
            self._store.remove_stray(name)
            orphans += 1
        if orphans:
            with self._storage.tx() as repos:
                self._record(
                    repos,
                    Evt.PROPOSAL_CLEANUP,
                    Res.SUCCESS,
                    None,
                    "ORPHANS_REMOVED",
                    {"files": orphans},
                )
        return CleanupResult(purged, orphans)


@dataclass(frozen=True)
class _Outcome:
    state: str
    rules: list[str]
    findings: list[dict[str, str]]
    parsed: schema.Parsed | None
    risk: dict[str, Any] | None
    linked_id: UUID | None
