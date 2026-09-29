"""Pairs pages and actions. None of these calls Coinbase or touches orders, balances or the bot.

Reads are open to any signed-in user; every change needs ADMIN. Two-step sensitive actions never
write on GET: the request page only displays; `reauth` and `confirm` are CSRF-protected POSTs.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import PairActionForm, PairConfirmForm, PairReauthForm, ProposeForm
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ACTIONS, ActorClass, Chain, PairAction
from app.domain.permissions import Permission, has_permission
from app.pairs.service import Outcome
from app.pairs.transitions import Actor
from app.web import pair_views

router = APIRouter()

_view_access = require_permission(Permission.VIEW_PAIRS)
_manage_access = require_permission(Permission.MANAGE_PAIRS)

PRODUCT_PAGE_SIZE = 25
_FLASH_FOR_KIND: dict[str, str] = {
    "stale": "pair_stale",
    "not_allowed": "pair_not_allowed",
    "blocked": "pair_blocked",
    "cap_reached": "pair_cap",
    "duplicate": "pair_duplicate",
    "conflict": "pair_conflict",
    "phrase_mismatch": "phrase_mismatch",
    "reauth_required": "reauth_required",
    "not_found": "not_found",
}
_STATUS_FOR_KIND: dict[str, int] = {
    "stale": 409,
    "not_allowed": 409,
    "blocked": 409,
    "cap_reached": 409,
    "duplicate": 409,
    "conflict": 409,
    "phrase_mismatch": 400,
    "reauth_required": 400,
    "not_found": 404,
}
_DONE_FLASH: dict[PairAction, str] = {
    PairAction.VALIDATE: "pair_validation_queued",
    PairAction.PAUSE: "pair_paused",
    PairAction.DEACTIVATE: "pair_deactivated",
    PairAction.ACTIVATE: "pair_activated",
    PairAction.RESUME: "pair_resumed",
    PairAction.DISABLE: "pair_disabled",
    PairAction.REENABLE: "pair_reenabled",
    PairAction.ARCHIVE: "pair_archived",
}


def _is_admin(ctx: AuthContext) -> bool:
    return has_permission(ctx.user.role, Permission.MANAGE_PAIRS)


def _actor(request: Request, ctx: AuthContext) -> Actor:
    return Actor(
        AuditActor(ctx.user.id, ActorRole(ctx.user.role.value)),
        ActorClass.WEB,
        resolve_client(request).tag,
        request_id(request),
    )


def _max_age(request: Request) -> int:
    return get_services(request).settings.pair_policy.validation.max_metadata_age_seconds


def _full_action(action: PairAction) -> PairAction:
    if ACTIONS[action].chain is not Chain.FULL:
        raise HTTPException(status_code=404)
    return action


# ------------------------------------------------------------------------------ reads
@router.get("/pairs")
def pairs_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_view_access)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    services = get_services(request)
    view = pair_views.build_pairs(
        services.pairs.overview(),
        now=services.clock.now(),
        max_age_seconds=_max_age(request),
        is_admin=_is_admin(ctx),
        message=msg,
    )
    return services.renderer.html("pairs.html", auth=ctx, active="pairs", view=view)


def _products_response(
    request: Request,
    ctx: AuthContext,
    *,
    show: str,
    page: int,
    message: str | None,
    status: int = 200,
) -> Response:
    services = get_services(request)
    result = services.pairs.products(
        default_only=show != "all", page=page, page_size=PRODUCT_PAGE_SIZE
    )
    view = pair_views.build_products(
        result, now=services.clock.now(), is_admin=_is_admin(ctx), message=message
    )
    return services.renderer.html("pair_products.html", status, auth=ctx, active="pairs", view=view)


@router.get("/pairs/products")
def products_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_view_access)],
    show: Annotated[Literal["default", "all"], Query()] = "default",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _products_response(request, ctx, show=show, page=page, message=msg)


@router.get("/pairs/{pair_id}")
def pair_page(
    request: Request,
    pair_id: UUID,
    ctx: Annotated[AuthContext, Depends(_view_access)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _detail_response(request, ctx, pair_id, message=msg)


def _detail_response(
    request: Request,
    ctx: AuthContext,
    pair_id: UUID,
    *,
    message: str | None,
    status: int = 200,
) -> Response:
    services = get_services(request)
    admin = _is_admin(ctx)
    detail = services.pairs.detail(pair_id, include_audit=admin)
    if detail is None:
        raise HTTPException(status_code=404)
    view = pair_views.build_detail(
        detail, max_age_seconds=_max_age(request), is_admin=admin, message=message
    )
    return services.renderer.html("pair_detail.html", status, auth=ctx, active="pairs", view=view)


# ------------------------------------------------------------------------------ candidate
@router.post("/pairs/candidates")
def add_candidate(
    request: Request,
    form: Annotated[ProposeForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    outcome = get_services(request).pairs.propose(_actor(request, ctx), form.product_id)
    if outcome.kind == "ok" and outcome.pair_id:
        return RedirectResponse(f"/pairs/{outcome.pair_id}?msg=pair_proposed", status_code=303)
    return _products_response(
        request,
        ctx,
        show="all",
        page=1,
        message=_FLASH_FOR_KIND.get(outcome.kind, "pair_conflict"),
        status=_STATUS_FOR_KIND.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ one-click actions
def _one_click(
    request: Request, pair_id: UUID, form: PairActionForm, ctx: AuthContext, action: PairAction
) -> Response:
    outcome = get_services(request).pairs.simple_action(
        _actor(request, ctx), pair_id, action, form.version
    )
    return _finish(request, ctx, pair_id, action, outcome)


def _flash(outcome: Outcome) -> str:
    if outcome.kind == "not_allowed" and outcome.reasons:
        return f"r:{outcome.reasons[0]}"
    return _FLASH_FOR_KIND.get(outcome.kind, "pair_conflict")


def _finish(
    request: Request, ctx: AuthContext, pair_id: UUID, action: PairAction, outcome: Outcome
) -> Response:
    if outcome.kind == "ok":
        return RedirectResponse(f"/pairs/{pair_id}?msg={_DONE_FLASH[action]}", status_code=303)
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    return _detail_response(
        request,
        ctx,
        pair_id,
        message=_flash(outcome),
        status=_STATUS_FOR_KIND.get(outcome.kind, 409),
    )


@router.post("/pairs/{pair_id}/validate")
def validate_pair(
    request: Request,
    pair_id: UUID,
    form: Annotated[PairActionForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    return _one_click(request, pair_id, form, ctx, PairAction.VALIDATE)


@router.post("/pairs/{pair_id}/pause")
def pause_pair(
    request: Request,
    pair_id: UUID,
    form: Annotated[PairActionForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    return _one_click(request, pair_id, form, ctx, PairAction.PAUSE)


@router.post("/pairs/{pair_id}/deactivate")
def deactivate_pair(
    request: Request,
    pair_id: UUID,
    form: Annotated[PairActionForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    return _one_click(request, pair_id, form, ctx, PairAction.DEACTIVATE)


# ------------------------------------------------------------------------------ full chain
@router.get("/pairs/{pair_id}/{action}/request")
def confirmation_page(
    request: Request,
    pair_id: UUID,
    action: PairAction,
    ctx: Annotated[AuthContext, Depends(_manage_access)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    services = get_services(request)
    conf = services.pairs.confirmation(ctx, pair_id, _full_action(action))
    if conf is None:
        raise HTTPException(status_code=404)
    return services.renderer.html(
        "pair_confirm.html",
        auth=ctx,
        active="pairs",
        view=pair_views.build_confirm(conf, message=msg),
    )


@router.post("/pairs/{pair_id}/{action}/reauth")
def confirmation_reauth(
    request: Request,
    pair_id: UUID,
    action: PairAction,
    form: Annotated[PairReauthForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    services = get_services(request)
    action = _full_action(action)
    outcome = services.auth.reauthenticate(
        ctx, form.password, resolve_client(request), request_id(request)
    )
    code = {"ok": "reauth_ok", "throttled": "throttled"}.get(outcome, "reauth_invalid")
    if outcome == "ok":
        return RedirectResponse(
            f"/pairs/{pair_id}/{action.value}/request?msg={code}", status_code=303
        )
    conf = services.pairs.confirmation(ctx, pair_id, action)
    if conf is None:
        raise HTTPException(status_code=404)
    return services.renderer.html(
        "pair_confirm.html",
        429 if outcome == "throttled" else 400,
        auth=ctx,
        active="pairs",
        view=pair_views.build_confirm(conf, message=code),
    )


@router.post("/pairs/{pair_id}/{action}/confirm")
def confirm_action(
    request: Request,
    pair_id: UUID,
    action: PairAction,
    form: Annotated[PairConfirmForm, Form()],
    ctx: Annotated[AuthContext, Depends(_manage_access)],
) -> Response:
    services = get_services(request)
    action = _full_action(action)
    outcome = services.pairs.confirm(
        ctx, _actor(request, ctx), pair_id, action, form.version, form.confirmation
    )
    if outcome.kind == "ok":
        return RedirectResponse(f"/pairs/{pair_id}?msg={_DONE_FLASH[action]}", status_code=303)
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    conf = services.pairs.confirmation(ctx, pair_id, action)
    message = _flash(outcome)
    status = _STATUS_FOR_KIND.get(outcome.kind, 409)
    if conf is None:  # the state no longer allows the action: show the pair instead
        return _detail_response(request, ctx, pair_id, message=message, status=status)
    return services.renderer.html(
        "pair_confirm.html",
        status,
        auth=ctx,
        active="pairs",
        view=pair_views.build_confirm(conf, message=message),
    )
