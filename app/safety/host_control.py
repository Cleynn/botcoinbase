"""Host-side control: open the breaker, pause, activate or release the kill switch.

Only the host role can open the breaker, mark recovery or release the kill switch (the database
enforces it). Releasing the kill switch never resumes the bot: it leaves the bot PAUSED and marks
recovery INCOMPLETE, so a fresh reconciliation and the ADMIN resume chain must follow.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.pairs.runner import HOST_ACTOR
from app.safety.breaker import TRIP_REASONS
from app.safety.control import Outcome
from app.storage.database import Storage
from app.storage.repositories import Repos

RELEASE_PHRASE: Final = "RELEASE KILL SWITCH"
HOST_KILL_PHRASE: Final = "ACTIVATE KILL SWITCH"


class HostControl:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._audit = AuditWriter(clock)

    def _record(
        self,
        repos: Repos,
        event: Evt,
        result: Res,
        reason: str | None,
        detail: dict[str, str | int | bool | None] | None = None,
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

    def trip_breaker(self, reason: str) -> bool:
        """Open the breaker and pause the bot. False if it is already open. Sells nothing."""
        if reason not in TRIP_REASONS:
            raise ValueError("unknown trip reason")
        now = self._clock.now()
        with self._storage.tx() as repos:
            control = repos.safety.control(for_update=True)
            if control.breaker_state == "OPEN":
                return False
            cooldown = now + timedelta(seconds=self._settings.safety.breaker_cooldown_seconds)
            ok = repos.safety.update_control(
                control.version,
                now,
                bot_state="PAUSED",
                breaker_state="OPEN",
                breaker_reason=reason,
                breaker_opened_at=now,
                breaker_cooldown_until=cooldown,
                last_change_reason="BREAKER_TRIPPED",
            )
            if ok:
                self._record(repos, Evt.BOT_BREAKER_OPENED, Res.SUCCESS, reason)
            return ok

    def pause(self, reason: str = "HOST_PAUSE") -> bool:
        now = self._clock.now()
        with self._storage.tx() as repos:
            control = repos.safety.control(for_update=True)
            if control.bot_state != "RUNNING":
                return False
            ok = repos.safety.update_control(
                control.version, now, bot_state="PAUSED", last_change_reason=reason
            )
            if ok:
                self._record(repos, Evt.BOT_PAUSED, Res.SUCCESS, reason)
            return ok

    def activate_kill(self, confirm: str, reason: str = "HOST_KILL") -> Outcome:
        if confirm != HOST_KILL_PHRASE:
            return Outcome("phrase_mismatch")
        now = self._clock.now()
        with self._storage.tx() as repos:
            control = repos.safety.control(for_update=True)
            if control.kill_switch == "ACTIVE":
                return Outcome("not_allowed", ("ALREADY_ACTIVE",))
            repos.safety.update_control(
                control.version,
                now,
                bot_state="PAUSED",
                kill_switch="ACTIVE",
                kill_reason=reason,
                kill_activated_at=now,
                last_change_reason=reason,
            )
            self._record(repos, Evt.BOT_KILL_ACTIVATED, Res.SUCCESS, reason, {"via": "host"})
        return Outcome("ok")

    def release_kill(self, confirm: str) -> Outcome:
        if confirm != RELEASE_PHRASE:
            with self._storage.tx() as repos:
                self._record(
                    repos,
                    Evt.BOT_CONTROL_DENIED,
                    Res.DENIED,
                    "PHRASE_MISMATCH",
                    {"attempted": "release"},
                )
            return Outcome("phrase_mismatch")
        now = self._clock.now()
        with self._storage.tx() as repos:
            control = repos.safety.control(for_update=True)
            if control.kill_switch != "ACTIVE":
                return Outcome("not_allowed", ("NOT_ACTIVE",))
            if repos.safety.open_command() is not None:
                self._record(
                    repos,
                    Evt.BOT_CONTROL_DENIED,
                    Res.DENIED,
                    "CANCEL_IN_PROGRESS",
                    {"attempted": "release"},
                )
                return Outcome("not_allowed", ("CANCEL_IN_PROGRESS",))
            repos.safety.update_control(
                control.version,
                now,
                bot_state="PAUSED",
                kill_switch="INACTIVE",
                kill_reason=None,
                kill_activated_at=None,
                recovery_state="INCOMPLETE",
                recovery_completed_at=None,
                last_change_reason="KILL_RELEASED",
            )
            self._record(repos, Evt.BOT_KILL_RELEASED, Res.SUCCESS, "KILL_RELEASED")
        return Outcome("ok")
