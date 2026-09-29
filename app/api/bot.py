"""Bot control pages and actions. Read-only page for every signed-in user; controls are ADMIN only.

Nothing here creates, submits, changes or sells an order, and nothing here talks to an exchange.
Every action follows: authz (permission) -> CSRF (app-wide guard) -> fresh reauth -> typed
confirmation -> audit -> internal command -> outcome audit (see `app.safety.control`).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import ReviewPlainConfirm, ReviewPlainReauth
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
