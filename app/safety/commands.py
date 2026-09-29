"""Runs the CANCEL_KNOWN command the ADMIN dashboard (or the kill switch) queued. Host side.

It cancels ONLY orders the bot itself created and knows the exchange id of, one per-order result at
a time (an HTTP success is never taken as a cancellation: the attempt becomes CANCEL_REQUESTED and
only reconciliation may mark it CANCELLED). Foreign orders are never touched. There is no cancel-all
call and no market order anywhere, so nothing here can sell anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.exchange.errors import ExchangeError
from app.exchange.gateway import ExecutionGateway
from app.pairs.runner import HOST_ACTOR
from app.storage.database import Storage
from app.storage.repositories import Repos

BATCH: Final = 100  # CB-10: at most 100 order ids per cancel call


@dataclass(frozen=True)
class CommandOutcome:
    state: str
    counts: dict[str, int]
    failure_code: str | None


class CommandRunner:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        gateways: Mapping[str, ExecutionGateway],
        paper_cancel: Callable[[str], int] | None = None,
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._gateways, self._paper_cancel = gateways, paper_cancel
        self._audit = AuditWriter(clock)

    def _record(
        self, repos: Repos, event: Evt, result: Res, reason: str | None, detail: dict[str, int]
    ) -> None:
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

    def run_pending(self) -> CommandOutcome | None:
        with self._storage.tx() as repos:
            command = repos.safety.pending_command()
            if command is None:
                return None
            repos.safety.start_command(command.id)
            targets = {
                venue: [
                    (a.id, a.exchange_order_id)
                    for a in repos.safety.attempts_in(("WORKING", "CANCEL_REQUESTED"), venue)
                    if a.exchange_order_id
                ]
                for venue in ("FAKE",)
            }
            no_id = sum(
                1
                for a in repos.safety.attempts_in(("WORKING", "CANCEL_REQUESTED", "UNKNOWN"))
                if not a.exchange_order_id
            )
        counts = {"requested": 0, "queued": 0, "rejected": 0, "unknown": 0, "unresolved": no_id}
        failure: str | None = None
        for venue, items in targets.items():
            if not items:
                continue
            gateway = self._gateways.get(venue)
            if gateway is None:
                counts["unknown"] += len(items)
                failure = "GATEWAY_UNAVAILABLE"
                continue
            for i in range(0, len(items), BATCH):
                chunk = items[i : i + BATCH]
                ids = [order_id for _, order_id in chunk if order_id]
                counts["requested"] += len(ids)
                try:
                    results = gateway.cancel(ids)
                    ok, code = True, None
                except ExchangeError as exc:
                    results, ok, code = {}, False, exc.code
                except Exception:  # noqa: BLE001
                    results, ok, code = {}, False, "UNEXPECTED_RESPONSE"
                now = self._clock.now()
                with self._storage.tx() as repos:
                    repos.safety.add_api_event(venue, "cancel", ok, code, now)
                    for attempt_id, order_id in chunk:
                        result = results.get(order_id or "")
                        if result is None:
                            counts["unknown"] += 1
                        elif result.outcome == "CANCEL_QUEUED":
                            counts["queued"] += 1
                            repos.safety.transition(
                                attempt_id, ("WORKING",), "CANCEL_REQUESTED", now
                            )
                        elif result.outcome == "REJECTED":
                            counts["rejected"] += 1
                        else:
                            counts["unknown"] += 1
                if not ok:
                    failure = code or "CANCEL_FAILED"
        if self._paper_cancel is not None:
            counts["paper_cancelled"] = self._paper_cancel("COMMAND")
        if counts["unknown"] and failure is None:
            failure = "CANCEL_UNCONFIRMED"
        state = "FAILED" if failure else "DONE"
        with self._storage.tx() as repos:
            repos.safety.finish_command(command.id, state, self._clock.now(), dict(counts), failure)
            self._record(
                repos,
                Evt.BOT_CANCEL_FAILED if failure else Evt.BOT_CANCEL_COMPLETED,
                Res.FAILURE if failure else Res.SUCCESS,
                failure or "CANCEL_DONE",
                counts,
            )
        return CommandOutcome(state, counts, failure)
