"""Startup recovery: reconcile before any action.

Every start of the host process begins here. The bot is set PAUSED with recovery INCOMPLETE and a
new boot id; attempts that were AUTHORIZED but never marked as sent are closed as NOT_SENT; attempts
marked SUBMITTING by an older boot become UNKNOWN (their outcome is decided only by reconciliation);
then a STARTUP reconciliation runs. Recovery is COMPLETE only when that run is OK and no attempt is
UNKNOWN. Recovery never resumes the bot: RESUME is a separate ADMIN action.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.pairs.runner import HOST_ACTOR
from app.safety.reconciler import Reconciler
from app.storage.database import Storage
from app.storage.repositories import Repos


@dataclass(frozen=True)
class RecoveryResult:
    boot_id: str
    complete: bool
    blockers: tuple[str, ...]
    run_outcome: str


class RecoveryService:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        reconciler: Reconciler | None,
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._reconciler = reconciler
        self._audit = AuditWriter(clock)

    def _record(
        self, repos: Repos, event: Evt, result: Res, reason: str | None = None,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:  # fmt: skip
        self._audit.record(
            repos,
            event,
            result,
            actor=HOST_CLI_ACTOR,
            target_type="bot",
            target_id="bot",
            reason=reason,
            client_tag=HOST_ACTOR.client_tag,
            request_id=HOST_ACTOR.request_id,
            detail=detail,
        )

    def run(self, boot_id: str | None = None) -> RecoveryResult:
        """Recover this process. `boot_id` is the identity the process will then submit with: the
        database refuses an attempt whose boot id is not the one recorded here, so a process
        that did not run recovery (or was superseded by a newer one) cannot authorize orders."""
        boot_id = boot_id or str(uuid4())
        now = self._clock.now()
        with self._storage.tx() as repos:
            control = repos.safety.control(for_update=True)
            changes: dict[str, object] = {
                "boot_id": boot_id,
                "last_change_reason": "RECOVERY_START",
            }
            if control.recovery_state == "COMPLETE" or control.bot_state == "RUNNING":
                changes.update(
                    bot_state="PAUSED", recovery_state="INCOMPLETE", recovery_completed_at=None
                )
            repeat = control.boot_id == boot_id and len(changes) == 2  # the same start again
            if not repeat:
                repos.safety.update_control(control.version, now, **changes)
            closed = unknown = 0
            for venue in ("PAPER", "FAKE", "COINBASE"):
                for attempt in repos.safety.attempts_in(("AUTHORIZED", "SUBMITTING"), venue):
                    if attempt.state == "AUTHORIZED":
                        closed += repos.safety.transition(
                            attempt.id, ("AUTHORIZED",), "REJECTED", now, failure_code="NOT_SENT"
                        )
                    else:
                        unknown += repos.safety.transition(
                            attempt.id, ("SUBMITTING",), "UNKNOWN", now, failure_code="RECOVERED"
                        )
            self._record(
                repos,
                Evt.BOT_RECOVERY_STARTED,
                Res.SUCCESS,
                "RECOVERY_START",
                {"not_sent": closed, "unknown": unknown},
            )
        blockers: list[str] = []
        if self._reconciler is None:  # no exchange reader in this deployment: cannot complete
            return RecoveryResult(boot_id, False, ("NO_EXCHANGE_READER",), "NONE")
        run = self._reconciler.run("STARTUP")
        if run.outcome != "OK":
            blockers.append(
                "RECONCILIATION_FAILED" if run.outcome == "FAILED" else "RECONCILIATION_MISMATCH"
            )
        with self._storage.tx() as repos:
            if repos.safety.count_state("UNKNOWN") > 0:
                blockers.append("UNKNOWN_ATTEMPT")
            if not blockers:
                control = repos.safety.control(for_update=True)
                done = self._clock.now()
                repos.safety.update_control(
                    control.version,
                    done,
                    recovery_state="COMPLETE",
                    recovery_completed_at=done,
                    last_change_reason="RECOVERY_DONE",
                )
                self._record(repos, Evt.BOT_RECOVERY_COMPLETED, Res.SUCCESS, "RECOVERY_DONE")
        return RecoveryResult(boot_id, not blockers, tuple(blockers), run.outcome)
