"""View models for the review package pages. Fixed sentences only: no input is ever reflected."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.review.schema import SCOPES
from app.review.service import PHRASES, Overview
from app.storage.review_repositories import PackageRow

CANNOT_TRADE: Final = (
    "A review package cannot trade and cannot change the bot, any pair, risk setting, order, "
    "configuration or live setting. It is a read-only, historical, sanitized file for a person to "
    "read or to give to an AI assistant by hand."
)

SCOPE_LABELS: Final = {
    "backtests": "Backtest summaries, report and snapshot ids",
    "paper": "Paper orders, fills and summary",
    "pairs": "Pair lifecycle history",
    "data_quality": "Data-quality runs and events",
    "grid_plans": "Grid plan summaries",
    "audit_summary": "Audit event counts per day (no actors or details)",
}

REASONS: Final = {
    "FEATURE_DISABLED": "Review packages are disabled.",
    "PACKAGE_BEING_BUILT": "A package is waiting to be built or is being built.",
    "TOO_MANY_RETAINED": "Ten packages are already retained. Wait for retention to expire some.",
    "RATE_LIMITED": "Three packages were requested in the last hour.",
    "ALREADY_ENABLED": "Review packages are already enabled.",
    "ALREADY_DISABLED": "Review packages are already disabled.",
    "PERIOD_ORDER": "The period end is before its start.",
    "PERIOD_IN_FUTURE": "The period cannot end in the future.",
    "PERIOD_TOO_LONG": "The period is longer than 90 days.",
    "SCOPE": "Choose at least one valid scope item.",
    "RETENTION_DAYS": "Retention must be between 1 and 90 days.",
    "DATABASE_REFUSED": "The database refused the request.",
}

MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "review_enabled": ("success", "Review packages are enabled."),
    "review_disabled": ("success", "Review packages are disabled. Downloads are blocked."),
    "review_requested": (
        "success",
        "Package requested. The host builds it with the review build command.",
    ),
    "review_verified": ("success", "The package is intact and clean."),
    "reauth_ok": ("success", "Password confirmed. It can authorise one action within the window."),
    "reauth_invalid": ("error", "The password is incorrect."),
    "throttled": ("error", "Too many attempts. Try again later."),
    "phrase_mismatch": ("error", "The typed phrase did not match exactly. Nothing was changed."),
    "reauth_required": ("error", "Confirm your password first. Nothing was changed."),
    "review_conflict": ("error", "That is not possible in the current state."),
    "review_disabled_error": ("error", "Review packages are disabled."),
    "review_limit": ("error", "A limit refused the request. Nothing was changed."),
    "review_invalid": ("error", "The request was not valid. Nothing was changed."),
    "review_not_ready": ("error", "Only a READY package can be verified or downloaded."),
    "review_corrupt": ("error", "Verification failed: the package is marked CORRUPT."),
    "not_found": ("error", "The request could not be completed."),
}


def flash(code: str | None) -> tuple[str | None, str | None]:
    kind, text = MESSAGES.get(code or "", (None, None))
    return kind, text


def reason_text(code: str) -> str:
    return REASONS.get(code, "Refused by policy.")


def _ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "Not available"


@dataclass(frozen=True)
class PackageView:
    id: str
    state: str
    period: str
    scope: tuple[str, ...]
    requested: str
    expires: str
    size: str
    sha: str
    failure: str
    can_use: bool
    files: tuple[tuple[str, int, str], ...]
    content_removed: bool


def _size(n: int | None) -> str:
    if n is None:
        return "Not available"
    return f"{n / 1024:.1f} KiB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MiB"


def package_view(p: PackageRow, enabled: bool) -> PackageView:
    files = tuple((str(f["path"]), int(f["bytes"]), str(f["sha256"])[:16]) for f in (p.files or []))
    return PackageView(
        id=str(p.id),
        state=p.state,
        period=f"{p.period_start} to {p.period_end}",
        scope=p.scope,
        requested=_ts(p.requested_at),
        expires=_ts(p.expires_at),
        size=_size(p.size_bytes),
        sha=p.package_sha256 or "Not available",
        failure=p.failure_code or "",
        can_use=enabled and p.state == "READY",
        files=files,
        content_removed=p.content_removed_at is not None,
    )


@dataclass(frozen=True)
class OverviewView:
    enabled: bool
    retention_days: int
    updated: str
    packages: tuple[PackageView, ...]
    counts: tuple[tuple[str, int], ...]
    blockers: tuple[str, ...]
    scopes: tuple[tuple[str, str], ...]
    message_kind: str | None
    message_text: str | None
    cannot_trade: str = CANNOT_TRADE


def build_overview(o: Overview, message: str | None) -> OverviewView:
    kind, text = flash(message)
    return OverviewView(
        enabled=o.settings.enabled,
        retention_days=o.settings.retention_days,
        updated=_ts(o.settings.updated_at),
        packages=tuple(package_view(p, o.settings.enabled) for p in o.packages),
        counts=tuple(sorted(o.counts.items())),
        blockers=o.create_blockers,
        scopes=tuple((s, SCOPE_LABELS[s]) for s in SCOPES),
        message_kind=kind,
        message_text=text,
    )


@dataclass(frozen=True)
class ConfirmView:
    action: str
    path: str  # URL prefix for the reauth and confirm posts
    title: str
    summary: tuple[str, ...]
    phrase: str
    reauth_active: bool
    hidden: tuple[tuple[str, str], ...]
    message_kind: str | None
    message_text: str | None
    cannot_trade: str = CANNOT_TRADE


_TITLES: Final = {
    "enable": "Enable read-only review packages",
    "disable": "Disable read-only review packages",
    "create": "Create a read-only review package",
}
PREFIX: Final = {
    "enable": "/review/enable",
    "disable": "/review/disable",
    "create": "/review/packages/create",
}


def build_confirm(
    action: str,
    summary: tuple[str, ...],
    hidden: tuple[tuple[str, str], ...],
    reauth_active: bool,
    message: str | None,
) -> ConfirmView:
    kind, text = flash(message)
    return ConfirmView(
        action,
        PREFIX[action],
        _TITLES[action],
        summary,
        PHRASES[action],
        reauth_active,
        hidden,
        kind,
        text,
    )


@dataclass(frozen=True)
class DetailView:
    package: PackageView
    message_kind: str | None
    message_text: str | None
    cannot_trade: str = CANNOT_TRADE


def build_detail(p: PackageRow, enabled: bool, message: str | None) -> DetailView:
    kind, text = flash(message)
    return DetailView(package_view(p, enabled), kind, text)
