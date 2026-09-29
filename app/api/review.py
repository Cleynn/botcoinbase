"""Review package pages and actions (ADMIN only). Nothing here trades, calls an LLM, imports a
proposal, or changes bot, pair, risk, order, configuration or live state.

Sensitive actions never write on GET: the request page only displays; `reauth` and `confirm` are
CSRF-protected POSTs. Download and verify are POSTs too (CSRF and Origin checked, audited).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import (
    ReviewEnableConfirm,
    ReviewEnableReauth,
    ReviewPackageAction,
    ReviewPlainConfirm,
    ReviewPlainReauth,
    ReviewRequestConfirm,
    ReviewRequestReauth,
)
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.domain.permissions import Permission
from app.pairs.transitions import Actor
from app.review.schema import SCOPES
from app.review.service import Outcome
from app.web import review_views as views

router = APIRouter()
_admin = require_permission(Permission.MANAGE_REVIEW_PACKAGES)

_FLASH: dict[str, str] = {
    "phrase_mismatch": "phrase_mismatch",
    "reauth_required": "reauth_required",
    "conflict": "review_conflict",
    "disabled": "review_disabled_error",
    "limit": "review_limit",
    "invalid": "review_invalid",
    "not_ready": "review_not_ready",
    "corrupt": "review_corrupt",
    "not_found": "not_found",
}
_STATUS: dict[str, int] = {
    "phrase_mismatch": 400,
    "reauth_required": 400,
    "conflict": 409,
    "disabled": 409,
    "limit": 429,
    "invalid": 400,
    "not_ready": 409,
    "corrupt": 409,
    "not_found": 404,
}


def _actor(request: Request, ctx: AuthContext) -> Actor:
    return Actor(
        AuditActor(ctx.user.id, ActorRole(ctx.user.role.value)),
        ActorClass.WEB,
        resolve_client(request).tag,
        request_id(request),
    )


def _overview(
    request: Request, ctx: AuthContext, message: str | None, status: int = 200
) -> Response:
    services = get_services(request)
    view = views.build_overview(services.review.overview(ctx), message)
    return services.renderer.html("review.html", status, auth=ctx, active="review", view=view)


def _confirm_page(
    request: Request,
    ctx: AuthContext,
    action: str,
    summary: tuple[str, ...],
    hidden: tuple[tuple[str, str], ...],
    message: str | None,
    status: int = 200,
) -> Response:
    services = get_services(request)
    view = views.build_confirm(action, summary, hidden, services.review.reauth_active(ctx), message)
    return services.renderer.html(
        "review_confirm.html", status, auth=ctx, active="review", view=view
    )


def _reauth(request: Request, ctx: AuthContext, password: str) -> str:
    outcome = get_services(request).auth.reauthenticate(
        ctx, password, resolve_client(request), request_id(request)
    )
    return {"ok": "reauth_ok", "throttled": "throttled"}.get(outcome, "reauth_invalid")


def _after(request: Request, ctx: AuthContext, outcome: Outcome, done: str) -> Response | None:
    """A redirect on success, else None (the caller re-renders its own page with the flash)."""
    return (
        RedirectResponse(f"/review?msg={done}", status_code=303) if outcome.kind == "ok" else None
    )


# ------------------------------------------------------------------------------ overview
@router.get("/review")
def review_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _overview(request, ctx, msg)


# ------------------------------------------------------------------------------ enable
def _enable_summary(days: int) -> tuple[str, ...]:
    return (
        f"Packages are kept for {days} days, then expire and their files are removed.",
        "Enabling allows an ADMIN to request packages. It does not create one.",
    )


@router.get("/review/enable/request")
def enable_request(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    retention_days: Annotated[int, Query(ge=1, le=90)] = 14,
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _confirm_page(
        request,
        ctx,
        "enable",
        _enable_summary(retention_days),
        (("retention_days", str(retention_days)),),
        msg,
    )


@router.post("/review/enable/reauth")
def enable_reauth(
    request: Request,
    form: Annotated[ReviewEnableReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(
            f"/review/enable/request?retention_days={form.retention_days}&msg={code}",
            status_code=303,
        )
    return _confirm_page(
        request,
        ctx,
        "enable",
        _enable_summary(form.retention_days),
        (("retention_days", str(form.retention_days)),),
        code,
        429 if code == "throttled" else 400,
    )


@router.post("/review/enable/confirm")
def enable_confirm(
    request: Request,
    form: Annotated[ReviewEnableConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).review.enable(
        ctx, _actor(request, ctx), form.confirmation, form.retention_days
    )
    done = _after(request, ctx, outcome, "review_enabled")
    if done:
        return done
    return _confirm_page(
        request,
        ctx,
        "enable",
        _enable_summary(form.retention_days),
        (("retention_days", str(form.retention_days)),),
        _FLASH.get(outcome.kind, "review_conflict"),
        _STATUS.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ disable
_DISABLE_SUMMARY = (
    "Disabling blocks new requests, verification and downloads. Existing packages are kept until "
    "retention expires them.",
)


@router.get("/review/disable/request")
def disable_request(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _confirm_page(request, ctx, "disable", _DISABLE_SUMMARY, (), msg)


@router.post("/review/disable/reauth")
def disable_reauth(
    request: Request,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(f"/review/disable/request?msg={code}", status_code=303)
    return _confirm_page(
        request, ctx, "disable", _DISABLE_SUMMARY, (), code, 429 if code == "throttled" else 400
    )


@router.post("/review/disable/confirm")
def disable_confirm(
    request: Request,
    form: Annotated[ReviewPlainConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).review.disable(ctx, _actor(request, ctx), form.confirmation)
    done = _after(request, ctx, outcome, "review_disabled")
    if done:
        return done
    return _confirm_page(
        request,
        ctx,
        "disable",
        _DISABLE_SUMMARY,
        (),
        _FLASH.get(outcome.kind, "review_conflict"),
        _STATUS.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ create
def _create_page(
    request: Request,
    ctx: AuthContext,
    start: date,
    end: date,
    scope: list[str],
    message: str | None,
    status: int = 200,
) -> Response:
    chosen, problems = get_services(request).review.validate_request(start, end, scope)
    if problems:
        return _overview(request, ctx, "review_invalid", 400)
    summary = (
        f"Period: {start} to {end} (UTC dates, inclusive).",
        "Scope: " + ", ".join(views.SCOPE_LABELS[s] for s in chosen) + ".",
        "The package is requested now and built by the host; it contains no free text, secrets or "
        "private data.",
    )
    hidden = (
        ("period_start", start.isoformat()),
        ("period_end", end.isoformat()),
        *(("scope", s) for s in chosen),
    )
    return _confirm_page(request, ctx, "create", summary, hidden, message, status)


@router.get("/review/packages/create/request")
def create_request(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    period_start: date,
    period_end: date,
    scope: Annotated[list[str], Query(max_length=6)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _create_page(request, ctx, period_start, period_end, scope, msg)


@router.post("/review/packages/create/reauth")
def create_reauth(
    request: Request,
    form: Annotated[ReviewRequestReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        query = f"period_start={form.period_start}&period_end={form.period_end}" + "".join(
            f"&scope={s}" for s in form.scope
        )
        return RedirectResponse(
            f"/review/packages/create/request?{query}&msg={code}", status_code=303
        )
    return _create_page(
        request,
        ctx,
        form.period_start,
        form.period_end,
        list(form.scope),
        code,
        429 if code == "throttled" else 400,
    )


@router.post("/review/packages/create/confirm")
def create_confirm(
    request: Request,
    form: Annotated[ReviewRequestConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).review.request(
        ctx,
        _actor(request, ctx),
        form.confirmation,
        form.period_start,
        form.period_end,
        list(form.scope),
    )
    if outcome.kind == "ok" and outcome.package_id:
        return RedirectResponse(
            f"/review/packages/{outcome.package_id}?msg=review_requested", status_code=303
        )
    if outcome.kind in ("invalid", "disabled", "limit"):
        return _overview(request, ctx, _FLASH[outcome.kind], _STATUS[outcome.kind])
    return _create_page(
        request,
        ctx,
        form.period_start,
        form.period_end,
        list(form.scope),
        _FLASH.get(outcome.kind, "review_conflict"),
        _STATUS.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ one package
def _detail(
    request: Request, ctx: AuthContext, package_id: UUID, message: str | None, status: int = 200
) -> Response:
    services = get_services(request)
    row = services.review.package(package_id)
    if row is None:
        raise HTTPException(status_code=404)
    enabled = services.review.overview(ctx).settings.enabled
    view = views.build_detail(row, enabled, message)
    return services.renderer.html(
        "review_package.html", status, auth=ctx, active="review", view=view
    )


@router.get("/review/packages/{package_id}")
def package_page(
    request: Request,
    package_id: UUID,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _detail(request, ctx, package_id, msg)


@router.post("/review/packages/{package_id}/verify")
def package_verify(
    request: Request,
    package_id: UUID,
    form: Annotated[ReviewPackageAction, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    result = get_services(request).review.verify(_actor(request, ctx), package_id)
    if result.outcome.kind == "ok":
        return RedirectResponse(
            f"/review/packages/{package_id}?msg=review_verified", status_code=303
        )
    if result.outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    return _detail(
        request,
        ctx,
        package_id,
        _FLASH.get(result.outcome.kind, "review_conflict"),
        _STATUS.get(result.outcome.kind, 409),
    )


@router.post("/review/packages/{package_id}/download")
def package_download(
    request: Request,
    package_id: UUID,
    form: Annotated[ReviewPackageAction, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    result = get_services(request).review.download(_actor(request, ctx), package_id)
    if result.outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    if result.outcome.kind != "ok":
        return _detail(
            request,
            ctx,
            package_id,
            _FLASH.get(result.outcome.kind, "review_conflict"),
            _STATUS.get(result.outcome.kind, 409),
        )
    # Never rendered: an opaque attachment with no sniffing and no caching.
    return Response(
        result.data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


__all__ = ["router", "SCOPES"]
