"""Proposal pages and actions (ADMIN only). Every proposal is UNTRUSTED ADVISORY INPUT.

Nothing here executes, evaluates, renders or applies proposal content, and nothing here changes a
pair, order, ledger, strategy, risk setting, configuration or live state. Approval only records a
manual change request. Sensitive actions never write on GET: the request page only displays;
`reauth` and `confirm` are CSRF-protected POSTs.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import (
    ProposalAttestForm,
    ProposalChangeRequestForm,
    ProposalCloseForm,
    ProposalDisableForm,
    ProposalReviewForm,
    ReviewPlainConfirm,
    ReviewPlainReauth,
)
from app.domain.enums import ActorRole
from app.domain.models import AuditActor, AuthContext
from app.domain.pairs import ActorClass
from app.domain.permissions import Permission
from app.pairs.transitions import Actor
from app.proposals.service import Outcome
from app.web import proposal_views as views

router = APIRouter()
_admin = require_permission(Permission.MANAGE_PROPOSALS)
AttestPath = Literal["implemented", "backtested", "paper-validated"]

_FLASH: dict[str, str] = {
    "phrase_mismatch": "phrase_mismatch",
    "reauth_required": "reauth_required",
    "conflict": "proposal_conflict",
    "disabled": "proposal_disabled",
    "limit": "proposal_limit",
    "invalid": "proposal_invalid",
    "not_allowed": "proposal_not_allowed",
    "not_found": "not_found",
}
_STATUS: dict[str, int] = {
    "phrase_mismatch": 400,
    "reauth_required": 400,
    "conflict": 409,
    "disabled": 409,
    "limit": 429,
    "invalid": 400,
    "not_allowed": 409,
    "not_found": 404,
    "rejected_input": 400,
}


def _actor(request: Request, ctx: AuthContext) -> Actor:
    return Actor(
        AuditActor(ctx.user.id, ActorRole(ctx.user.role.value)),
        ActorClass.WEB,
        resolve_client(request).tag,
        request_id(request),
    )


def _flash_of(outcome: Outcome) -> str:
    return _FLASH.get(outcome.kind, "proposal_conflict")


def _reauth(request: Request, ctx: AuthContext, password: str) -> str:
    outcome = get_services(request).auth.reauthenticate(
        ctx, password, resolve_client(request), request_id(request)
    )
    return {"ok": "reauth_ok", "throttled": "throttled"}.get(outcome, "reauth_invalid")


def _overview(
    request: Request, ctx: AuthContext, message: str | None, status: int = 200
) -> Response:
    services = get_services(request)
    view = views.build_overview(services.proposals.overview(ctx), message)
    return services.renderer.html("proposals.html", status, auth=ctx, active="proposals", view=view)


def _render_confirm(
    request: Request, ctx: AuthContext, view: views.ConfirmView, status: int = 200
) -> Response:
    return get_services(request).renderer.html(
        "proposal_confirm.html", status, auth=ctx, active="proposals", view=view
    )


def _detail(
    request: Request, ctx: AuthContext, proposal_id: UUID, message: str | None, status: int = 200
) -> Response:
    services = get_services(request)
    found = services.proposals.detail(ctx, proposal_id)
    if found is None:
        raise HTTPException(status_code=404)
    return services.renderer.html(
        "proposal_detail.html",
        status,
        auth=ctx,
        active="proposals",
        view=views.build_detail(found, message),
    )


def _active(request: Request, ctx: AuthContext) -> bool:
    return get_services(request).proposals.reauth_active(ctx)


# ------------------------------------------------------------------------------ overview
@router.get("/review/proposals")
def proposals_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _overview(request, ctx, msg)


# ------------------------------------------------------------------------------ enable / disable
@router.get("/review/proposals/import-enable/request")
def enable_request(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _render_confirm(request, ctx, views.confirm_enable(_active(request, ctx), msg))


@router.post("/review/proposals/import-enable/reauth")
def enable_reauth(
    request: Request,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(
            f"/review/proposals/import-enable/request?msg={code}", status_code=303
        )
    return _render_confirm(
        request,
        ctx,
        views.confirm_enable(_active(request, ctx), code),
        429 if code == "throttled" else 400,
    )


@router.post("/review/proposals/import-enable/confirm")
def enable_confirm(
    request: Request,
    form: Annotated[ReviewPlainConfirm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).proposals.enable_import(
        ctx, _actor(request, ctx), form.confirmation
    )
    if outcome.kind == "ok":
        return RedirectResponse("/review/proposals?msg=proposal_import_enabled", status_code=303)
    return _render_confirm(
        request,
        ctx,
        views.confirm_enable(_active(request, ctx), _flash_of(outcome)),
        _STATUS.get(outcome.kind, 409),
    )


@router.post("/review/proposals/import-disable")
def import_disable(
    request: Request,
    form: Annotated[ProposalDisableForm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).proposals.disable_import(_actor(request, ctx))
    if outcome.kind == "ok":
        return RedirectResponse("/review/proposals?msg=proposal_import_disabled", status_code=303)
    return _overview(request, ctx, _flash_of(outcome), _STATUS.get(outcome.kind, 409))


# ------------------------------------------------------------------------------ import
@router.get("/review/proposals/import/request")
def import_request(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _render_confirm(request, ctx, views.confirm_import(_active(request, ctx), msg))


@router.post("/review/proposals/import/reauth")
def import_reauth(
    request: Request,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(f"/review/proposals/import/request?msg={code}", status_code=303)
    return _render_confirm(
        request,
        ctx,
        views.confirm_import(_active(request, ctx), code),
        429 if code == "throttled" else 400,
    )


@router.post("/review/proposals/import/confirm")
async def import_confirm(
    request: Request, ctx: Annotated[AuthContext, Depends(_admin)]
) -> Response:
    """One file part OR pasted text, plus the phrase. The form was already parsed (and its size
    bounded) by the CSRF guard; here it is checked field by field and nothing is trusted."""
    services = get_services(request)
    limits = services.settings.proposals

    def refuse(code: str, status: int = 400) -> Response:
        return _render_confirm(
            request, ctx, views.confirm_import(_active(request, ctx), None, code), status
        )

    form = await request.form()
    names = [name for name, _value in form.multi_items()]
    if len(names) != len(set(names)) or set(names) - {
        "csrf_token",
        "confirmation",
        "text",
        "file",
    }:
        return refuse("SOURCE_NOT_ALLOWED")
    typed = form.get("confirmation")
    upload = form.get("file")
    text = form.get("text")
    has_file = isinstance(upload, UploadFile) and bool(upload.filename or upload.size)
    has_text = isinstance(text, str) and bool(text.strip())
    if (
        not isinstance(typed, str)
        or has_file == has_text
        or ("file" in form and not isinstance(upload, UploadFile))
    ):
        return refuse("SOURCE_NOT_ALLOWED")
    if isinstance(upload, UploadFile) and has_file:
        data = await upload.read(limits.max_bytes + 1)
        mime, filename = upload.content_type, upload.filename
    elif isinstance(text, str):
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            return refuse("NOT_UTF8")
        mime, filename = "text/plain", None
    else:  # pragma: no cover  (has_file != has_text guarantees one source)
        return refuse("SOURCE_NOT_ALLOWED")
    outcome = services.proposals.import_proposal(
        ctx, _actor(request, ctx), typed, declared_mime=mime, filename=filename, data=data
    )
    if outcome.kind == "ok" and outcome.proposal_id:
        return RedirectResponse(
            f"/review/proposals/{outcome.proposal_id}?msg=proposal_imported", status_code=303
        )
    if outcome.kind in ("rejected_input", "limit", "disabled") and outcome.reasons:
        return refuse(outcome.reasons[0], _STATUS.get(outcome.kind, 400))
    return _render_confirm(
        request,
        ctx,
        views.confirm_import(_active(request, ctx), _flash_of(outcome)),
        _STATUS.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ one proposal
@router.get("/review/proposals/{proposal_id}")
def proposal_page(
    request: Request,
    proposal_id: UUID,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return _detail(request, ctx, proposal_id, msg)


def _after(
    request: Request, ctx: AuthContext, proposal_id: UUID, outcome: Outcome, done: str
) -> Response:
    if outcome.kind == "ok":
        return RedirectResponse(f"/review/proposals/{proposal_id}?msg={done}", status_code=303)
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    return _detail(request, ctx, proposal_id, _flash_of(outcome), _STATUS.get(outcome.kind, 409))


@router.post("/review/proposals/{proposal_id}/review")
def proposal_review(
    request: Request,
    proposal_id: UUID,
    form: Annotated[ProposalReviewForm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).proposals.review(_actor(request, ctx), proposal_id, form.notes)
    return _after(request, ctx, proposal_id, outcome, "proposal_reviewed")


@router.post("/review/proposals/{proposal_id}/close")
def proposal_close(
    request: Request,
    proposal_id: UUID,
    form: Annotated[ProposalCloseForm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).proposals.close(_actor(request, ctx), proposal_id, form.reason)
    return _after(request, ctx, proposal_id, outcome, "proposal_closed")


# ------------------------------------------------------------------------------ change request
@router.get("/review/proposals/{proposal_id}/change-request/request")
def cr_request(
    request: Request,
    proposal_id: UUID,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    if get_services(request).proposals.detail(ctx, proposal_id) is None:
        raise HTTPException(status_code=404)
    return _render_confirm(
        request, ctx, views.confirm_change_request(proposal_id, _active(request, ctx), msg)
    )


@router.post("/review/proposals/{proposal_id}/change-request/reauth")
def cr_reauth(
    request: Request,
    proposal_id: UUID,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(
            f"/review/proposals/{proposal_id}/change-request/request?msg={code}", status_code=303
        )
    return _render_confirm(
        request,
        ctx,
        views.confirm_change_request(proposal_id, _active(request, ctx), code),
        429 if code == "throttled" else 400,
    )


@router.post("/review/proposals/{proposal_id}/change-request/confirm")
def cr_confirm(
    request: Request,
    proposal_id: UUID,
    form: Annotated[ProposalChangeRequestForm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    outcome = get_services(request).proposals.create_change_request(
        ctx,
        _actor(request, ctx),
        proposal_id,
        form.confirmation,
        change_type=form.change_type,
        impact=form.impact_assessment,
        ceilings_unaffected=form.ceilings_unaffected,
    )
    if outcome.kind == "ok":
        return RedirectResponse(
            f"/review/proposals/{proposal_id}?msg=proposal_change_request", status_code=303
        )
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    return _render_confirm(
        request,
        ctx,
        views.confirm_change_request(proposal_id, _active(request, ctx), _flash_of(outcome)),
        _STATUS.get(outcome.kind, 409),
    )


# ------------------------------------------------------------------------------ attestations
@router.get("/review/proposals/{proposal_id}/attest/{kind}/request")
def attest_request(
    request: Request,
    proposal_id: UUID,
    kind: AttestPath,
    ctx: Annotated[AuthContext, Depends(_admin)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    if get_services(request).proposals.detail(ctx, proposal_id) is None:
        raise HTTPException(status_code=404)
    return _render_confirm(
        request,
        ctx,
        views.confirm_attest(proposal_id, views.ATTEST_FROM_PATH[kind], _active(request, ctx), msg),
    )


@router.post("/review/proposals/{proposal_id}/attest/{kind}/reauth")
def attest_reauth(
    request: Request,
    proposal_id: UUID,
    kind: AttestPath,
    form: Annotated[ReviewPlainReauth, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    code = _reauth(request, ctx, form.password)
    if code == "reauth_ok":
        return RedirectResponse(
            f"/review/proposals/{proposal_id}/attest/{kind}/request?msg={code}", status_code=303
        )
    return _render_confirm(
        request,
        ctx,
        views.confirm_attest(
            proposal_id, views.ATTEST_FROM_PATH[kind], _active(request, ctx), code
        ),
        429 if code == "throttled" else 400,
    )


@router.post("/review/proposals/{proposal_id}/attest/{kind}/confirm")
def attest_confirm(
    request: Request,
    proposal_id: UUID,
    kind: AttestPath,
    form: Annotated[ProposalAttestForm, Form()],
    ctx: Annotated[AuthContext, Depends(_admin)],
) -> Response:
    full = views.ATTEST_FROM_PATH[kind]
    ids = [line.strip() for line in (form.report_ids or "").splitlines() if line.strip()]
    outcome = get_services(request).proposals.attest(
        ctx,
        _actor(request, ctx),
        proposal_id,
        full,
        reference=(form.reference or "").strip() or None,
        report_ids=ids,
    )
    if outcome.kind == "ok":
        return RedirectResponse(
            f"/review/proposals/{proposal_id}?msg=proposal_{full.lower()}", status_code=303
        )
    if outcome.kind == "not_found":
        raise HTTPException(status_code=404)
    return _render_confirm(
        request,
        ctx,
        views.confirm_attest(proposal_id, full, _active(request, ctx), _flash_of(outcome)),
        _STATUS.get(outcome.kind, 409),
    )
