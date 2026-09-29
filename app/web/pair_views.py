"""Display models for the Pairs pages. Templates receive prepared strings only.

Everything that came from the exchange (status text, aliases, observed values) is shown through
Jinja autoescape; nothing here marks content safe. Reason codes map to fixed sentences.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.domain.models import AuditRecord
from app.domain.pairs import (
    ACTIONS,
    UNREPRESENTABLE_STATES,
    Chain,
    HistoryRow,
    PairAction,
    PairRecord,
    PairState,
    ValidationRun,
)
from app.pairs.policy import ALL_FLAGS
from app.pairs.service import Confirmation, PairDetail, PairsOverview, ProductPage
from app.web.view_models import fmt

LIFECYCLE: Final = (
    "DISCOVERED",
    "PROPOSED",
    "VALIDATING",
    "RESEARCH_ONLY",
    "PAPER_ELIGIBLE",
    "PAPER_ACTIVE",
    "LIVE_ELIGIBLE",
    "LIVE_ACTIVE",
)
SIDE_STATES: Final = ("PAUSED", "DISABLED", "ARCHIVED")

STATE_HELP: Final[dict[str, str]] = {
    "PROPOSED": "A candidate. Nothing has been checked and nothing will trade.",
    "VALIDATING": "Queued for validation. The host runner fetches public Coinbase data and records the result.",
    "RESEARCH_ONLY": "Validation did not pass. The pair is for research only.",
    "PAPER_ELIGIBLE": "Validation passed. It can be activated for paper trading by an ADMIN, separately.",
    "PAPER_ACTIVE": "The one active paper pair.",
    "PAUSED": "Paused; it must be cleared or revalidated before any other use.",
    "DISABLED": "Disabled by an ADMIN. It can be re-enabled (typed confirmation) or archived.",
    "ARCHIVED": "Archived: terminal. History is kept; adding the product again creates a new candidate.",
}

REASONS: Final[dict[str, str]] = {
    "BOT_STATE_UNAVAILABLE": "The bot state does not exist in this build; paper activation needs a PAUSED bot.",
    "MODE_NOT_PAPER": "The configured mode is not PAPER.",
    "NO_PASS_VALIDATION": "There is no passing validation run.",
    "VALIDATION_EXPIRED": "The passing validation run has expired; validate again.",
    "VALIDATION_SUPERSEDED": "A newer validation run replaced the eligibility evidence.",
    "METADATA_CHANGED": "Product metadata changed since validation; validate again.",
    "METADATA_STALE": "Product metadata is older than the allowed age.",
    "PRODUCT_NOT_OK": "The product's current status or flags do not allow trading.",
    "ANOTHER_PAIR_ACTIVE": "Another pair is already active. Only one active pair is allowed.",
    "ANOTHER_PAIR_PAUSED": "Another pair is paused and must be cleared first.",
    "RECONCILIATION_UNAVAILABLE": "Reconciliation does not exist in this build, so a previously active pair cannot be shown clean.",
    "NOT_CLEAN": "The pair is not clean (open orders, inventory or unreconciled state).",
    "PRODUCT_MISSING": "The product record is missing.",
    "STALE_VERSION": "The page was out of date; the pair changed. Reload and try again.",
    "ILLEGAL_TRANSITION": "That action is not allowed from the pair's current state.",
    "ARCHIVED_IS_TERMINAL": "An archived pair cannot change state.",
    "ACTIVE_PAIR_CANNOT_ARCHIVE": "An active pair cannot be archived; pause it and clear it first.",
    "ACTIVE_PAIR_CANNOT_DISABLE": "An active pair cannot be disabled; pause it and clear it first.",
    "PHRASE_MISMATCH": "The confirmation phrase did not match.",
    "REAUTH_REQUIRED": "Confirm your password first, then repeat the action.",
    "GUARD_FAILED": "A safety guard refused the action.",
    "PAIR_CAP_REACHED": "The maximum number of open pairs has been reached.",
    "ALREADY_A_CANDIDATE": "That product is already a candidate.",
    "TRANSITION_REJECTED": "The database refused the change.",
}

CHECK_LABELS: Final[dict[str, str]] = {
    "PRODUCT_STATUS": "Product status and flags",
    "QUOTE_CURRENCY": "Quote currency",
    "BASE_NOT_STABLE": "Base is not a stable asset",
    "DATA_BASIS": "Data basis",
    "METADATA_FRESHNESS": "Metadata freshness",
    "CLOCK_SYNC": "Clock agreement",
    "INCREMENTS": "Increments",
    "PRECISION": "Precision",
    "MINIMUMS": "Minimum order size",
    "HISTORY_AVAILABILITY": "History availability (daily candles)",
    "OHLCV_QUALITY": "OHLCV quality (five-minute candles)",
    "LIQUIDITY_SPREAD": "Liquidity and spread",
    "FEE_VIABILITY": "Fee viability",
    "CAPITAL_FEASIBILITY": "50 / 15 / 35 USDC feasibility",
}

ACTION_LABELS: Final[dict[PairAction, str]] = {
    PairAction.VALIDATE: "Queue validation",
    PairAction.PAUSE: "Pause",
    PairAction.DEACTIVATE: "Deactivate (back to eligible)",
    PairAction.ACTIVATE: "Activate for paper trading",
    PairAction.RESUME: "Resume paper trading",
    PairAction.DISABLE: "Disable",
    PairAction.REENABLE: "Re-enable",
    PairAction.ARCHIVE: "Archive",
}

MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "pair_proposed": (
        "success",
        "Candidate added as PROPOSED. It is not validated and will not trade.",
    ),
    "pair_validation_queued": ("success", "Validation queued. The host runner will process it."),
    "pair_paused": ("success", "The pair was paused."),
    "pair_deactivated": ("success", "The pair is eligible again and no longer paused."),
    "pair_disabled": ("success", "The pair was disabled."),
    "pair_reenabled": ("success", "The pair was re-enabled and queued for validation."),
    "pair_archived": ("success", "The pair was archived. Its history is kept."),
    "pair_activated": ("success", "The pair was activated for paper trading."),
    "pair_resumed": ("success", "Paper trading resumed for the pair."),
    "reauth_ok": (
        "success",
        "Password confirmed. It can authorise one action within the time window.",
    ),
    "pair_stale": ("error", REASONS["STALE_VERSION"]),
    "pair_not_allowed": ("error", REASONS["ILLEGAL_TRANSITION"]),
    "pair_blocked": ("error", REASONS["GUARD_FAILED"]),
    "pair_cap": ("error", REASONS["PAIR_CAP_REACHED"]),
    "pair_duplicate": ("error", REASONS["ALREADY_A_CANDIDATE"]),
    "pair_conflict": ("error", REASONS["TRANSITION_REJECTED"]),
    "phrase_mismatch": ("error", REASONS["PHRASE_MISMATCH"]),
    "reauth_required": ("error", REASONS["REAUTH_REQUIRED"]),
    "reauth_invalid": ("error", "The password is incorrect."),
    "throttled": ("error", "Too many attempts. Try again later."),
    "not_found": ("error", "The request could not be completed."),
}


def reason_text(code: str) -> str:
    return REASONS.get(code, "Refused by policy.")


def age_text(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "Not available"
    seconds = max(int((now - then).total_seconds()), 0)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def flash(code: str | None) -> tuple[str | None, str | None]:
    """Fixed sentences chosen by code. `r:<REASON>` maps to a fixed reason sentence; unknown codes
    show nothing, and no input is ever reflected."""
    if code and code.startswith("r:") and code[2:] in REASONS:
        return "error", REASONS[code[2:]]
    kind, text = MESSAGES.get(code or "", (None, None))
    return kind, text


# ---------------------------------------------------------------------------- list
@dataclass(frozen=True)
class PairRow:
    id: str
    product_id: str
    state: str
    version: int
    basis: str
    validation: str
    validated: str
    metadata_age: str
    metadata_stale: bool
    proposed: str


@dataclass(frozen=True)
class PairsView:
    rows: tuple[PairRow, ...]
    counts: tuple[tuple[str, int], ...]
    open_count: int
    max_pairs: int
    active: str
    lifecycle: tuple[str, ...]
    unrepresentable: tuple[str, ...]
    message_kind: str | None
    message_text: str | None
    is_admin: bool


def build_pairs(
    overview: PairsOverview,
    *,
    now: datetime,
    max_age_seconds: int,
    is_admin: bool,
    message: str | None,
) -> PairsView:
    rows = []
    for item in overview.pairs:
        run = item.latest_run
        verified = item.product.last_verified_at if item.product else None
        rows.append(
            PairRow(
                str(item.pair.id),
                item.pair.product_id,
                item.pair.state.value,
                item.pair.version,
                item.pair.data_basis,
                run.outcome if run else "Not validated",
                fmt(run.finished_at) if run else "Not available",
                age_text(now, verified),
                bool(verified and (now - verified).total_seconds() > max_age_seconds),
                fmt(item.pair.proposed_at),
            )
        )
    kind, text = flash(message)
    order = [s.value for s in PairState]
    counts = tuple((s, overview.counts.get(s, 0)) for s in order)
    return PairsView(
        tuple(rows),
        counts,
        overview.open_count,
        overview.max_pairs,
        overview.active.product_id if overview.active else "None",
        LIFECYCLE,
        UNREPRESENTABLE_STATES,
        kind,
        text,
        is_admin,
    )


# ---------------------------------------------------------------------------- products
@dataclass(frozen=True)
class ProductRowView:
    id: str
    product_id: str
    status: str
    blocking: str
    default: bool
    reasons: str
    metadata_age: str
    rank: str
    pair_state: str
    pair_id: str
    can_add: bool


@dataclass(frozen=True)
class ProductsView:
    rows: tuple[ProductRowView, ...]
    total: int
    page: int
    pages: int
    default_only: bool
    is_admin: bool
    message_kind: str | None
    message_text: str | None


def build_products(
    page: ProductPage, *, now: datetime, is_admin: bool, message: str | None
) -> ProductsView:
    rows = []
    for row in page.rows:
        meta = row.product.metadata
        blocking = ", ".join(flag for flag in ALL_FLAGS if getattr(meta, flag) is True) or "none"
        rows.append(
            ProductRowView(
                str(row.product.id),
                meta.product_id,
                meta.status,
                blocking,
                row.default_candidate,
                ", ".join(row.verdict_reasons) or "-",
                age_text(now, row.product.last_verified_at),
                str(row.product.discovered_rank) if row.product.discovered_rank else "-",
                row.existing_pair.state.value if row.existing_pair else "DISCOVERED",
                str(row.existing_pair.id) if row.existing_pair else "",
                is_admin and row.existing_pair is None,
            )
        )
    pages = max((page.total + page.page_size - 1) // page.page_size, 1)
    kind, text = flash(message)
    return ProductsView(
        tuple(rows), page.total, page.page, pages, page.default_only, is_admin, kind, text
    )


# ---------------------------------------------------------------------------- detail
@dataclass(frozen=True)
class CheckRow:
    label: str
    status: str
    reason: str
    message: str
    observed: str


@dataclass(frozen=True)
class RunView:
    id: str
    outcome: str
    finished: str
    expires: str
    expired: bool
    thresholds: str
    checks: tuple[CheckRow, ...]


@dataclass(frozen=True)
class HistoryView:
    version: int
    transition: str
    actor: str
    reason: str
    when: str
    audit_seq: str


@dataclass(frozen=True)
class AuditView:
    seq: int
    when: str
    code: str
    result: str
    reason: str
    actor: str
    client: str


@dataclass(frozen=True)
class ActionView:
    action: str
    label: str
    href: str  # confirmation page for full-chain actions, empty for one-click actions
    needs_phrase: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class DetailView:
    id: str
    product_id: str
    state: str
    state_help: str
    version: int
    lifecycle: tuple[tuple[str, str], ...]  # (label, css state: done|current|todo|blocked)
    side_state: str
    facts: tuple[tuple[str, str], ...]
    flags: tuple[tuple[str, str], ...]
    metadata_age: str
    metadata_stale: bool
    history_state: tuple[tuple[str, str], ...]
    latest_run: RunView | None
    earlier_runs: tuple[RunView, ...]
    valid: bool
    transitions: tuple[HistoryView, ...]
    audit: tuple[AuditView, ...]
    audit_detailed: bool
    actions: tuple[ActionView, ...]
    is_admin: bool
    message_kind: str | None
    message_text: str | None


def _run_view(run: ValidationRun, now: datetime) -> RunView:
    return RunView(
        str(run.id),
        run.outcome,
        fmt(run.finished_at),
        fmt(run.expires_at),
        run.expires_at <= now,
        run.thresholds_sha256[:16],
        tuple(
            CheckRow(
                CHECK_LABELS.get(c.code, c.code),
                c.status,
                c.reason,
                c.message,
                ", ".join(f"{k}={v}" for k, v in sorted(c.observed.items())) or "-",
            )
            for c in run.checks
        ),
    )


def _lifecycle(pair: PairRecord) -> tuple[tuple[str, str], ...]:
    state = pair.state.value
    order = [s for s in LIFECYCLE if s not in UNREPRESENTABLE_STATES and s != "DISCOVERED"]
    position = order.index(state) if state in order else -1
    steps: list[tuple[str, str]] = [("DISCOVERED", "done")]
    for name in LIFECYCLE[1:]:
        if name in UNREPRESENTABLE_STATES:
            steps.append((name, "blocked"))
        elif name == state:
            steps.append((name, "current"))
        elif position >= 0 and order.index(name) < position:
            steps.append((name, "done"))
        else:
            steps.append((name, "todo"))
    return tuple(steps)


def _history_state(run: ValidationRun | None) -> tuple[tuple[str, str], ...]:
    if run is None:
        return (("Candle history", "Not checked yet"),)
    by_code = {c.code: c for c in run.checks}
    rows: list[tuple[str, str]] = []
    for code, label in (
        ("HISTORY_AVAILABILITY", "Daily history"),
        ("OHLCV_QUALITY", "Five-minute candles"),
    ):
        check = by_code.get(code)
        rows.append((label, f"{check.status}: {check.message}" if check else "Not checked"))
    return tuple(rows)


def build_detail(
    detail: PairDetail,
    *,
    max_age_seconds: int,
    is_admin: bool,
    message: str | None,
) -> DetailView:
    pair, product, now = detail.pair, detail.product, detail.now
    meta = product.metadata
    actions = []
    for action, blockers in detail.blockers.items():
        spec = ACTIONS[action]
        if not is_admin:
            continue
        full = spec.chain is Chain.FULL
        actions.append(
            ActionView(
                action.value,
                ACTION_LABELS[action],
                f"/pairs/{pair.id}/{action.value}/request" if full else "",
                full,
                blockers,
            )
        )
    kind, text = flash(message)
    facts = (
        ("Order product", pair.order_product_id),
        ("Data product", pair.data_product_id or "Unknown"),
        (
            "Data basis",
            pair.data_basis
            + (
                " (priced from the unified USD book)"
                if pair.data_basis == "UNIFIED_USD_BOOK"
                else ""
            ),
        ),
        ("Base currency", meta.base_currency),
        ("Quote currency", meta.quote_currency),
        ("Product type / venue", f"{meta.product_type or 'Unknown'} / {meta.venue or 'Unknown'}"),
        ("Exchange status text", meta.status),
        ("Base increment", _n(meta.base_increment)),
        ("Quote increment", _n(meta.quote_increment)),
        ("Price increment", _n(meta.price_increment)),
        ("Base minimum size", _n(meta.base_min_size)),
        ("Base maximum size", _n(meta.base_max_size)),
        ("Quote minimum size", _n(meta.quote_min_size)),
        ("Quote maximum size", _n(meta.quote_max_size)),
        ("Alias", meta.alias or "None"),
        ("Alias to", ", ".join(meta.alias_to) or "None"),
        ("Unreadable fields", ", ".join(meta.malformed) or "none"),
        ("Proposed", f"{fmt(pair.proposed_at)} via {pair.proposed_via}"),
        ("Last state change", fmt(pair.state_changed_at)),
        ("Ever active", "yes" if pair.ever_active else "no"),
        ("Metadata verified", fmt(product.last_verified_at)),
    )
    flags = tuple(
        (f, {True: "true", False: "false", None: "Unknown"}[getattr(meta, f)]) for f in ALL_FLAGS
    )
    latest = detail.runs[0] if detail.runs else None
    return DetailView(
        str(pair.id),
        pair.product_id,
        pair.state.value,
        STATE_HELP[pair.state.value],
        pair.version,
        _lifecycle(pair),
        pair.state.value if pair.state.value in SIDE_STATES else "",
        facts,
        flags,
        age_text(now, product.last_verified_at),
        (now - product.last_verified_at).total_seconds() > max_age_seconds,
        _history_state(latest),
        _run_view(latest, now) if latest else None,
        tuple(_run_view(r, now) for r in detail.runs[1:]),
        detail.is_valid,
        tuple(_history_row(h, named=is_admin) for h in detail.history),
        tuple(_audit_row(a, detailed=is_admin) for a in detail.audit),
        is_admin,
        tuple(actions),
        is_admin,
        kind,
        text,
    )


def _n(value: object) -> str:
    return (
        "Not published"
        if value is None
        else format(value, "f")
        if hasattr(value, "quantize")
        else str(value)
    )


def _history_row(row: HistoryRow, *, named: bool) -> HistoryView:
    return HistoryView(
        row.version_after,
        f"{row.state_before or 'DISCOVERED'} -> {row.state_after}",
        (row.actor_username if named else None)
        or ("host CLI" if row.actor_class == "HOST" else "ADMIN"),
        row.reason_code or "-",
        fmt(row.occurred_at),
        str(row.audit_seq) if row.audit_seq else "-",
    )


def _audit_row(record: AuditRecord, *, detailed: bool) -> AuditView:
    return AuditView(
        record.seq,
        fmt(record.occurred_at),
        record.event_code.value,
        record.result.value,
        record.reason_code or "-",
        (record.actor_username or (record.actor_role.value if record.actor_role else "-"))
        if detailed
        else "-",
        (record.client_tag or "-") if detailed else "-",
    )


# ---------------------------------------------------------------------------- confirmation
@dataclass(frozen=True)
class ConfirmView:
    pair_id: str
    product_id: str
    action: str
    label: str
    phrase: str
    version: int
    state: str
    blockers: tuple[str, ...]
    reauth_active: bool
    warning: str
    message_kind: str | None
    message_text: str | None


WARNINGS: Final[dict[PairAction, str]] = {
    PairAction.ACTIVATE: "Activation makes this the only active paper pair. It does not start the bot and cannot trade by itself.",
    PairAction.RESUME: "Resuming returns this pair to active paper status. It does not start the bot.",
    PairAction.DISABLE: "Disabling stops the pair from being used. History is kept. It can be re-enabled later.",
    PairAction.REENABLE: "Re-enabling queues the pair for validation. It will not become active by itself.",
    PairAction.ARCHIVE: "Archiving is permanent for this pair: it can never change state again. History, reports and audit entries are kept.",
}


def build_confirm(conf: Confirmation, *, message: str | None) -> ConfirmView:
    kind, text = flash(message)
    return ConfirmView(
        str(conf.pair.id),
        conf.pair.product_id,
        conf.action.value,
        ACTION_LABELS[conf.action],
        conf.phrase,
        conf.pair.version,
        conf.pair.state.value,
        conf.blockers,
        conf.reauth_active,
        WARNINGS.get(conf.action, ""),
        kind,
        text,
    )


__all__ = ["build_confirm", "build_detail", "build_pairs", "build_products", "reason_text"]
