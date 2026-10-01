"""Bot control pages and actions. Read-only page for every signed-in user; controls are ADMIN only.

Nothing here creates, submits, changes or sells an order, and nothing here talks to an exchange.
Every action follows: authz (permission) -> CSRF (app-wide guard) -> fresh reauth -> typed
confirmation -> audit -> internal command -> outcome audit (see `app.safety.control`).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import (
    BotProfileConfirm,
    BotProfileReauth,
    BotTradingConfirm,
    BotTradingReauth,
    PairPick,
    ReviewPlainConfirm,
    ReviewPlainReauth,
)
from app.capital.profiles import PROFILES
from app.capital.trading import TradingConfig, problems
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.domain.permissions import Permission, has_permission
from app.pairs.transitions import Actor
from app.safety.control import Outcome
from app.web import bot_views as views

router = APIRouter()
_view = require_permission(Permission.VIEW_BOT)
_admin = require_permission(Permission.MANAGE_BOT)
Slug = Literal["pause", "resume", "cancel-known", "kill"]

_OK_FLASH = {
    "pause": "bot_paused",
    "resume": "bot_resumed",
    "cancel_known": "bot_cancel_queued",
    "kill": "bot_kill",
}
_STATUS = {
    "phrase_mismatch": 400,
    "reauth_required": 400,
    "invalid": 400,
    "not_allowed": 409,
    "conflict": 409,
    "not_found": 404,
}


def _actor(request: Request, ctx: AuthContext) -> Actor:
    return Actor(
        AuditActor(ctx.user.id, ActorRole(ctx.user.role.value)),
        ActorClass.WEB,
        resolve_client(request).tag,
        request_id(request),
    )


def _reauth(request: Request, ctx: AuthContext, password: str) -> str:
    outcome = get_services(request).auth.reauthenticate(
        ctx, password, resolve_client(request), request_id(request)
    )
    return {"ok": "reauth_ok", "throttled": "throttled"}.get(outcome, "reauth_invalid")


def _confirm_page(
    request: Request,
    ctx: AuthContext,
    action: str,
    message: str | None,
    status: int = 200,
    error: str | None = None,
) -> Response:
    services = get_services(request)
    blockers: tuple[str, ...] = ()
    if action == "resume":
        with services.storage.tx() as repos:
            blockers = services.bot.resume_blockers(repos, services.clock.now())
    view = views.confirm(
        action,
        reauth_active=services.bot.reauth_active(ctx),
        message=message,
        blockers=blockers,
        error=error,
    )
    return services.renderer.html("bot_confirm.html", status, auth=ctx, active="bot", view=view)


@router.get("/bot")
def bot_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_view)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    services = get_services(request)
    can_manage = has_permission(ctx.user.role, Permission.MANAGE_BOT)
    view = views.build_bot(services.bot.overview(ctx), can_manage=can_manage, message=msg)
    return services.renderer.html("bot.html", auth=ctx, active="bot", view=view)


@router.get("/bot/{slug}/request")
def request_page(
    request: Request,
    slug: Slug,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _confirm_page(request, ctx, views.SLUGS[slug], msg)


@router.post("/bot/{slug}/reauth")
def reauth(
    request: Request,
    slug: Slug,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(f"/bot/{slug}/request?msg={code}", status_code=303)
    return _confirm_page(request, ctx, views.SLUGS[slug], code, 429 if code == "throttled" else 400)


@router.post("/bot/{slug}/confirm")
def confirm(
    request: Request,
    slug: Slug,
    form: Annotated[ReviewPlainConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    action = views.SLUGS[slug]
    outcome: Outcome = get_services(request).bot.execute(
        ctx, _actor(request, ctx), action, form.confirmation
    )
    if outcome.kind == "ok":
        return RedirectResponse(f"/bot?msg={_OK_FLASH[action]}", status_code=303)
    if outcome.kind in ("phrase_mismatch", "reauth_required", "invalid"):
        return _confirm_page(request, ctx, action, outcome.kind, _STATUS[outcome.kind])
    return _confirm_page(
        request,
        ctx,
        action,
        None,
        _STATUS.get(outcome.kind, 409),
        error=outcome.reasons[0] if outcome.reasons else "GUARD_REFUSED",
    )


# ---------------------------------------------------------------- trading mode
TradingMode = Literal["backtest", "paper", "live"]


def _mode_page(
    request: Request,
    ctx: AuthContext,
    mode: str,
    message: str | None,
    status: int = 200,
    error: str | None = None,
) -> Response:
    services = get_services(request)
    view = views.mode_confirm(
        mode, reauth_active=services.bot.reauth_active(ctx), message=message, error=error
    )
    return services.renderer.html("bot_confirm.html", status, auth=ctx, active="bot", view=view)


@router.get("/bot/mode/{mode}/request")
def mode_request(
    request: Request,
    mode: TradingMode,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _mode_page(request, ctx, mode, msg)


@router.post("/bot/mode/{mode}/reauth")
def mode_reauth(
    request: Request,
    mode: TradingMode,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(f"/bot/mode/{mode}/request?msg={code}", status_code=303)
    return _mode_page(request, ctx, mode, code, 429 if code == "throttled" else 400)


@router.post("/bot/mode/{mode}/confirm")
def mode_confirm(
    request: Request,
    mode: TradingMode,
    form: Annotated[ReviewPlainConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome: Outcome = get_services(request).bot.switch_mode(
        ctx, _actor(request, ctx), mode, form.confirmation
    )
    if outcome.kind == "ok":
        return RedirectResponse(f"/bot?msg=mode_{mode}", status_code=303)
    if outcome.kind in ("phrase_mismatch", "reauth_required", "invalid"):
        return _mode_page(request, ctx, mode, outcome.kind, _STATUS[outcome.kind])
    return _mode_page(
        request,
        ctx,
        mode,
        None,
        _STATUS.get(outcome.kind, 409),
        error=outcome.reasons[0] if outcome.reasons else "GUARD_REFUSED",
    )


# ---------------------------------------------------------------- trading configuration (edit)
_AMOUNT_Q = Query(pattern=r"^[0-9]{1,9}(\.[0-9]{1,8})?$")


def _proposed(
    mode: str,
    pick: list[str],
    levels: int,
    per_grid: str,
    invested: str,
    reserve: str,
    per_order: str,
) -> TradingConfig:
    return TradingConfig(
        mode.upper(),
        max(1, len(set(pick))),  # the pair count is the size of the selection (at least 1)
        levels,
        Decimal(per_grid),
        Decimal(invested),
        Decimal(reserve),
        Decimal(per_order),
    )


def _trading_page(
    request: Request,
    ctx: AuthContext,
    mode: str,
    proposed: TradingConfig,
    pick: list[str],
    message: str | None,
    status: int = 200,
    error: str | None = None,
) -> Response:
    services = get_services(request)
    view = views.trading_confirm(
        mode,
        services.bot.trading_config(mode),
        proposed,
        chosen=services.bot.trading_selection(mode)[0],
        pick=tuple(sorted(set(pick))),
        reauth_active=services.bot.reauth_active(ctx),
        message=message,
        error=error,
    )
    return services.renderer.html("bot_confirm.html", status, auth=ctx, active="bot", view=view)


@router.get("/bot/trading/{mode}/edit")
def trading_edit(
    request: Request,
    mode: Mode,
    ctx: Annotated[AuthContext, Depends(_admin)],
    err: Annotated[str | None, Query(pattern=r"^[A-Z_]{3,40}$")] = None,
) -> Response:
    services = get_services(request)
    chosen, options = services.bot.trading_selection(mode)
    view = views.trading_edit(mode, services.bot.trading_config(mode), chosen, options, err)
    return services.renderer.html("bot_trading_edit.html", auth=ctx, active="bot", view=view)


@router.get("/bot/trading/{mode}/request")
def trading_request(
    request: Request,
    mode: Mode,
    levels: Annotated[int, Query(ge=3, le=20)],
    per_grid: Annotated[str, _AMOUNT_Q],
    invested: Annotated[str, _AMOUNT_Q],
    reserve: Annotated[str, _AMOUNT_Q],
    per_order: Annotated[str, _AMOUNT_Q],
    ctx: Annotated[AuthContext, Depends(_admin)],
    pick: Annotated[list[PairPick] | None, Query(max_length=10)] = None,
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    picked = pick or []
    proposed = _proposed(mode, picked, levels, per_grid, invested, reserve, per_order)
    bad = problems(proposed)
    selectable = {p for p, _state in get_services(request).bot.trading_selection(mode)[1]}
    if not bad and not set(picked) <= selectable:
        bad = ["PAIR_NOT_SELECTABLE"]
    if bad:  # shown on the form, nothing is spent
        return RedirectResponse(f"/bot/trading/{mode}/edit?err={bad[0]}", status_code=303)
    return _trading_page(request, ctx, mode, proposed, picked, msg)


def _query(mode: str, form: BotTradingReauth | BotTradingConfirm) -> str:
    picks = "".join(f"&pick={p}" for p in sorted(set(form.pick)))  # validated product ids
    return (
        f"/bot/trading/{mode}/request?levels={form.levels}"
        f"&per_grid={form.per_grid}&invested={form.invested}&reserve={form.reserve}"
        f"&per_order={form.per_order}{picks}"
    )


@router.post("/bot/trading/{mode}/reauth")
def trading_reauth(
    request: Request,
    mode: Mode,
    form: Annotated[BotTradingReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(f"{_query(mode, form)}&msg={code}", status_code=303)
    proposed = _proposed(
        mode, form.pick, form.levels, form.per_grid, form.invested, form.reserve, form.per_order
    )
    return _trading_page(
        request, ctx, mode, proposed, form.pick, code, 429 if code == "throttled" else 400
    )


@router.post("/bot/trading/{mode}/confirm")
def trading_confirm(
    request: Request,
    mode: Mode,
    form: Annotated[BotTradingConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    proposed = _proposed(
        mode, form.pick, form.levels, form.per_grid, form.invested, form.reserve, form.per_order
    )
    outcome: Outcome = get_services(request).bot.set_trading_config(
        ctx, _actor(request, ctx), mode, proposed, form.confirmation, selection=form.pick
    )
    if outcome.kind == "ok":
        return RedirectResponse(f"/bot?msg=config_{mode}", status_code=303)
    if outcome.kind in ("phrase_mismatch", "reauth_required"):
        return _trading_page(
            request, ctx, mode, proposed, form.pick, outcome.kind, _STATUS[outcome.kind]
        )
    return _trading_page(
        request,
        ctx,
        mode,
        proposed,
        form.pick,
        None,
        _STATUS.get(outcome.kind, 409),
        error=outcome.reasons[0] if outcome.reasons else "GUARD_REFUSED",
    )


# ---------------------------------------------------------------- capital profile (PAPER / LIVE)
Mode = Literal["paper", "live"]
_PROFILE_QUERY = Query(pattern=r"^[a-z][a-z0-9_]{1,23}$")


def _profile_page(
    request: Request,
    ctx: AuthContext,
    mode: str,
    profile: str,
    message: str | None,
    status: int = 200,
    error: str | None = None,
) -> Response:
    services = get_services(request)
    if profile not in PROFILES:
        return RedirectResponse("/bot?msg=invalid", status_code=303)
    view = views.profile_confirm(
        mode,
        profile,
        reauth_active=services.bot.reauth_active(ctx),
        message=message,
        error=error,
    )
    return services.renderer.html("bot_confirm.html", status, auth=ctx, active="bot", view=view)


@router.get("/bot/capital/{mode}/request")
def profile_request(
    request: Request,
    mode: Mode,
    profile: Annotated[str, _PROFILE_QUERY],
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _profile_page(request, ctx, mode, profile, msg)


@router.post("/bot/capital/{mode}/reauth")
def profile_reauth(
    request: Request,
    mode: Mode,
    form: Annotated[BotProfileReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(
            f"/bot/capital/{mode}/request?profile={form.profile}&msg={code}", status_code=303
        )
    return _profile_page(
        request, ctx, mode, form.profile, code, 429 if code == "throttled" else 400
    )


@router.post("/bot/capital/{mode}/confirm")
def profile_confirm(
    request: Request,
    mode: Mode,
    form: Annotated[BotProfileConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome: Outcome = get_services(request).bot.set_profile(
        ctx, _actor(request, ctx), mode, form.profile, form.confirmation
    )
    if outcome.kind == "ok":
        return RedirectResponse(f"/bot?msg=profile_{mode}", status_code=303)
    if outcome.kind in ("phrase_mismatch", "reauth_required", "invalid"):
        return _profile_page(request, ctx, mode, form.profile, outcome.kind, _STATUS[outcome.kind])
    return _profile_page(
        request,
        ctx,
        mode,
        form.profile,
        None,
        _STATUS.get(outcome.kind, 409),
        error=outcome.reasons[0] if outcome.reasons else "GUARD_REFUSED",
    )
