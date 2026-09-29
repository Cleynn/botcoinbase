"""Display models for the Bot page and the bot rows on the overview. Fixed vocabularies only:
every reason, state and message shown comes from a code, never from request or exchange text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.safety.control import PHRASES, Overview
from app.safety.live_gate import BLOCKERS, STATUS_TEXT
from app.safety.types import BLOCK_REASONS
from app.storage.repositories import Repos
from app.web.view_models import fmt

NOTICE: Final = (
    "This page cannot create, submit, change or sell any order. It can pause the bot, cancel "
    "orders the bot itself created, and activate the kill switch. Live trading is blocked."
)
SLUGS: Final[dict[str, str]] = {
    "pause": "pause",
    "resume": "resume",
    "cancel-known": "cancel_known",
    "kill": "kill",
}
ACTION_SLUG: Final = {v: k for k, v in SLUGS.items()}

REASON_TEXT: Final[dict[str, str]] = {
    **BLOCK_REASONS,
    "ALREADY_RUNNING": "the bot is already RUNNING",
    "ALREADY_PAUSED": "the bot is already PAUSED",
    "ALREADY_ACTIVE": "the kill switch is already active",
    "BREAKER_COOLDOWN": "the circuit breaker cooldown has not elapsed",
    "RECONCILIATION_BEFORE_TRIP": "the last reconciliation is older than the breaker trip",
    "RECONCILIATION_MISMATCH": "the last reconciliation found a mismatch",
    "BOT_MUST_BE_PAUSED": "the bot must be PAUSED first",
    "COMMAND_IN_PROGRESS": "another cancel command is still queued or running",
    "STATE_CHANGED": "the state changed while you were confirming; check it and try again",
    "GUARD_REFUSED": "the database refused the change",
    "PHRASE_MISMATCH": "the confirmation phrase did not match exactly",
    "REAUTH_REQUIRED": "confirm your password first",
}

ACTION_TEXT: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "pause": (
        "Pause the bot",
        (
            "Pausing stops all new orders immediately.",
            "Orders already on the exchange stay there until you cancel them.",
        ),
    ),
    "resume": (
        "Resume the bot after reconciliation",
        (
            "Resuming needs a current successful reconciliation of the exchange with the bot's "
            "records, completed startup recovery, an inactive kill switch and a closed (or cooled "
            "down) circuit breaker.",
            "Resuming lets the bot place new orders again only through the risk checks. "
            "Nothing on this page places an order.",
        ),
    ),
    "cancel_known": (
        "Cancel known bot orders",
        (
            "Asks the host to cancel only orders the bot created and knows the exchange id of, one "
            "by one. Foreign orders are never touched.",
            "It never sells anything. A cancel is confirmed only by the next reconciliation.",
            "The bot must be PAUSED.",
        ),
    ),
    "kill": (
        "Activate the kill switch",
        (
            "Blocks every new order and pauses the bot at once, and queues a cancel of the "
            "bot's known orders.",
            "It never sells any holding. Only the host can release the kill switch, and releasing "
            "it never resumes the bot.",
        ),
    ),
}

MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "bot_paused": ("success", "The bot is PAUSED. No new orders will be placed."),
    "bot_resumed": ("success", "The bot is RUNNING. New orders still pass the risk checks."),
    "bot_kill": ("success", "Kill switch ACTIVE. New orders are blocked and a cancel was queued."),
    "bot_cancel_queued": ("success", "Cancel of known bot orders queued for the host."),
    "reauth_ok": ("success", "Password confirmed. It authorises one action within the window."),
    "reauth_invalid": ("error", "The password is incorrect."),
    "throttled": ("error", "Too many attempts. Try again later."),
    "reauth_required": ("error", "Confirm your password first, then repeat the action."),
    "phrase_mismatch": ("error", "The confirmation phrase did not match exactly."),
    "invalid": ("error", "The request was invalid."),
}


def flash(code: str | None) -> tuple[str | None, str | None]:
    return MESSAGES.get(code or "", (None, None))


def reason_text(code: str) -> str:
    return REASON_TEXT.get(code, "refused")


@dataclass(frozen=True)
class BotView:
    notice: str
    gate_text: str
    gate_reasons: tuple[str, ...]
    bot_state: str
    kill_switch: str
    kill_reason: str
    breaker_state: str
    breaker_reason: str
    breaker_until: str
    recovery_state: str
    recon_text: str
    recon_findings: tuple[tuple[str, str], ...]
    counts: tuple[tuple[str, int], ...]
    api_text: str
    blocks: tuple[tuple[str, tuple[str, ...]], ...]
    history: tuple[tuple[str, str, str, str], ...]
    commands: tuple[tuple[str, str, str, str, str], ...]
    resume_blockers: tuple[str, ...]
    can_manage: bool
    actions: tuple[tuple[str, str, str], ...]  # slug, label, phrase
    message_kind: str | None
    message_text: str | None
    extra: dict[str, str] = field(default_factory=dict)


def _ts(moment: datetime | None) -> str:
    return fmt(moment) if moment else "-"


def build_bot(o: Overview, *, can_manage: bool, message: str | None) -> BotView:
    kind, text = flash(message)
    c = o.control
    if o.run is None:
        recon = "No reconciliation has run."
    elif o.run.outcome == "OK":
        recon = f"OK at {_ts(o.run.finished_at)} ({o.run.trigger}, {o.run.orders_seen} orders)"
    elif o.run.outcome == "FAILED":
        recon = f"FAILED at {_ts(o.run.finished_at)}: {o.run.failure_code}"
    else:
        recon = f"MISMATCH at {_ts(o.run.finished_at)}: {o.run.findings_count} finding(s)"
    return BotView(
        NOTICE,
        STATUS_TEXT,
        tuple(f"{code}: {text_}" for code, text_ in BLOCKERS.items() if code in o.gate.reasons),
        c.bot_state,
        c.kill_switch,
        c.kill_reason or "-",
        c.breaker_state,
        c.breaker_reason or "-",
        _ts(c.breaker_cooldown_until) if c.breaker_state == "OPEN" else "-",
        c.recovery_state,
        recon,
        tuple((f.code, f.subject or "-") for f in o.findings),
        tuple(sorted(o.counts.items())),
        f"{o.api_failures_1h} failed of {o.api_calls_1h} exchange reads in the last hour",
        tuple(
            (
                _ts(b["decided_at"]),
                tuple(reason_text(r) for r in b["reasons"]) or ("allowed",),
            )
            for b in o.recent_blocks
        ),
        tuple(
            (_ts(h["occurred_at"]), h["actor_class"], h["event"], h["reason"]) for h in o.history
        ),
        tuple(
            (
                _ts(cmd.requested_at),
                cmd.origin,
                cmd.state,
                _ts(cmd.finished_at),
                cmd.failure_code or "-",
            )
            for cmd in o.commands
        ),
        tuple(reason_text(b) for b in o.resume_blockers),
        can_manage,
        tuple((slug, ACTION_TEXT[a][0], PHRASES[a]) for slug, a in SLUGS.items()),
        kind,
        text,
    )


@dataclass(frozen=True)
class ConfirmView:
    title: str
    notice: str
    lines: tuple[str, ...]
    blockers: tuple[str, ...]
    action_path: str
    phrase: str
    reauth_active: bool
    message_kind: str | None
    message_text: str | None


def confirm(
    action: str,
    *,
    reauth_active: bool,
    message: str | None,
    blockers: tuple[str, ...] = (),
    error: str | None = None,
) -> ConfirmView:
    kind, text = flash(message)
    if error:
        kind, text = "error", f"Refused: {reason_text(error)}."
    title, lines = ACTION_TEXT[action]
    return ConfirmView(
        title,
        NOTICE,
        lines,
        tuple(reason_text(b) for b in blockers),
        f"/bot/{ACTION_SLUG[action]}",
        PHRASES[action],
        reauth_active,
        kind,
        text,
    )


def bot_rows(repos: Repos, now: datetime) -> tuple[tuple[str, str], ...]:
    """The overview's read-only bot rows (real values from the control row)."""
    c = repos.safety.control()
    run = repos.safety.latest_run()
    if run is None:
        recon = "no reconciliation yet"
    else:
        age = int((now - run.finished_at).total_seconds())
        recon = f"{run.outcome} {age // 60} min ago"
    return (
        ("Bot state", c.bot_state),
        ("Kill switch state", c.kill_switch),
        ("Circuit breaker state", c.breaker_state),
        ("Startup recovery", c.recovery_state),
        ("Reconciliation status", recon),
    )
