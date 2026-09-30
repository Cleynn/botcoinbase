"""The ADMIN control workflow for the bot: PAUSE, RESUME, CANCEL KNOWN, ACTIVATE KILL SWITCH.

Flow (request -> authz -> CSRF -> reauth -> typed confirm -> audit -> internal command -> outcome
audit): authorization and CSRF happen in the web layer before this service is called. Here, in
order: the exact phrase, then a fresh single-use reauthentication (a wrong phrase never spends it),
then a `bot.control_requested` audit event committed on its own, then the internal command (a
compare-and-set update of the control row, or an enqueue of a CANCEL_KNOWN command), then an outcome
audit event (`bot.paused`, `bot.resumed`, ... or `bot.control_denied` with a reason).

This module is database-only code. It cannot reach an exchange, create an order or sell anything:
the kill switch pauses the bot and enqueues a cancel command for the host; it never trades.
RESUME needs a current successful reconciliation, which only the host can produce.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import psycopg

from app.auth.audit import AuditWriter
from app.capital.funds import Funds, FundsUnavailable, paper_funds, usable_quote
from app.capital.profiles import PROFILES
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import AuthContext, Clock
from app.pairs.transitions import Actor
from app.safety.live_gate import panel
from app.safety.types import GateResult
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.safety_repositories import CommandRow, ControlRow, FindingRow, RunRow

PHRASES: Final[dict[str, str]] = {
    "pause": "PAUSE BOT",
    "resume": "RESUME BOT AFTER RECONCILIATION",
    "cancel_known": "CANCEL KNOWN BOT ORDERS",
    "kill": "ACTIVATE KILL SWITCH",
}
ACTIONS: Final = tuple(PHRASES)
# Choosing the capital profile for a mode (never an order): same flow, one phrase per mode.
PROFILE_PHRASES: Final[dict[str, str]] = {
    "paper": "UPDATE CAPITAL LIMITS FOR PAPER MODE",
    "live": "UPDATE CAPITAL LIMITS FOR LIVE MODE",
}
DB_RECONCILE_WINDOW_SECONDS: Final = 300  # the database guard's hard limit

Kind = Literal[
    "ok", "phrase_mismatch", "reauth_required", "not_allowed", "conflict", "invalid", "not_found"
]


@dataclass(frozen=True)
class Outcome:
    kind: Kind
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapitalOverview:
    """What the paper ledger holds and what the selected PAPER profile lets the bot deploy. The
    venue's own USDC is read only by the host (through the read adapter), never by the web tier."""

    available: str | None
    reserved: str | None
    inventory_cost: str | None
    usable: str | None
    note: str | None


@dataclass(frozen=True)
class Overview:
    control: ControlRow
    run: RunRow | None
    findings: tuple[FindingRow, ...]
    counts: dict[str, int]
    history: tuple[dict[str, Any], ...]
    commands: tuple[CommandRow, ...]
    resume_blockers: tuple[str, ...]
    gate: GateResult
    recent_blocks: tuple[dict[str, Any], ...]
    api_calls_1h: int
    api_failures_1h: int
    reauth_active: bool = False
    capital: CapitalOverview | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _same(typed: str, expected: str) -> bool:
    return typed.isascii() and hmac.compare_digest(typed.encode("ascii"), expected.encode("ascii"))


