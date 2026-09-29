"""Template rendering and view models. Templates receive prepared display values only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
from fastapi.responses import HTMLResponse

from app import constants
from app.config import Settings
from app.domain.enums import Role
from app.domain.models import AuditRecord, AuthContext, ChainStatus, SessionSummary, UserSummary
from app.domain.permissions import Permission, has_permission

WEB_DIR = Path(__file__).resolve().parent
DISABLED_NAV = ("Bot", "LLM Review")

# Phase 1 fields that have no data source yet. Every value is "Unknown" or "Not available".
UNAVAILABLE_TILES: tuple[tuple[str, str], ...] = (
    ("Bot state", "Unknown"),
    ("Active pair", "Not available"),
    ("Protected reserve", "Not available"),
    ("Deployed capital", "Not available"),
    ("Deployment cap", "Not available"),
    ("Grid status", "Not available"),
    ("Data freshness", "Not available"),
    ("Product metadata freshness", "Not available"),
    ("Reconciliation status", "Unknown"),
    ("Circuit breaker state", "Unknown"),
    ("Kill switch state", "Unknown"),
    ("Recent risk decisions", "Not available"),
    ("Alerts", "Not available"),
    ("Latest reports", "Not available"),
    ("LLM Review Package state", "Not available"),
)

# Flash messages are chosen by fixed codes, never reflected from input.
MESSAGES: dict[str, tuple[str, str]] = {
    "password_changed": ("success", "Password changed. Your other sessions were signed out."),
    "reauth_ok": (
        "success",
        "Password confirmed. It can authorise one sensitive action within {window} seconds.",
    ),
    "others_revoked": ("success", "Your other sessions were signed out."),
    "session_revoked": ("success", "The session was signed out."),
    "user_sessions_revoked": ("success", "All sessions for that user were signed out."),
    "bad_current": ("error", "The current password is incorrect."),
    "mismatch": ("error", "The new passwords do not match."),
    "same": ("error", "The new password must differ from the current one."),
    "policy": ("error", "The new password does not meet the password policy."),
    "throttled": ("error", "Too many attempts. Try again later."),
    "reauth_invalid": ("error", "The password is incorrect."),
    "reauth_required": ("error", "Confirm your password first, then repeat the action."),
    "phrase_mismatch": ("error", "The confirmation phrase did not match."),
    "not_found": ("error", "The request could not be completed."),
    "invalid_request": ("error", "The request was invalid."),
}


def fmt(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if value else "Not available"


@dataclass(frozen=True)
class AuthView:
    username: str
    role: str
    csrf_token: str
    is_admin: bool

    @classmethod
    def of(cls, ctx: AuthContext) -> AuthView:
        return cls(
            ctx.user.username,
            ctx.user.role.value,
            ctx.csrf_token,
            has_permission(ctx.user.role, Permission.VIEW_AUDIT),
        )


@dataclass(frozen=True)
class DashboardView:
    known: tuple[tuple[str, str], ...]
    unavailable: tuple[tuple[str, str], ...]
    now: datetime


@dataclass(frozen=True)
class SessionRow:
    id: str
    started: str
    last_active: str
    expires: str
    current: bool


@dataclass(frozen=True)
class UserRow:
    id: str
    username: str
    role: str
    status: str
    sessions: int
    phrase: str


@dataclass(frozen=True)
class SecurityView:
    username: str
    role: str
    password_changed: str
    sessions: tuple[SessionRow, ...]
    users: tuple[UserRow, ...] | None
    reauth_active: bool
    reauth_window: int
    message_kind: str | None
    message_text: str | None
    rules: tuple[str, ...]


@dataclass(frozen=True)
class AuditRow:
    seq: int
    when: str
    code: str
    result: str
    actor: str
    target: str
    reason: str
    client: str
    detail: str


@dataclass(frozen=True)
class AuditView:
    rows: tuple[AuditRow, ...]
    chain_text: str
    chain_ok: bool
    next_cursor: int | None
    code: str
    codes: tuple[str, ...]


def build_dashboard(settings: Settings, ctx: AuthContext, now: datetime) -> DashboardView:
    """Only values the system actually knows appear in `known`; the rest stay unavailable."""
    known = (
        ("Bot mode", settings.mode),
        ("Live trading", constants.LIVE_TRADING_STATUS),
        ("Signed in as", f"{ctx.user.username} ({ctx.user.role.value})"),
        ("Session ends (absolute)", fmt(ctx.session.absolute_expires_at)),
        ("Idle timeout", f"{settings.auth.idle_timeout_seconds // 60} minutes"),
    )
    return DashboardView(known, UNAVAILABLE_TILES, now)


def build_security(
    settings: Settings,
    ctx: AuthContext,
    sessions: tuple[SessionSummary, ...],
    users: tuple[UserSummary, ...] | None,
    reauth_active: bool,
    *,
    message: str | None = None,
    rules: tuple[str, ...] = (),
) -> SecurityView:
    kind, text = MESSAGES.get(message or "", (None, None))
    if text:
        text = text.format(window=settings.auth.reauth_window_seconds)
    return SecurityView(
        username=ctx.user.username,
        role=ctx.user.role.value,
        password_changed=fmt(ctx.user.password_changed_at),
        sessions=tuple(
            SessionRow(
                str(s.id),
                fmt(s.created_at),
                fmt(s.last_seen_at),
                fmt(s.absolute_expires_at),
                s.is_current,
            )
            for s in sessions
        ),
        users=None
        if users is None
        else tuple(
            UserRow(
                str(u.id),
                u.username,
                u.role.value,
                "disabled" if u.disabled else "active",
                u.active_sessions,
                f"REVOKE SESSIONS FOR {u.username.upper()}",
            )
            for u in users
        ),
        reauth_active=reauth_active,
        reauth_window=settings.auth.reauth_window_seconds,
        message_kind=kind,
        message_text=text,
        rules=rules,
    )


def _detail_text(detail: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(detail.items())) or "-"


def build_audit(
    records: list[AuditRecord],
    chain: ChainStatus,
    *,
    code: str,
    codes: tuple[str, ...],
    page_size: int,
) -> AuditView:
    page = records[:page_size]
    if not chain.ok:
        chain_text = f"Audit chain BROKEN at event {chain.broken_at}"
    elif chain.complete:
        chain_text = f"Audit chain verified ({chain.events_checked} events)"
    else:
        chain_text = (
            f"Audit chain verified for the first {chain.events_checked} events (scan capped)"
        )
    rows = tuple(
        AuditRow(
            r.seq,
            fmt(r.occurred_at),
            r.event_code.value,
            r.result.value,
            r.actor_username or (r.actor_role.value if r.actor_role else "-"),
            f"{r.target_type}:{r.target_id}" if r.target_type else "-",
            r.reason_code or "-",
            r.client_tag or "-",
            _detail_text(r.detail),
        )
        for r in page
    )
    next_cursor = page[-1].seq if len(records) > page_size and page else None
    return AuditView(rows, chain_text, chain.ok, next_cursor, code, codes)


class Renderer:
    """Jinja2 environment: autoescape on, StrictUndefined, no inline scripts, no external assets."""

    _STATUS_PARTIAL = '{% from "base.html" import status_tile %}{{ status_tile(now) }}'

    def __init__(self, settings: Settings) -> None:
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(WEB_DIR / "templates"),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
        )
        self._env.globals.update(
            app_name=constants.APP_NAME,
            mode=settings.mode,
            live_status=constants.LIVE_TRADING_STATUS,
            disabled_nav=DISABLED_NAV,
            grafana_url=f"https://{settings.grafana_hostname}/"
            if settings.grafana_hostname
            else None,
        )

    def add_global(self, name: str, value: Any) -> None:
        self._env.globals[name] = value

    def html(
        self, template: str, status: int = 200, *, auth: AuthContext | None = None, **context: Any
    ) -> HTMLResponse:
        view = AuthView.of(auth) if auth else None
        body = self._env.get_template(template).render(auth=view, **context)
        return HTMLResponse(body, status_code=status)

    def status_fragment(self, now: datetime) -> HTMLResponse:
        return HTMLResponse(self._env.from_string(self._STATUS_PARTIAL).render(now=now))


__all__ = [
    "AuditView",
    "AuthView",
    "DashboardView",
    "MESSAGES",
    "Renderer",
    "Role",
    "SecurityView",
    "build_audit",
    "build_dashboard",
    "build_security",
]
