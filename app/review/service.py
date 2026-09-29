"""Review package use cases for the web tier (ADMIN only; the route enforces the permission).

Sensitive actions follow the full chain: ADMIN permission (route) -> CSRF (app-wide) -> validation
-> exact typed phrase -> fresh single-use reauthentication -> change + audit in one transaction.
A wrong phrase never consumes the reauthentication, and every refusal is itself audited
(`review.denied`). Nothing here calls an LLM, imports a proposal, or touches bot, pair, strategy,
risk, configuration, order, ledger or gate state: it reads and writes review rows and audit events.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final, Literal
from uuid import UUID, uuid4

import psycopg

from app.auth.audit import AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import AuthContext, Clock
from app.pairs.transitions import Actor
from app.review.builder import read_and_verify
from app.review.schema import MAX_PERIOD_DAYS, SCOPES
from app.review.store import PackageStore
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.review_repositories import PackageRow, ReviewSettingsRow

PHRASES: Final = {
    "enable": "ENABLE READ-ONLY REVIEW PACKAGES",
    "disable": "DISABLE READ-ONLY REVIEW PACKAGES",
    "create": "CREATE READ-ONLY REVIEW PACKAGE",
}
MAX_RETAINED: Final = 10
MAX_REQUESTS_PER_HOUR: Final = 3

Kind = Literal[
    "ok",
    "not_found",
    "invalid",
    "phrase_mismatch",
    "reauth_required",
    "conflict",
    "disabled",
    "limit",
    "not_ready",
    "corrupt",
]
Evt = AuditEventType
Res = AuditResult


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    package_id: UUID | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Overview:
    settings: ReviewSettingsRow
    packages: tuple[PackageRow, ...]
    counts: dict[str, int]
    create_blockers: tuple[str, ...]
    reauth_active: bool = False


@dataclass(frozen=True)
class Download:
    outcome: Outcome
    data: bytes = b""
    filename: str = ""
    problems: tuple[str, ...] = field(default=())


def _same(typed: str, expected: str) -> bool:
    return typed.isascii() and hmac.compare_digest(typed.encode("ascii"), expected.encode("ascii"))


class ReviewService:
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
        self._store = PackageStore(settings.review.dir)

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
            target_type="review_package" if target else "review_feature",
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
            repos, actor, Evt.REVIEW_DENIED, Res.DENIED, target, reason, {"attempted": action}
        )

    # ------------------------------------------------------------------ read model
    def overview(self, ctx: AuthContext) -> Overview:
        with self._storage.tx() as repos:
            settings = repos.review.settings()
            packages = tuple(repos.review.recent(25))
            counts = repos.review.counts()
            blockers = self._create_blockers(repos, settings, self._clock.now())
        return Overview(settings, packages, counts, blockers, self._reauth_active(ctx))

    def package(self, package_id: UUID) -> PackageRow | None:
        with self._storage.tx() as repos:
            return repos.review.package(package_id)

    def reauth_active(self, ctx: AuthContext) -> bool:
        return self._reauth_active(ctx)

    def _create_blockers(
        self, repos: Repos, settings: ReviewSettingsRow, now: object
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not settings.enabled:
            return ("FEATURE_DISABLED",)
        counts = repos.review.counts()
        if counts.get("REQUESTED", 0) or counts.get("GENERATING", 0):
            reasons.append("PACKAGE_BEING_BUILT")
        retained = sum(counts.get(s, 0) for s in ("REQUESTED", "GENERATING", "READY", "CORRUPT"))
        if retained >= MAX_RETAINED:
            reasons.append("TOO_MANY_RETAINED")
        recent = [p for p in repos.review.recent(MAX_REQUESTS_PER_HOUR + 1)]
        cutoff = self._clock.now() - timedelta(hours=1)
        if sum(1 for p in recent if p.requested_at > cutoff) >= MAX_REQUESTS_PER_HOUR:
            reasons.append("RATE_LIMITED")
        return tuple(reasons)

    # ------------------------------------------------------------------ enable / disable
    def enable(self, ctx: AuthContext, actor: Actor, typed: str, retention_days: int) -> Outcome:
        if not 1 <= retention_days <= 90:
            return Outcome("invalid", reasons=("RETENTION_DAYS",))
        with self._storage.tx() as repos:
            current = repos.review.settings(for_update=True)
            if current.enabled:
                self._deny(repos, actor, "enable", "ALREADY_ENABLED")
                return Outcome("conflict", reasons=("ALREADY_ENABLED",))
            refused = self._chain(repos, ctx, actor, "enable", typed)
            if refused is not None:
                return refused
            repos.review.set_settings(True, retention_days, self._clock.now())
            self._record(
                repos,
                actor,
                Evt.REVIEW_ENABLED,
                Res.SUCCESS,
                None,
                None,
                {"retention_days": retention_days},
            )
        return Outcome("ok")

    def disable(self, ctx: AuthContext, actor: Actor, typed: str) -> Outcome:
        with self._storage.tx() as repos:
            current = repos.review.settings(for_update=True)
            if not current.enabled:
                self._deny(repos, actor, "disable", "ALREADY_DISABLED")
                return Outcome("conflict", reasons=("ALREADY_DISABLED",))
            refused = self._chain(repos, ctx, actor, "disable", typed)
            if refused is not None:
                return refused
            repos.review.set_settings(False, current.retention_days, self._clock.now())
            self._record(repos, actor, Evt.REVIEW_DISABLED, Res.SUCCESS, None)
        return Outcome("ok")

    def _chain(
        self, repos: Repos, ctx: AuthContext, actor: Actor, action: str, typed: str
    ) -> Outcome | None:
        """Phrase, then fresh single-use reauthentication. None means the chain passed."""
        if not _same(typed, PHRASES[action]):
            self._deny(repos, actor, action, "PHRASE_MISMATCH")
            return Outcome("phrase_mismatch")
        if not self._consume_reauth(repos, ctx):
            self._deny(repos, actor, action, "REAUTH_REQUIRED")
            return Outcome("reauth_required")
        return None

    # ------------------------------------------------------------------ create (request)
    def validate_request(
        self, start: date, end: date, scope: list[str]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(canonical scope, problems)."""
        problems: list[str] = []
        today = self._clock.now().date()
        if end < start:
            problems.append("PERIOD_ORDER")
        if end > today:
            problems.append("PERIOD_IN_FUTURE")
        limit = min(MAX_PERIOD_DAYS, self._settings.review.max_period_days)
        if (end - start).days > limit:
            problems.append("PERIOD_TOO_LONG")
        chosen = tuple(s for s in SCOPES if s in set(scope))
        if not chosen or len(set(scope)) != len(chosen) or len(scope) != len(set(scope)):
            problems.append("SCOPE")  # empty, unknown or repeated items
        return chosen, tuple(problems)

    def request(
        self, ctx: AuthContext, actor: Actor, typed: str, start: date, end: date, scope: list[str]
    ) -> Outcome:
        chosen, problems = self.validate_request(start, end, scope)
        if problems:
            return Outcome("invalid", reasons=problems)
        package_id = uuid4()
        try:
            with self._storage.tx() as repos:
                settings = repos.review.settings(for_update=True)
                if not settings.enabled:
                    self._deny(repos, actor, "create", "FEATURE_DISABLED")
                    return Outcome("disabled")
                blockers = self._create_blockers(repos, settings, None)
                if blockers:
                    self._deny(repos, actor, "create", blockers[0])
                    return Outcome("limit", reasons=blockers)
                refused = self._chain(repos, ctx, actor, "create", typed)
                if refused is not None:
                    return refused
                repos.review.insert_request(
                    package_id,
                    start,
                    end,
                    list(chosen),
                    settings.retention_days,
                    ctx.user.id,
                    self._clock.now(),
                )
                self._record(
                    repos,
                    actor,
                    Evt.REVIEW_REQUESTED,
                    Res.SUCCESS,
                    package_id,
                    None,
                    {"period_days": (end - start).days + 1, "scopes": len(chosen)},
                )
        except psycopg.errors.IntegrityConstraintViolation:
            with self._storage.tx() as repos:
                self._deny(repos, actor, "create", "DATABASE_REFUSED")
            return Outcome("limit", reasons=("DATABASE_REFUSED",))
        return Outcome("ok", package_id)

    # ------------------------------------------------------------------ verify / download
    def verify(self, actor: Actor, package_id: UUID) -> Download:
        return self._read(actor, package_id, download=False)

    def download(self, actor: Actor, package_id: UUID) -> Download:
        return self._read(actor, package_id, download=True)

    def _read(self, actor: Actor, package_id: UUID, *, download: bool) -> Download:
        action = "download" if download else "verify"
        with self._storage.tx() as repos:
            row = repos.review.package(package_id, for_update=True)
            if row is None:
                return Download(Outcome("not_found"))
            if not repos.review.settings().enabled:
                self._deny(repos, actor, action, "FEATURE_DISABLED", package_id)
                return Download(Outcome("disabled", package_id))
            if row.state != "READY":
                self._deny(repos, actor, action, f"STATE_{row.state}", package_id)
                return Download(Outcome("not_ready", package_id, (row.state,)))
            found, blob = read_and_verify(row, self._store, self._settings)
            problems = tuple(found)
            if problems:
                repos.review.mark_corrupt(package_id, self._clock.now())
                self._record(
                    repos,
                    actor,
                    Evt.REVIEW_CORRUPT,
                    Res.FAILURE,
                    package_id,
                    problems[0],
                    {"problems": len(problems), "during": action},
                )
                return Download(Outcome("corrupt", package_id), problems=problems)
            self._record(
                repos,
                actor,
                Evt.REVIEW_DOWNLOADED if download else Evt.REVIEW_VERIFIED,
                Res.SUCCESS,
                package_id,
                None,
                {"bytes": row.size_bytes} if download else None,
            )
        name = f"review-package-{package_id}.zip" if download else ""
        return Download(Outcome("ok", package_id), blob if download else b"", name)
