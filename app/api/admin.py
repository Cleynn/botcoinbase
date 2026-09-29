"""Security page, audit page and session-revocation actions. None of these touch trading state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.api.dependencies import get_services, request_id, require_permission, resolve_client
from app.api.schemas import RevokeOthersForm, RevokeSessionForm, RevokeUserSessionsForm
from app.domain.enums import AuditEventType
from app.domain.models import AuthContext
from app.domain.permissions import Permission
from app.web.view_models import build_audit, build_security

router = APIRouter()

_security_access = require_permission(Permission.VIEW_OWN_SECURITY)
_revoke_own_access = require_permission(Permission.REVOKE_OWN_SESSIONS)
_revoke_user_access = require_permission(Permission.REVOKE_USER_SESSIONS)
_audit_access = require_permission(Permission.VIEW_AUDIT)

AUDIT_PAGE_SIZE = 50


def render_security(
    request: Request,
    ctx: AuthContext,
    *,
    status: int = 200,
    message: str | None = None,
    rules: tuple[str, ...] = (),
) -> Response:
    services = get_services(request)
    overview = services.auth.overview(ctx)
    view = build_security(
        services.settings,
        ctx,
        overview.sessions,
        overview.users,
        overview.reauth_active,
        message=message,
        rules=rules,
    )
    return services.renderer.html("security.html", status, auth=ctx, active="security", view=view)


@router.get("/security")
def security_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_security_access)],
    msg: Annotated[str | None, Query(max_length=32)] = None,
) -> Response:
    return render_security(request, ctx, message=msg)


@router.post("/security/sessions/revoke-others")
def revoke_others(
    request: Request,
    form: Annotated[RevokeOthersForm, Form()],
    ctx: Annotated[AuthContext, Depends(_revoke_own_access)],
) -> Response:
    get_services(request).auth.revoke_own_others(ctx, resolve_client(request), request_id(request))
    return RedirectResponse("/security?msg=others_revoked", status_code=303)


@router.post("/security/sessions/revoke")
def revoke_one(
    request: Request,
    form: Annotated[RevokeSessionForm, Form()],
    ctx: Annotated[AuthContext, Depends(_revoke_own_access)],
) -> Response:
    revoked = get_services(request).auth.revoke_own_session(
        ctx, form.session_id, resolve_client(request), request_id(request)
    )
    if not revoked:
        return render_security(request, ctx, status=400, message="invalid_request")
    return RedirectResponse("/security?msg=session_revoked", status_code=303)


@router.post("/security/users/revoke-sessions")
def revoke_user_sessions(
    request: Request,
    form: Annotated[RevokeUserSessionsForm, Form()],
    ctx: Annotated[AuthContext, Depends(_revoke_user_access)],
) -> Response:
    outcome = get_services(request).auth.admin_revoke_user_sessions(
        ctx, form.user_id, form.confirmation, resolve_client(request), request_id(request)
    )
    if outcome == "ok":
        return RedirectResponse("/security?msg=user_sessions_revoked", status_code=303)
    return render_security(
        request, ctx, status=400 if outcome != "not_found" else 404, message=outcome
    )


@router.get("/audit")
def audit_page(
    request: Request,
    ctx: Annotated[AuthContext, Depends(_audit_access)],
    code: Annotated[str, Query(max_length=64)] = "",
    before: Annotated[int | None, Query(ge=1, le=2**62)] = None,
) -> Response:
    services = get_services(request)
    codes = tuple(e.value for e in AuditEventType)
    if code and code not in codes:
        return services.renderer.html(
            "error.html", 400, auth=ctx, status_code_text=400, message="The request was invalid."
        )
    records, chain = services.auth.audit_page(
        code=AuditEventType(code) if code else None, before=before, limit=AUDIT_PAGE_SIZE
    )
    view = build_audit(records, chain, code=code, codes=codes, page_size=AUDIT_PAGE_SIZE)
    return services.renderer.html("audit.html", auth=ctx, active="audit", view=view)
