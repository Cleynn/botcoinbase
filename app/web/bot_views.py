"""Display models for the Bot page and the bot rows on the overview. Fixed vocabularies only:
every reason, state and message shown comes from a code, never from request or exchange text."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.capital.profiles import PROFILES
from app.capital.trading import TradingConfig
from app.safety.control import (
    CONFIG_PHRASES,
    MODE_PHRASES,
    PHRASES,
    PROFILE_PHRASES,
    Overview,
)
from app.safety.live_gate import BLOCKERS, STATUS_TEXT
from app.safety.types import BLOCK_REASONS
from app.storage.repositories import Repos
from app.web.view_models import fmt

NOTICE: Final = (
    "This page cannot create, submit, change or sell any order. It can pause the bot, cancel "
    "orders the bot itself created, activate the kill switch and choose the trading mode. "
    "Choosing LIVE places nothing: the host must also arm live trading."
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
    "PROFILE_UNCHANGED": "that profile is already selected",
    "PROFILE": "that capital profile does not exist",
    "PROFILE_NOT_APPROVED": "only the pilot profile is approved in production",
    "MODE": "that mode does not exist",
    "MAX_PAIRS": "the number of pairs must be between 1 and 10",
    "LEVELS": "grid lines must be between 3 and 20",
    "AMOUNTS": "amounts must be zero or positive numbers",
    "GRIDS_EXCEED_INVESTED_CAP": "pairs times the amount per grid exceeds the invested cap",
    "ORDER_EXCEEDS_GRID": "the per-order cap exceeds the amount per grid",
    "RESERVE_BELOW_FLOOR": "the reserve must be at least 20% of invested plus reserve",
    "CONFIG_UNCHANGED": "those numbers are already in force",
    "MODE_UNCHANGED": "that mode is already selected",
    "PAPER_NOT_IDLE": "the paper session must be PAUSED with no open paper orders",
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
    "profile_paper": ("success", "The PAPER capital profile was updated."),
    "config_paper": ("success", "The PAPER trading configuration was updated."),
    "config_live": (
        "success",
        "The LIVE trading configuration was updated. Any live arming ended: arm again.",
    ),
    "mode_backtest": (
        "success",
        "Mode set to BACKTEST. Nothing was started and no order was placed.",
    ),
    "mode_paper": ("success", "Mode set to PAPER. Nothing was started and no order was placed."),
    "mode_live": (
        "success",
        "Mode set to LIVE. No order is placed until the host arms live trading; "
        "the kill switch, reserve and caps still apply.",
    ),
    "profile_live": (
        "success",
        "The LIVE capital profile was recorded. LIVE TRADING stays BLOCKED.",
    ),
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
    profiles: tuple[tuple[str, str, str, str, str, str, bool, bool], ...] = ()
    paper_profile: str = ""
    live_profile: str = ""
    capital_lines: tuple[tuple[str, str], ...] = ()
    profile_changeable: bool = False
    trading_mode: str = "PAPER"
    live_armed: bool = False
    mode_buttons: tuple[tuple[str, str, bool], ...] = ()  # slug, label, selected
    mode_changeable: bool = False
    trading_rows: tuple[tuple[str, int, int, str, str, str, str, int], ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


MODE_LABELS: Final[dict[str, str]] = {
    "backtest": "BACKTEST (historical data, no orders)",
    "paper": "PAPER (simulated orders on live prices)",
    "live": "LIVE (real orders on Coinbase, needs host arming)",
}


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
        profiles=tuple(
            (
                p.name,
                p.title,
                str(p.allocation_cap),
                str(p.protected_reserve),
                str(p.max_deployment),
                str(p.max_order),
                p.name == c.paper_profile,
                p.name == c.live_profile,
            )
            for p in PROFILES.values()
        ),
        paper_profile=c.paper_profile,
        live_profile=c.live_profile,
        capital_lines=_capital_lines(o),
        profile_changeable=can_manage and c.bot_state == "PAUSED",
        trading_mode=o.trading_mode,
        live_armed=o.live_armed,
        mode_buttons=tuple(
            (slug, label, slug.upper() == o.trading_mode) for slug, label in MODE_LABELS.items()
        ),
        mode_changeable=can_manage and c.bot_state == "PAUSED",
        trading_rows=tuple(
            (
                t.mode.lower(),
                t.max_pairs,
                t.levels_per_grid,
                str(t.quote_per_grid),
                str(t.invested_cap),
                str(t.reserve),
                str(t.per_order_cap),
                t.version,
            )
            for t in o.trading
        ),
    )


def _capital_lines(o: Overview) -> tuple[tuple[str, str], ...]:
    cap = o.capital
    if cap is None:
        return ()
    if cap.available is None:
        return (("Paper funds", cap.note or "unknown"),)
    return (
        ("Paper available USDC (cash less reserved buys)", cap.available),
        ("Quote reserved by open paper buys", cap.reserved or "0"),
        ("Paper inventory cost", cap.inventory_cost or "0"),
        ("Deployable now under the selected paper profile", cap.usable or "0"),
    )


PROFILE_LINES: Final[dict[str, tuple[str, ...]]] = {
    "paper": (
        "Sets the capital limits (allocation cap, protected reserve, maximum deployment and "
        "per-order cap) the PAPER bot and the database enforce.",
        "The bot must be PAUSED. Nothing on this page places, changes or sells an order.",
    ),
    "live": (
        "Records the capital profile a LIVE run would use. LIVE TRADING stays BLOCKED: this "
        "choice cannot open the live gate and no live mode exists in this build.",
        "The bot must be PAUSED. Nothing on this page places, changes or sells an order.",
    ),
}


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
    hidden: tuple[tuple[str, str], ...] = ()  # fields carried through both steps


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


MODE_LINES: Final[dict[str, tuple[str, ...]]] = {
    "backtest": ("BACKTEST replays stored historical candles. It never places an order.",),
    "paper": (
        "PAPER simulates orders against live prices with the paper ledger. No real order exists.",
    ),
    "live": (
        "LIVE means the host may place real post-only orders on Coinbase. Switching the mode "
        "places nothing: the host must also arm live trading (time limited, cancelled by the kill "
        "switch). The protected reserve, caps, fund checks and the kill switch always apply.",
    ),
}


def mode_confirm(
    mode: str,
    *,
    reauth_active: bool,
    message: str | None,
    error: str | None = None,
) -> ConfirmView:
    kind, text = flash(message)
    if error:
        kind, text = "error", f"Refused: {reason_text(error)}."
    return ConfirmView(
        f"Switch to {mode.upper()} mode",
        NOTICE,
        (
            *MODE_LINES[mode],
            "The bot must be PAUSED and the paper session idle (no open paper order).",
        ),
        (),
        f"/bot/mode/{mode}",
        MODE_PHRASES[mode],
        reauth_active,
        kind,
        text,
    )


@dataclass(frozen=True)
class TradingEditView:
    mode: str
    action: str  # where the form goes (a GET that shows the confirmation step)
    values: tuple[tuple[str, str, str, str], ...]  # field name, label, current value, hint
    message_kind: str | None
    message_text: str | None


EDIT_FIELDS: Final = (
    ("pairs", "Pairs traded in parallel (1 to 10)", "How many pairs may have an active grid."),
    ("levels", "Grid lines per grid (3 to 20)", "A grid of N lines has N-1 buy/sell cells."),
    ("per_grid", "USDC invested per grid", "Per pair. Pairs times this stays within the cap."),
    ("invested", "Total invested cap (USDC)", "The most committed across every grid."),
    ("reserve", "Reserve never invested (USDC)", "At least 20% of invested plus reserve."),
    ("per_order", "Largest single order (USDC)", "Each order, buy or sell, stays below this."),
)


def trading_edit(mode: str, cfg: TradingConfig, error: str | None = None) -> TradingEditView:
    current = {
        "pairs": str(cfg.max_pairs),
        "levels": str(cfg.levels_per_grid),
        "per_grid": str(cfg.quote_per_grid),
        "invested": str(cfg.invested_cap),
        "reserve": str(cfg.reserve),
        "per_order": str(cfg.per_order_cap),
    }
    kind, text = ("error", f"Refused: {reason_text(error)}.") if error else (None, None)
    return TradingEditView(
        mode,
        f"/bot/trading/{mode}/request",
        tuple((name, label, current[name], hint) for name, label, hint in EDIT_FIELDS),
        kind,
        text,
    )


def trading_confirm(
    mode: str,
    current: TradingConfig,
    proposed: TradingConfig,
    *,
    reauth_active: bool,
    message: str | None,
    error: str | None = None,
) -> ConfirmView:
    kind, text = flash(message)
    if error:
        kind, text = "error", f"Refused: {reason_text(error)}."
    rows = (
        ("pairs", current.max_pairs, proposed.max_pairs),
        ("grid lines", current.levels_per_grid, proposed.levels_per_grid),
        ("USDC per grid", current.quote_per_grid, proposed.quote_per_grid),
        ("invested cap", current.invested_cap, proposed.invested_cap),
        ("reserve", current.reserve, proposed.reserve),
        ("largest order", current.per_order_cap, proposed.per_order_cap),
    )
    lines = tuple(f"{name}: {old} -> {new}" for name, old, new in rows)
    extra = (
        "Nothing is placed or changed on the exchange. The bot must be PAUSED.",
        "Changing the LIVE numbers ends any live arming: arm again afterwards."
        if mode == "live"
        else "The paper session must be PAUSED with no open paper order.",
        "The bot never invests the reserve and never exceeds these caps, "
        "whatever the number of pairs.",
    )
    return ConfirmView(
        f"Update trading configuration for {mode.upper()} mode",
        NOTICE,
        (*lines, *extra),
        (),
        f"/bot/trading/{mode}",
        CONFIG_PHRASES[mode],
        reauth_active,
        kind,
        text,
        (
            ("pairs", str(proposed.max_pairs)),
            ("levels", str(proposed.levels_per_grid)),
            ("per_grid", str(proposed.quote_per_grid)),
            ("invested", str(proposed.invested_cap)),
            ("reserve", str(proposed.reserve)),
            ("per_order", str(proposed.per_order_cap)),
        ),
    )


def profile_confirm(
    mode: str,
    profile: str,
    *,
    reauth_active: bool,
    message: str | None,
    error: str | None = None,
) -> ConfirmView:
    kind, text = flash(message)
    if error:
        kind, text = "error", f"Refused: {reason_text(error)}."
    p = PROFILES[profile]
    detail = (
        f"Selected: {p.title} ({p.name}): allocation cap {p.allocation_cap} USDC, protected "
        f"reserve {p.protected_reserve} USDC, maximum deployment {p.max_deployment} USDC, "
        f"per-order cap {p.max_order} USDC."
    )
    return ConfirmView(
        f"Update capital limits for {mode.upper()} mode",
        NOTICE,
        (detail, *PROFILE_LINES[mode]),
        (),
        f"/bot/capital/{mode}",
        PROFILE_PHRASES[mode],
        reauth_active,
        kind,
        text,
        (("profile", profile),),
    )