class ControlService:
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

    # ------------------------------------------------------------------ audit
    def _record(
        self,
        repos: Repos,
        actor: Actor,
        event: Evt,
        result: Res,
        reason: str | None = None,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=actor.audit,
            target_type="bot",
            target_id="bot",
            reason=reason,
            client_tag=actor.client_tag,
            request_id=actor.request_id,
            detail=detail,
        )

    def _deny(
        self, repos: Repos, actor: Actor, action: str, reason: str, detail: str | None = None
    ) -> None:
        self._record(
            repos,
            actor,
            Evt.BOT_CONTROL_DENIED,
            Res.DENIED,
            reason,
            {"attempted": action, "why": detail},
        )

    # ------------------------------------------------------------------ read model
    def reauth_active(self, ctx: AuthContext) -> bool:
        return self._reauth_active(ctx)

    def overview(self, ctx: AuthContext | None = None) -> Overview:
        now = self._clock.now()
        with self._storage.tx() as repos:
            run = repos.safety.latest_run()
            findings = tuple(repos.safety.findings(run.id)) if run else ()
            calls, bad = repos.safety.api_calls_since(now - timedelta(hours=1))
            return Overview(
                control=repos.safety.control(),
                run=run,
                findings=findings,
                counts=repos.safety.counts_by_state(),
                history=tuple(repos.safety.history(15)),
                commands=tuple(repos.safety.recent_commands(8)),
                resume_blockers=self.resume_blockers(repos, now),
                gate=panel(self._settings),
                recent_blocks=tuple(repos.safety.recent_block_reasons(8)),
                api_calls_1h=calls,
                api_failures_1h=bad,
                reauth_active=self._reauth_active(ctx) if ctx is not None else False,
                capital=self._capital(repos, now),
            )

    def _capital(self, repos: Repos, now: datetime) -> CapitalOverview:
        control = repos.safety.control()
        profile = PROFILES.get(control.paper_profile)
        cash = repos.paper.cash()
        if profile is None:
            return CapitalOverview(None, None, None, None, "the selected profile is unknown")
        if not repos.paper.has_deposit():
            return CapitalOverview(None, None, None, None, "no paper deposit yet")
        try:
            funds: Funds = paper_funds(
                cash=cash,
                reserved=repos.paper.reserved(),
                inventory_cost=repos.paper.total_position()[1],
                now=now,
            )
        except FundsUnavailable:
            return CapitalOverview(None, None, None, None, "the paper ledger is inconsistent")
        return CapitalOverview(
            str(funds.available),
            str(funds.hold),
            str(funds.inventory_cost),
            str(usable_quote(funds, profile)),
            None,
        )

    def resume_blockers(self, repos: Repos, now: datetime) -> tuple[str, ...]:
        """Everything that forbids RESUME right now (the database enforces the same rules)."""
        c = repos.safety.control()
        if c.bot_state == "RUNNING":
            return ("ALREADY_RUNNING",)
        out: list[str] = []
        if c.kill_switch == "ACTIVE":
            out.append("KILL_SWITCH_ACTIVE")
        if c.recovery_state != "COMPLETE":
            out.append("RECOVERY_INCOMPLETE")
        if (
            c.breaker_state == "OPEN"
            and c.breaker_cooldown_until
            and c.breaker_cooldown_until > now
        ):
            out.append("BREAKER_COOLDOWN")
        limit = min(self._settings.safety.reconcile_max_age_seconds, DB_RECONCILE_WINDOW_SECONDS)
        run = repos.safety.latest_run()
        if run is None:
            out.append("RECONCILIATION_MISSING")
        elif run.outcome == "FAILED":
            out.append("RECONCILIATION_FAILED")
        elif run.outcome == "MISMATCH":
            out.append("RECONCILIATION_MISMATCH")
        elif (now - run.finished_at).total_seconds() > limit:
            out.append("RECONCILIATION_STALE")
        elif (
            c.breaker_state == "OPEN"
            and c.breaker_opened_at
            and run.finished_at <= c.breaker_opened_at
        ):
            out.append("RECONCILIATION_BEFORE_TRIP")
        if repos.safety.count_state("UNKNOWN") > 0:
            out.append("UNKNOWN_ATTEMPT")
        return tuple(out)

    # ------------------------------------------------------------------ the workflow
    def _confirmed(
        self,
        ctx: AuthContext,
        actor: Actor,
        action: str,
        expected: str,
        typed: str,
        detail: dict[str, str | int | bool | None],
    ) -> Outcome | None:
        """Steps 1-3 of the workflow. None means: proceed to the internal command."""
        with self._storage.tx() as repos:
            if not _same(typed or "", expected):
                self._deny(repos, actor, action, "PHRASE_MISMATCH")
                return Outcome("phrase_mismatch")
            if not self._consume_reauth(repos, ctx):
                self._deny(repos, actor, action, "REAUTH_REQUIRED")
                return Outcome("reauth_required")
            self._record(
                repos, actor, Evt.BOT_CONTROL_REQUESTED, Res.SUCCESS, action.upper(), detail
            )
        return None

    def execute(self, ctx: AuthContext, actor: Actor, action: str, typed: str) -> Outcome:
        if action not in PHRASES:
            return Outcome("invalid", ("ACTION",))
        # 1. phrase, 2. fresh reauth, 3. audit of the accepted request (its own transaction)
        refused = self._confirmed(ctx, actor, action, PHRASES[action], typed, {"action": action})
        if refused is not None:
            return refused
        # 4. the internal command, 5. its outcome audit
        try:
            with self._storage.tx() as repos:
                return self._command(repos, actor, action)
        except psycopg.errors.IntegrityConstraintViolation:
            with self._storage.tx() as repos:
                self._deny(repos, actor, action, "GUARD_REFUSED")
            return Outcome("not_allowed", ("GUARD_REFUSED",))

    def set_profile(
        self, ctx: AuthContext, actor: Actor, mode: str, profile: str, typed: str
    ) -> Outcome:
        """Choose the capital profile for PAPER or LIVE. Never creates, changes or sells an order.
        A profile is only ever chosen while the bot is PAUSED; choosing one for LIVE leaves the
        live gate exactly as blocked as before."""
        if mode not in PROFILE_PHRASES:
            return Outcome("invalid", ("MODE",))
        if profile not in PROFILES:  # checked first: a bad request never spends the reauth
            return Outcome("invalid", ("PROFILE",))
        action = f"{mode}_profile"
        detail: dict[str, str | int | bool | None] = {"mode": mode, "profile": profile}
        refused = self._confirmed(ctx, actor, action, PROFILE_PHRASES[mode], typed, detail)
        if refused is not None:
            return refused
        try:
            with self._storage.tx() as repos:
                return self._profile_command(repos, actor, action, mode, profile)
        except psycopg.errors.IntegrityConstraintViolation:
            with self._storage.tx() as repos:
                self._deny(repos, actor, action, "GUARD_REFUSED")
            return Outcome("not_allowed", ("GUARD_REFUSED",))

    def _profile_command(
        self, repos: Repos, actor: Actor, action: str, mode: str, profile: str
    ) -> Outcome:
        now = self._clock.now()
        control = repos.safety.control(for_update=True)
        if control.bot_state != "PAUSED":
            self._deny(repos, actor, action, "BOT_MUST_BE_PAUSED")
            return Outcome("not_allowed", ("BOT_MUST_BE_PAUSED",))
        current = control.paper_profile if mode == "paper" else control.live_profile
        if current == profile:
            self._deny(repos, actor, action, "PROFILE_UNCHANGED")
            return Outcome("not_allowed", ("PROFILE_UNCHANGED",))
        if not repos.safety.update_control(
            control.version,
            now,
            last_change_reason=f"{mode.upper()}_PROFILE_CHANGE",
            **{f"{mode}_profile": profile},
        ):
            return self._lost_race(repos, actor, action)
        self._record(
            repos,
            actor,
            Evt.BOT_PROFILE_CHANGED,
            Res.SUCCESS,
            f"{mode.upper()}_PROFILE_CHANGE",
            {"mode": mode, "from": current, "to": profile, "live_gate": "BLOCKED"},
        )
        return Outcome("ok")

    def _command(self, repos: Repos, actor: Actor, action: str) -> Outcome:
        now = self._clock.now()
        control = repos.safety.control(for_update=True)
        if action == "pause":
            if control.bot_state != "RUNNING":
                self._deny(repos, actor, action, "ALREADY_PAUSED")
                return Outcome("not_allowed", ("ALREADY_PAUSED",))
            if not repos.safety.update_control(
                control.version, now, bot_state="PAUSED", last_change_reason="OPERATOR_PAUSE"
            ):
                return self._lost_race(repos, actor, action)
            self._record(repos, actor, Evt.BOT_PAUSED, Res.SUCCESS, "OPERATOR_PAUSE")
            return Outcome("ok")
        if action == "kill":
            if control.kill_switch == "ACTIVE":
                self._deny(repos, actor, action, "ALREADY_ACTIVE")
                return Outcome("not_allowed", ("ALREADY_ACTIVE",))
            if not repos.safety.update_control(
                control.version,
                now,
                bot_state="PAUSED",
                kill_switch="ACTIVE",
                kill_reason="OPERATOR_KILL",
                kill_activated_at=now,
                last_change_reason="OPERATOR_KILL",
            ):
                return self._lost_race(repos, actor, action)
            queued = repos.safety.open_command() is None
            if queued:
                repos.safety.add_command(
                    uuid4(), "KILL_SWITCH", actor.audit.user_id or UUID(int=0), now
                )
            self._record(
                repos,
                actor,
                Evt.BOT_KILL_ACTIVATED,
                Res.SUCCESS,
                "OPERATOR_KILL",
                {"cancel_queued": queued},
            )
            return Outcome("ok")
        if action == "cancel_known":
            if control.bot_state != "PAUSED":
                self._deny(repos, actor, action, "BOT_MUST_BE_PAUSED")
                return Outcome("not_allowed", ("BOT_MUST_BE_PAUSED",))
            if repos.safety.open_command() is not None:
                self._deny(repos, actor, action, "COMMAND_IN_PROGRESS")
                return Outcome("conflict", ("COMMAND_IN_PROGRESS",))
            repos.safety.add_command(uuid4(), "OPERATOR", actor.audit.user_id or UUID(int=0), now)
            self._record(repos, actor, Evt.BOT_CANCEL_REQUESTED, Res.SUCCESS, "OPERATOR_CANCEL")
            return Outcome("ok")
        # resume
        blockers = self.resume_blockers(repos, now)
        if blockers:
            self._deny(repos, actor, action, blockers[0], ",".join(blockers)[:190])
            return Outcome("not_allowed", blockers)
        changes: dict[str, Any] = {"bot_state": "RUNNING", "last_change_reason": "OPERATOR_RESUME"}
        if control.breaker_state == "OPEN":
            changes.update(
                breaker_state="CLOSED",
                breaker_reason=None,
                breaker_opened_at=None,
                breaker_cooldown_until=None,
            )
        if not repos.safety.update_control(control.version, now, **changes):
            return self._lost_race(repos, actor, action)
        self._record(
            repos,
            actor,
            Evt.BOT_RESUMED,
            Res.SUCCESS,
            "OPERATOR_RESUME",
            {"breaker_closed": control.breaker_state == "OPEN"},
        )
        return Outcome("ok")

    def _lost_race(self, repos: Repos, actor: Actor, action: str) -> Outcome:
        self._deny(repos, actor, action, "STATE_CHANGED")
        return Outcome("conflict", ("STATE_CHANGED",))
